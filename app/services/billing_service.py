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
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.integrations.stripe import StripeClient
from app.models.admin_audit_log import (
    ACTION_SUBSCRIPTION_PILOT_REFUNDED,
    RESOURCE_SUBSCRIPTION,
)
from app.models.subscription import (
    ALLOWED_BILLING_CADENCES,
    ALLOWED_TIERS,
    BILLING_CADENCE_ANNUAL,
    BILLING_CADENCE_MONTHLY,
    STATUS_CANCELED,
    Subscription,
    TIER_COMPANY,
    TIER_INDIVIDUAL,
    TIER_INSTANCE_CAPS,
    TIER_TEAM,
)
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)

logger = logging.getLogger(__name__)


class BillingNotConfiguredError(Exception):
    """Raised when a billing route is called but Stripe is not configured.

    The route layer maps this to HTTP 501 Not Implemented so a CI run
    against a backend without billing env vars exits with a clear
    message rather than a 500 from a Stripe library error.
    """


# ---------------------------------------------------------------------
# Step 30a.2-pilot: typed errors for the self-serve refund route.
#
# Each maps to a single HTTP status in app/api/v1/billing.py so the
# marketing site can render a precise message ("you are not eligible" /
# "the 90-day window has closed" / "we cannot find your pilot charge")
# without parsing English. The detail strings are stable identifiers --
# the marketing site keys its copy off them.
# ---------------------------------------------------------------------

class NotFirstTimePilotError(Exception):
    """403 -- the buyer is not on the first-time intro path.

    Either ``provider_snapshot.metadata.luciel_intro_applied`` is
    'false' / missing (the buyer rejoined and pays plan rate, no intro
    fee to refund) or the subscription has no recorded trial. Either
    way the $100 was never charged to this buyer and a refund here
    would be an unrelated debit.
    """


class PilotWindowExpiredError(Exception):
    """409 -- the 90-day intro window has already closed.

    Once ``trial_end`` is in the past Stripe has already issued the
    first full-rate invoice; the intro fee is non-refundable past that
    boundary by the policy locked in CANONICAL_RECAP §14 \u00b6273.
    The buyer must use the customer portal to cancel the recurring
    subscription instead.
    """


