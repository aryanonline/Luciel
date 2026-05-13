"""Step 30a: BillingService.

Read-mostly facade around the ``subscriptions`` table plus the two
thin Stripe write paths (checkout creation, portal session creation).
The heavy lifting -- minting tenants on payment, cascading
deactivation on cancel -- lives in ``BillingWebhookService``; this
class is what the API routes call from the request path.

Separation rationale:

  - Request-path code (BillingService) runs inside a request, has an
    AuditContext built from request.state, and either reads
    subscription state for the cookied user or *initiates* a Stripe
    operation that will eventually round-trip through a webhook.
  - Webhook-path code (BillingWebhookService) runs from a Stripe
    POST, has a synthetic AuditContext labelled ``stripe_webhook``,
    and is the only place that mutates ``subscriptions`` rows.

Keeping the two paths separate means a future change to the webhook
handler (e.g. adding a new event type) cannot accidentally tee into
the request-path audit trail, and a route-level test does not need a
running webhook listener.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.integrations.stripe import StripeClient
from app.models.subscription import (
    ALLOWED_BILLING_CADENCES,
    ALLOWED_TIERS,
    BILLING_CADENCE_ANNUAL,
    BILLING_CADENCE_MONTHLY,
    Subscription,
    TIER_COMPANY,
    TIER_INDIVIDUAL,
    TIER_INSTANCE_CAPS,
    TIER_TEAM,
)

logger = logging.getLogger(__name__)


class BillingNotConfiguredError(Exception):
    """Raised when a billing route is called but Stripe is not configured.

    The route layer maps this to HTTP 501 Not Implemented so a CI run
    against a backend without billing env vars exits with a clear
    message rather than a 500 from a Stripe library error.
    """


# ---------------------------------------------------------------------
# Step 30a.1: (tier, cadence) → settings-attribute-name lookup.
#
# The single source of truth for which env var holds which Stripe Price
# ID. ``resolve_price_id`` reads from ``settings`` via getattr to keep
# the table data, not code — a future tier addition only adds rows here
# and a parallel ``stripe_price_*`` setting.
#
# Pattern: each setting may be empty in dev/CI; ``resolve_price_id``
# raises ``BillingNotConfiguredError`` so the route layer answers 501.
# ---------------------------------------------------------------------

PRICE_ID_KEY: dict[tuple[str, str], str] = {
    (TIER_INDIVIDUAL, BILLING_CADENCE_MONTHLY): "stripe_price_individual",
    (TIER_INDIVIDUAL, BILLING_CADENCE_ANNUAL):  "stripe_price_individual_annual",
    (TIER_TEAM,       BILLING_CADENCE_MONTHLY): "stripe_price_team_monthly",
    (TIER_TEAM,       BILLING_CADENCE_ANNUAL):  "stripe_price_team_annual",
    (TIER_COMPANY,    BILLING_CADENCE_MONTHLY): "stripe_price_company_monthly",
    (TIER_COMPANY,    BILLING_CADENCE_ANNUAL):  "stripe_price_company_annual",
}


# ---------------------------------------------------------------------
# Step 30a.1: (tier, cadence) → trial_period_days.
#
# Per design doc §2:
#   - Individual monthly: 14d (unchanged from Step 30a).
#   - Team monthly:        7d.
#   - Company monthly:     7d.
#   - All annual cadences: 0d (no trial on a prepay commitment).
#
# The webhook reads these onto Stripe's ``subscription_data.trial_period_days``
# at Checkout-session creation time.
# ---------------------------------------------------------------------

TRIAL_DAYS: dict[tuple[str, str], int] = {
    (TIER_INDIVIDUAL, BILLING_CADENCE_MONTHLY): 14,
    (TIER_INDIVIDUAL, BILLING_CADENCE_ANNUAL):  0,
    (TIER_TEAM,       BILLING_CADENCE_MONTHLY): 7,
    (TIER_TEAM,       BILLING_CADENCE_ANNUAL):  0,
    (TIER_COMPANY,    BILLING_CADENCE_MONTHLY): 7,
    (TIER_COMPANY,    BILLING_CADENCE_ANNUAL):  0,
}


class BillingService:
    def __init__(self, db: Session, stripe_client: StripeClient) -> None:
        self.db = db
        self.stripe = stripe_client

    # -----------------------------------------------------------------
    # Configuration gate -- routes call this first
    # -----------------------------------------------------------------

    def require_configured(self) -> None:
        """Raises ``BillingNotConfiguredError`` if Stripe is not configured.

        Step 30a.1: we no longer require ``stripe_price_individual`` at
        boot — each (tier, cadence) pair is validated *lazily* in
        ``resolve_price_id`` when the checkout route picks one. The
        secret-key check stays at boot/route so a backend without any
        Stripe config still returns 501 immediately.
        """
        if not self.stripe.is_configured:
            raise BillingNotConfiguredError("Stripe secret key not configured.")

    # -----------------------------------------------------------------
    # Step 30a.1: (tier, cadence) → price id / trial days helpers
    # -----------------------------------------------------------------

    def resolve_price_id(self, *, tier: str, cadence: str) -> str:
        """Return the Stripe Price ID for the requested (tier, cadence) pair.

        Raises:
          ValueError                if the pair is not in PRICE_ID_KEY
                                    (shouldn't happen — schema validates).
          BillingNotConfiguredError if the matching settings field is
                                    empty (i.e. the env var was not set
                                    in prod). The route layer maps this
                                    to 501.
        """
        key = PRICE_ID_KEY.get((tier, cadence))
        if key is None:
            raise ValueError(
                f"Unsupported (tier={tier!r}, cadence={cadence!r}). "
                f"Allowed tiers: {ALLOWED_TIERS}. "
                f"Allowed cadences: {ALLOWED_BILLING_CADENCES}."
            )
        price_id = getattr(settings, key, "") or ""
        if not price_id:
            raise BillingNotConfiguredError(
                f"Stripe price id setting '{key}' is empty; "
                f"cannot start checkout for ({tier}, {cadence})."
            )
        return price_id

    @staticmethod
    def resolve_trial_days(*, tier: str, cadence: str) -> int:
        """Return the trial_period_days for the requested (tier, cadence).

        Falls back to ``settings.billing_trial_days`` for the Individual-
        monthly path (preserves Step 30a behaviour); 0 for anything not
        in TRIAL_DAYS (annual + unknown).
        """
        if (tier, cadence) in TRIAL_DAYS:
            return TRIAL_DAYS[(tier, cadence)]
        # Defensive fallback — should be unreachable in practice.
        return settings.billing_trial_days if (
            tier == TIER_INDIVIDUAL and cadence == BILLING_CADENCE_MONTHLY
        ) else 0

    @staticmethod
    def resolve_instance_count_cap(*, tier: str) -> int:
        """Per-tier cap for the new ``subscriptions.instance_count_cap`` column.

        Tier strings outside ``ALLOWED_TIERS`` fall back to the Individual
        cap (3) defensively — the schema validator should have caught
        that already.
        """
        return TIER_INSTANCE_CAPS.get(tier, TIER_INSTANCE_CAPS[TIER_INDIVIDUAL])

    # -----------------------------------------------------------------
    # Checkout
    # -----------------------------------------------------------------

    def create_checkout(
        self,
        *,
        email: str,
        display_name: str,
        tier: str = TIER_INDIVIDUAL,
        billing_cadence: str = BILLING_CADENCE_MONTHLY,
    ) -> dict[str, str]:
        """Create a Stripe Checkout session and return the redirect URL + id.

        Step 30a.1 lifted the v1 Individual-only carve-out. The accepted
        (tier, cadence) pairs are exactly the keys of ``PRICE_ID_KEY``;
        ``resolve_price_id`` is the single source of truth.

        We pass four pieces of metadata into Stripe so the webhook
        handler can correlate without an extra Stripe API call:

          - ``luciel_email``            — the email the buyer entered.
          - ``luciel_display_name``     — carried onto TenantConfig.display_name.
          - ``luciel_tier``             — 'individual' | 'team' | 'company'.
          - ``luciel_billing_cadence``  — 'monthly' | 'annual'.

        The Stripe Checkout session also sets ``customer_email`` so
        Stripe pre-fills the email field; the metadata copy is the
        canonical value for our webhook because Stripe may transform
        the customer-side email during a Customer object create.
        """
        self.require_configured()

        # Tier / cadence validation. The schema (CheckoutSessionRequest)
        # uses Literal[...] so 422s are raised before this point in the
        # request path; this service-layer guard exists for callers that
        # don't go through the schema (tests, future internal mints).
        if tier not in ALLOWED_TIERS:
            raise ValueError(
                f"Unsupported tier {tier!r}. Allowed: {ALLOWED_TIERS}."
            )
        if billing_cadence not in ALLOWED_BILLING_CADENCES:
            raise ValueError(
                f"Unsupported billing_cadence {billing_cadence!r}. "
                f"Allowed: {ALLOWED_BILLING_CADENCES}."
            )

        price_id = self.resolve_price_id(tier=tier, cadence=billing_cadence)
        trial_days = self.resolve_trial_days(tier=tier, cadence=billing_cadence)

        # Build the redirect URLs. We accept {CHECKOUT_SESSION_ID} as
        # a Stripe placeholder in billing_success_url; Stripe substitutes
        # the real id when redirecting the buyer.
        success_url = settings.billing_success_url
        cancel_url = settings.billing_cancel_url

        metadata = {
            "luciel_email": email,
            "luciel_display_name": display_name,
            "luciel_tier": tier,
            "luciel_billing_cadence": billing_cadence,
        }

        session = self.stripe.create_checkout_session(
            customer_email=email,
            price_id=price_id,
            success_url=success_url,
            cancel_url=cancel_url,
            trial_period_days=trial_days or None,
            metadata=metadata,
        )

        logger.info(
            "billing: checkout session created stripe_id=%s email=%s tier=%s cadence=%s trial_days=%s",
            getattr(session, "id", "?"),
            email,
            tier,
            billing_cadence,
            trial_days,
        )
        return {
            "checkout_url": session.url,
            "session_id": session.id,
        }

    # -----------------------------------------------------------------
    # Portal
    # -----------------------------------------------------------------

    def create_portal_session_for_user(self, *, user_id: Any) -> str:
        """Look up the cookied user's current subscription and create a portal session.

        Raises ``LookupError`` if the user has no active subscription
        on file -- the route layer maps this to 404.
        """
        self.require_configured()
        sub = self.get_active_subscription_for_user(user_id=user_id)
        if sub is None:
            raise LookupError("No active subscription found for this user.")

        return_url = f"{settings.marketing_site_url.rstrip('/')}/account/billing"
        portal = self.stripe.create_portal_session(
            customer_id=sub.stripe_customer_id,
            return_url=return_url,
        )
        return portal.url

    # -----------------------------------------------------------------
    # Reads
    # -----------------------------------------------------------------

    def get_active_subscription_for_user(self, *, user_id: Any) -> Subscription | None:
        """Return the user's currently-active subscription, if any.

        "Active" here is the Pattern E soft-delete sense -- ``active=True``
        on the row. Whether that row is *entitled* (Stripe status in
        ENTITLED_STATUSES) is a separate read via ``Subscription.is_entitled``.
        """
        stmt = (
            select(Subscription)
            .where(Subscription.user_id == user_id, Subscription.active.is_(True))
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
        return self.db.execute(stmt).scalars().first()

    def get_subscription_by_stripe_id(self, *, stripe_subscription_id: str) -> Subscription | None:
        stmt = select(Subscription).where(
            Subscription.stripe_subscription_id == stripe_subscription_id
        )
        return self.db.execute(stmt).scalars().first()

    def get_active_subscription_for_tenant(self, *, tenant_id: str) -> Subscription | None:
        stmt = (
            select(Subscription)
            .where(Subscription.tenant_id == tenant_id, Subscription.active.is_(True))
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
        return self.db.execute(stmt).scalars().first()
