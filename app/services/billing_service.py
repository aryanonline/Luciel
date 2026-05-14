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

from sqlalchemy import func, select
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
# Step 30a.2: paid-intro trial replaces per-(tier,cadence) free trial.
#
# Design decision (locked 2026-05-14): every primitive — all 6
# (tier, cadence) pairs, monthly AND annual — receives the SAME 90-day
# trial gated on a single one-time $100 CAD intro fee charged at
# checkout. Rationale:
#
#   * a paid trial filters tire-kickers (the 14-day free trial in
#     Step 30a.1 produced zero conversions in v1, see DRIFTS
#     D-trial-policy-mixed-per-tier-2026-05-14);
#   * a 90-day window matches Luciel's real evaluation cycle for a
#     real-estate brokerage (full lead-gen quarter);
#   * a single uniform trial collapses 6 free-trial decisions into 1
#     paid-intro decision, simplifying the marketing-page copy and
#     the Stripe Price catalog (1 intro_fee Price for all 6 plans).
#
# First-time semantics (locked 2026-05-14): "first-time" means
# customer_email has NEVER appeared on a Subscription row before
# (active=True OR active=False). A canceled customer who rejoins
# pays the plan rate immediately, no second intro fee. See
# ``is_first_time_customer`` below.
#
# Migration from Step 30a.1: ``TRIAL_DAYS`` is removed. ``resolve_trial_days``
# stays for any in-flight test fixtures but always returns 0 (no free
# trial). Tests asserting the old dict shape are updated to assert the
# new constant + first-time gate behaviour.
# ---------------------------------------------------------------------

INTRO_TRIAL_DAYS: int = 90
INTRO_FEE_PRICE_KEY: str = "stripe_price_intro_fee"


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
        """Step 30a.2 stub — always returns 0 (no FREE trial).

        Free trials were removed in favour of a uniform $100/90d paid
        intro for all primitives (see module docstring). This method is
        kept as a no-op shim so any out-of-tree test fixtures still call
        it without exploding; the real trial wiring lives in
        ``_compute_trial_and_intro_for_checkout`` below.
        """
        del tier, cadence  # explicitly unused
        return 0

    def resolve_intro_fee_price_id(self) -> str:
        """Return the Stripe Price ID for the one-time $100 CAD intro fee.

        Raises ``BillingNotConfiguredError`` if the
        ``stripe_price_intro_fee`` setting is empty — the route layer
        maps this to 501 just like any other missing-Price case.
        """
        price_id = getattr(settings, INTRO_FEE_PRICE_KEY, "") or ""
        if not price_id:
            raise BillingNotConfiguredError(
                f"Stripe price id setting '{INTRO_FEE_PRICE_KEY}' is empty; "
                f"cannot start checkout with intro fee."
            )
        return price_id

    def is_first_time_customer(self, *, email: str) -> bool:
        """Return True iff ``email`` has never been on ANY Subscription row.

        "First-time" is locked to a tenant-identity-once-ever policy
        (cancel+rejoin yields False so the rejoiner pays plan rate, no
        second intro). We implement this by joining against the entire
        ``subscriptions`` table on ``customer_email`` regardless of
        ``active`` — even a soft-deleted row counts as a prior touch.

        Both sides of the comparison are lower-cased via SQL ``LOWER()``
        so the lookup is symmetric with however the webhook writer stored
        the email (it preserves whatever came in via Stripe metadata, see
        ``billing_webhook_service.py``). Stripe itself is case-insensitive
        on customer_email but our column is plain VARCHAR(320), so
        "Foo@example.com" must match a row stored as "foo@example.com".

        Performance: a functional ``LOWER()`` index on customer_email is
        NOT in place; this query becomes a sequential scan in the limit
        of large ``subscriptions`` tables. Acceptable at Luciel's current
        scale (subscriptions are O(tenants), tens to low thousands). If
        the table grows past ~50k rows, add
        ``CREATE INDEX ix_subscriptions_customer_email_lower
         ON subscriptions (LOWER(customer_email));``
        in a follow-up migration and Postgres will pick it up
        automatically without code change.
        """
        normalized = (email or "").strip().lower()
        if not normalized:
            # Empty email cannot be "first-time" because we cannot
            # correlate it to anything; treat as not-first-time so a
            # caller bug never accidentally hands out a free intro to
            # everyone. The route layer's schema validates email is
            # non-empty before we reach here.
            return False
        stmt = (
            select(Subscription.id)
            .where(func.lower(Subscription.customer_email) == normalized)
            .limit(1)
        )
        existing = self.db.execute(stmt).scalars().first()
        return existing is None

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

        # Step 30a.2: first-time gate. Resolve the recurring Price ID
        # FIRST so a missing recurring config fails 501 before we touch
        # the intro fee path; only then check first-time and resolve the
        # intro fee. Order matters because the recurring miss is the
        # higher-impact error (no checkout possible at all) and we want
        # callers to see it before a missing intro fee Price ID.
        first_time = self.is_first_time_customer(email=email)
        intro_fee_price_id: str | None = None
        trial_days: int = 0
        if first_time:
            intro_fee_price_id = self.resolve_intro_fee_price_id()
            trial_days = INTRO_TRIAL_DAYS

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
            # Step 30a.2: stamp the intro decision into Stripe metadata
            # so the webhook handler can later audit "this Subscription
            # was created on the intro path" without re-deriving from
            # the line_items array. Boolean serialized as 'true'/'false'
            # because Stripe metadata values must be strings.
            "luciel_intro_applied": "true" if first_time else "false",
        }

        session = self.stripe.create_checkout_session(
            customer_email=email,
            price_id=price_id,
            success_url=success_url,
            cancel_url=cancel_url,
            trial_period_days=trial_days or None,
            metadata=metadata,
            intro_fee_price_id=intro_fee_price_id,
        )

        logger.info(
            "billing: checkout session created stripe_id=%s email=%s tier=%s cadence=%s "
            "first_time=%s intro_fee=%s trial_days=%s",
            getattr(session, "id", "?"),
            email,
            tier,
            billing_cadence,
            first_time,
            bool(intro_fee_price_id),
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