class PilotChargeNotFoundError(Exception):
    """404 -- Stripe cannot locate the intro Charge to refund.

    This happens if the subscription was created out-of-band (no intro
    line item), the Charge was already refunded via the dashboard, or
    Stripe has wiped the test-mode artifact. Distinct from
    NotFirstTimePilotError so an auditor can tell the two cases apart
    in support tickets.
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

    # -----------------------------------------------------------------
    # Step 30a.2-pilot: self-serve refund of the one-time intro fee.
    # -----------------------------------------------------------------

    # Locked-canon refund amount (CANONICAL_RECAP \u00a714 \u00b6273).
    # Hard-coded rather than read from the Stripe Price object because:
    #   (a) the audit row must record the EXACT refund the buyer was
    #       promised, not whatever the Price object currently says
    #       (which a future operator could legitimately change for new
    #       buyers without affecting in-flight pilots);
    #   (b) the route returns the refunded amount synchronously so the
    #       UI can show "$100 refunded" before the webhook round-trip
    #       confirms it on the Stripe side.
    _PILOT_REFUND_AMOUNT_CENTS: int = 10000
    _PILOT_REFUND_CURRENCY: str = "cad"

    def process_pilot_refund(self, *, user) -> dict[str, Any]:
        """Refund the $100 intro fee and tear down the pilot.

        Eligibility (all must hold; first failure raises and aborts):
          1. The user has an active subscription on file.
          2. The subscription was created on the first-time intro path,
             evidenced by ``provider_snapshot.metadata.luciel_intro_applied
             == 'true'`` (stamped by ``create_checkout``). A subscription
             whose metadata says 'false' means the buyer was a returning
             customer who never paid the $100; refunding here would
             return a sum that was never charged.
          3. ``datetime.utcnow() <= trial_end``. Past day 91 the intro
             fee is non-refundable.
          4. Stripe can locate the Charge that funded the intro fee.

        Side effects, all atomic in a single DB transaction:
          * Stripe Refund.create against the intro Charge.
          * Stripe Subscription.cancel (no proration, no final invoice).
          * One AdminAuditLog row with action=ACTION_SUBSCRIPTION_PILOT_REFUNDED
            carrying stripe_refund_id + intro_charge_id +
            refunded_amount_cents + currency in after_json.
          * Local subscription row: active=False, status=STATUS_CANCELED,
            canceled_at=now.
          * Tenant cascade-deactivate via
            AdminService.deactivate_tenant_with_cascade so every child
            row (conversations, identity_claims, memory_items, api_keys,
            luciel_instances, agents, agent_configs, domain_configs,
            tenant_config) flips to active=False in lock-step.

        Stripe will subsequently fire ``customer.subscription.deleted``
        to our webhook; ``BillingWebhookService._on_subscription_deleted``
        is idempotent against an already-canceled row and will record a
        replay-rejected audit entry rather than re-cascading.

        Returns a dict (NOT a model) so the route layer can validate the
        shape against PilotRefundResponse explicitly; this keeps the
        service decoupled from the Pydantic schema.
        """
        self.require_configured()

        sub = self.get_active_subscription_for_user(user_id=user.id)
        if sub is None:
            raise LookupError("No active subscription on file.")

        # Eligibility (2): must be first-time intro path.
        #
        # Commit 3g: the eligibility signal is the metadata flag ALONE,
        # mirroring Commit 3f's read-path derivation in /api/v1/billing/me.
        # The earlier code had a belt-and-suspenders `or sub.trial_end is None`
        # clause that was intended to catch repeat customers, but it created
        # an asymmetry with the read path: degraded rows where the checkout.
        # session webhook landed before the Subscription was fully retrieved
        # (see drift D-stripe-webhook-checkout-vs-subscription-field-source-
        # 2026-05-15) have luciel_intro_applied=true + trial_end=null. The
        # read path now renders the refund CTA on those rows; the write path
        # MUST accept them too, or the user clicks a button that always 403s.
        #
        # Repeat-customer protection is still enforced upstream at checkout:
        # BillingService.is_first_time_customer guards create_checkout, so a
        # subscription with luciel_intro_applied=true can only exist if the
        # buyer truly was first-time at purchase time.
        snapshot = sub.provider_snapshot or {}
        snapshot_meta = (snapshot.get("metadata") or {}) if isinstance(snapshot, dict) else {}
        intro_applied = str(snapshot_meta.get("luciel_intro_applied", "")).lower() == "true"
        if not intro_applied:
            logger.info(
                "billing: pilot-refund rejected -- not on intro path "
                "user_id=%s sub=%s intro_applied=%s trial_end=%s",
                getattr(user, "id", "?"), sub.id, intro_applied, sub.trial_end,
            )
            raise NotFirstTimePilotError(
                "This subscription was not created on the first-time "
                "intro path; there is no $100 intro fee to refund."
            )

        # Eligibility (3): within the 90-day window.
        #
        # Commit 3g: window-end falls back to created_at + 90 days when
        # trial_end is null, mirroring the read-path's pilot_window_end
        # derivation. The fallback is deterministic from a column the row
        # always has (created_at is server-default NOT NULL), so it works
        # for every historical row including the Commit-3e degraded ones.
        now = datetime.now(timezone.utc)
        # trial_end is a tz-aware DateTime in the model; defensive
        # coerce in case a legacy row was written without tzinfo.
        trial_end = sub.trial_end
        if trial_end is not None and trial_end.tzinfo is None:
            trial_end = trial_end.replace(tzinfo=timezone.utc)
        if trial_end is not None:
            effective_window_end = trial_end
        elif sub.created_at is not None:
            sub_created = sub.created_at
            if sub_created.tzinfo is None:
                sub_created = sub_created.replace(tzinfo=timezone.utc)
            effective_window_end = sub_created + timedelta(days=90)
        else:
            # No timestamps at all -- treat as expired.
            effective_window_end = None
        if effective_window_end is None or now > effective_window_end:
            logger.info(
                "billing: pilot-refund rejected -- window expired "
                "user_id=%s sub=%s trial_end=%s window_end=%s now=%s",
                getattr(user, "id", "?"), sub.id, trial_end, effective_window_end, now,
            )
            raise PilotWindowExpiredError(
                "The 90-day intro window has closed; the intro fee is "
                "no longer refundable. Use the Customer Portal to cancel."
            )

        # Eligibility (4): Stripe can find the intro Charge.
        # We resolve the intro Price id from settings so the lookup is
        # informational (the helper does not branch on it today, but the
        # signature accepts it so a future amount-verification can flip
        # on without a Stripe-client signature change).
        try:
            intro_fee_price_id = self.resolve_intro_fee_price_id()
        except BillingNotConfiguredError:
            # If the intro Price id is not configured, the refund cannot
            # be authorized. Route layer maps this to 501.
            raise
        charge_id = self.stripe.find_intro_charge_id(
            stripe_subscription_id=sub.stripe_subscription_id,
            intro_fee_price_id=intro_fee_price_id,
        )
        if not charge_id:
            logger.error(
                "billing: pilot-refund cannot locate intro charge "
                "user_id=%s sub=%s stripe_sub=%s",
                getattr(user, "id", "?"), sub.id, sub.stripe_subscription_id,
            )
            raise PilotChargeNotFoundError(
                "Stripe cannot locate the intro charge for this "
                "subscription; contact support if this is unexpected."
            )

        # Stripe writes -- refund first, then cancel. If the cancel
        # call fails after a successful refund we still leave the local
        # state consistent (subscription row flipped, tenant cascaded)
        # because the customer has been made whole financially; the
        # Stripe-side subscription becomes a zombie that the webhook
        # will clean up on its next event delivery.
        idem_key = f"pilot-refund:{sub.stripe_subscription_id}:{charge_id}"
        refund = self.stripe.refund_charge(charge_id=charge_id, idempotency_key=idem_key)
        refund_id = getattr(refund, "id", None) or refund.get("id") if isinstance(refund, dict) else getattr(refund, "id", None)
        try:
            self.stripe.cancel_subscription(
                stripe_subscription_id=sub.stripe_subscription_id,
            )
        except Exception:  # pragma: no cover - Stripe boundary
            logger.exception(
                "billing: pilot-refund cancel-after-refund failed "
                "sub=%s refund=%s -- proceeding with local teardown",
                sub.stripe_subscription_id, refund_id,
            )

        # Local state mutations + audit + cascade -- one DB transaction.
        before_status = sub.status
        sub.status = STATUS_CANCELED
        sub.active = False
        sub.canceled_at = now

        ctx = AuditContext(
            actor_key_prefix=None,
            actor_permissions=("customer_self_serve",),
            actor_label=f"pilot_refund:user:{user.id}",
        )
        audit_repo = AdminAuditRepository(self.db)
        audit_repo.record(
            ctx=ctx,
            tenant_id=sub.tenant_id,
            action=ACTION_SUBSCRIPTION_PILOT_REFUNDED,
            resource_type=RESOURCE_SUBSCRIPTION,
            resource_pk=sub.id,
            resource_natural_id=sub.stripe_subscription_id,
            before={"status": before_status, "active": True},
            after={
                "status": STATUS_CANCELED,
                "active": False,
                "stripe_refund_id": refund_id,
                "intro_charge_id": charge_id,
                "refunded_amount_cents": self._PILOT_REFUND_AMOUNT_CENTS,
                "currency": self._PILOT_REFUND_CURRENCY,
            },
            note="self-serve pilot refund -> refund + cancel + tenant cascade",
        )

        # Cascade-deactivate the tenant inside the same transaction.
        # autocommit=False so the audit row, the subscription mutation,
        # and every cascade row land atomically.
        try:
            from app.repositories.agent_repository import AgentRepository
            from app.services.admin_service import AdminService
            from app.services.luciel_instance_service import LucielInstanceService

            admin = AdminService(self.db)
            agent_repo = AgentRepository(self.db)
            luciel_service = LucielInstanceService(self.db, admin_service=admin)
            admin.deactivate_tenant_with_cascade(
                sub.tenant_id,
                audit_ctx=ctx,
                luciel_instance_service=luciel_service,
                agent_repo=agent_repo,
                updated_by=f"pilot_refund:user:{user.id}",
                autocommit=False,
            )
        except Exception:
            self.db.rollback()
            logger.exception(
                "billing: pilot-refund cascade failed tenant=%s sub=%s",
                sub.tenant_id, sub.stripe_subscription_id,
            )
            raise

        self.db.commit()

        logger.info(
            "billing: pilot-refund completed user_id=%s sub=%s tenant=%s "
            "refund=%s charge=%s amount_cents=%s",
            user.id, sub.id, sub.tenant_id, refund_id, charge_id,
            self._PILOT_REFUND_AMOUNT_CENTS,
        )
        return {
            "refund_id": refund_id,
            "charge_id": charge_id,
            "refunded_amount_cents": self._PILOT_REFUND_AMOUNT_CENTS,
            "currency": self._PILOT_REFUND_CURRENCY,
            "tenant_id": sub.tenant_id,
            "deactivated_at": now,
        }
