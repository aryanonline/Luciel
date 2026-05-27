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
    ACTION_PILOT_REFUND_EMAIL_SEND_FAILED,
    ACTION_SUBSCRIPTION_DOWNGRADE_SCHEDULED,
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
    TIER_ENTERPRISE,
    TIER_FREE,
    TIER_INSTANCE_CAPS,
    TIER_PRO,
)
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)
from app.services.email_service import (
    RefundEmailError,
    send_pilot_refund_email,
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
    # V2 Stripe price-id ENV-var keys (Arc 6 Commit 4 mint, locked in
    # CANONICAL_RECAP §11.7 / §14). Each value is the *attribute name* on
    # ``settings`` that holds the Stripe Price ID for that (tier, cadence)
    # pair. ``resolve_price_id`` reads via ``getattr(settings, key)`` and
    # raises ``BillingNotConfiguredError`` if the slot is empty.
    #
    # Tier topology (CANONICAL §11.7, revised Arc 7 Commit 1):
    #   * Free       — NO Stripe Price (CAPTCHA-gated signup, no Stripe
    #     row at all). Free is NOT a valid key here — callers that ask
    #     for ("free", *) get BillingNotConfiguredError, which the route
    #     layer translates to 501. The free signup path is the dedicated
    #     POST /api/v1/billing/signup-free route (Arc 6 Commit 8), NOT
    #     this checkout machinery.
    #   * Pro        — flat-rate self-serve, monthly + annual cadences.
    #   * Enterprise — flat-rate self-serve, monthly + annual cadences
    #     SYMMETRIC WITH PRO since Arc 7 Commit 1 retired the hybrid/
    #     metered-overage shape (partner doctrine pivot 2026-05-24:
    #     "Since we have abuse limits for each tier I don't think we
    #     need to include the metering option for enterprise"). The
    #     prior `stripe_price_enterprise_floor_annual` slot is RETIRED;
    #     the new slots are `stripe_price_enterprise_monthly` ($2,800
    #     CAD/mo) + `stripe_price_enterprise_annual` ($24,000 CAD/yr,
    #     28.6% annual discount matching Pro's ratio). The (enterprise,
    #     monthly) 400 reject at app/api/v1/billing.py is REMOVED in the
    #     same commit. Rate-limit ceilings (api_rate_limit_rpm,
    #     Arc 7 Commit 4 tier-aware middleware) + instance_count_cap +
    #     embed_key_count_cap are now the entitlement gates that
    #     separate Pro from Enterprise — see app/policy/entitlements.py
    #     TIER_* rows. ``leads_per_month_cap`` was retired entirely at
    #     Arc 7 Commit 5 (2026-05-24); rate-limit is the abuse boundary
    #     and a monthly count cap on a flat-recurring customer punishes
    #     success without protecting any surface RPM does not already.
    #
    # If a future tier is added: add a row here and a parallel
    # ``stripe_price_*`` field on ``Settings``. No code change in
    # ``resolve_price_id`` is required — it is data-driven.
    (TIER_PRO,        BILLING_CADENCE_MONTHLY): "stripe_price_pro_monthly",
    (TIER_PRO,        BILLING_CADENCE_ANNUAL):  "stripe_price_pro_annual",
    (TIER_ENTERPRISE, BILLING_CADENCE_MONTHLY): "stripe_price_enterprise_monthly",
    (TIER_ENTERPRISE, BILLING_CADENCE_ANNUAL):  "stripe_price_enterprise_annual",
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

        Arc 6 Commit 5 (Path A): the V1 ``stripe_price_individual`` boot-
        check is fully removed; each (tier, cadence) pair is validated
        *lazily* in ``resolve_price_id`` when the checkout route picks
        one against the V2 ``PRICE_ID_KEY`` table above. The
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
        """Per-tier cap for the ``subscriptions.instance_count_cap`` column.

        Tier strings outside ``ALLOWED_TIERS`` fall back to the Free cap
        (1) defensively — the schema validator should have caught that
        already. Free is the most-restrictive tier so an unknown string
        cannot accidentally over-provision capacity.
        """
        # V2 caps (CANONICAL §14, model app/models/subscription.py):
        # Free=1, Pro=10, Enterprise=None (unlimited). Enterprise's None
        # return is documented at the model layer; the caller is
        # responsible for handling None as "no cap".
        return TIER_INSTANCE_CAPS.get(tier, TIER_INSTANCE_CAPS[TIER_FREE])

    # -----------------------------------------------------------------
    # Checkout
    # -----------------------------------------------------------------

    def create_checkout(
        self,
        *,
        email: str,
        display_name: str,
        tier: str = TIER_PRO,
        billing_cadence: str = BILLING_CADENCE_MONTHLY,
    ) -> dict[str, str]:
        """Create a Stripe Checkout session and return the redirect URL + id.

        Arc 6 Commit 5 (Path A) note: the V1 vocabulary (individual/team/
        company tiers, Tenant noun) is fully retired from this route's
        metadata stamps. The accepted (tier, cadence) pairs are exactly
        the keys of ``PRICE_ID_KEY`` above; ``resolve_price_id`` is the
        single source of truth.

        We pass four pieces of metadata into Stripe so the webhook
        handler can correlate without an extra Stripe API call:

          - ``luciel_email``            — the email the buyer entered.
          - ``luciel_display_name``     — carried onto AdminConfig.display_name.
          - ``luciel_tier``             — 'pro' | 'enterprise' (V2 vocab; Free
                                          never reaches this route, it has
                                          its own /signup-free path).
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
    # Upgrade  (Arc 6 / Commit 8.5a)
    # -----------------------------------------------------------------

    def create_upgrade_checkout(
        self,
        *,
        admin_id: str,
        email: str,
        display_name: str,
        target_tier: str,
        billing_cadence: str = BILLING_CADENCE_MONTHLY,
    ) -> dict[str, str]:
        """Create a Stripe Checkout session for a tier UPGRADE.

        Distinct from ``create_checkout`` (the prospective-buyer path)
        in exactly one way: the Stripe metadata stamps
        ``luciel_admin_id`` so the webhook routes into the upgrade-branch
        instead of minting a new Admin. The Stripe-Hosted Checkout UI is
        otherwise identical from the buyer's perspective.

        Why we still go through Stripe-hosted Checkout (not
        ``subscription.create`` API):
          1. One webhook code path -- the same
             ``checkout.session.completed`` handler covers both new
             signups and upgrades.
          2. Stripe-managed card collection (PCI scope stays inside
             Stripe; we never see a PAN).
          3. Proration is handled by Stripe out of the box; we do not
             need to compute a credit-to-issue against an existing sub
             because the buyer has no existing sub yet (Free has no
             Stripe row; this is the first paid subscription on the
             Admin).

        Intro-fee gate:
          The $100 / 90-day pilot is intentionally NOT applied here.
          ``is_first_time_customer`` is keyed on email, and a Free
          admin who later upgrades to Pro counts as a *new* paying
          customer in Stripe's books (no prior Charge). However, the
          pilot was scoped in CANONICAL_RECAP §14 as a marketing-funnel
          inducement ("sign up direct to Pro"). Granting it to Free
          upgraders would create an arbitrage path where every Pro
          buyer signs up Free first to claim the pilot rate. The
          upgrade route therefore passes ``intro_fee_price_id=None``
          and ``trial_period_days=None``, charging the full recurring
          rate at the next billing cycle. Tracked as a deliberate
          design choice; not a drift.

        Raises ``ValueError`` on:
          * unsupported (tier, cadence) pair
          * missing or empty admin_id / email / display_name
        """
        self.require_configured()

        if not admin_id:
            raise ValueError("admin_id is required for upgrade checkout.")
        if not email:
            raise ValueError("email is required for upgrade checkout.")

        if target_tier not in ALLOWED_TIERS:
            raise ValueError(
                f"Unsupported tier {target_tier!r}. Allowed: {ALLOWED_TIERS}."
            )
        if target_tier == TIER_FREE:
            # Free has no Stripe row by design; the route layer should
            # have 400'd before reaching the service.
            raise ValueError(
                "create_upgrade_checkout: target_tier='free' is not a "
                "valid upgrade target. Free has no Stripe subscription."
            )
        if billing_cadence not in ALLOWED_BILLING_CADENCES:
            raise ValueError(
                f"Unsupported billing_cadence {billing_cadence!r}. "
                f"Allowed: {ALLOWED_BILLING_CADENCES}."
            )

        price_id = self.resolve_price_id(
            tier=target_tier, cadence=billing_cadence,
        )

        success_url = settings.billing_success_url
        cancel_url = settings.billing_cancel_url

        # ``luciel_admin_id`` is the upgrade-branch marker the webhook
        # detects. ``luciel_intro_applied=false`` is stamped explicitly
        # so a forensic engineer querying Stripe by metadata can tell
        # an upgrade-Pro from a marketing-Pro at a glance.
        metadata = {
            "luciel_email": email,
            "luciel_display_name": display_name,
            "luciel_tier": target_tier,
            "luciel_billing_cadence": billing_cadence,
            "luciel_admin_id": admin_id,
            "luciel_intro_applied": "false",
            "luciel_flow": "upgrade",
        }

        session = self.stripe.create_checkout_session(
            customer_email=email,
            price_id=price_id,
            success_url=success_url,
            cancel_url=cancel_url,
            trial_period_days=None,
            metadata=metadata,
            intro_fee_price_id=None,
        )

        logger.info(
            "billing: UPGRADE checkout session created stripe_id=%s "
            "admin_id=%s email=%s target_tier=%s cadence=%s",
            getattr(session, "id", "?"),
            admin_id,
            email,
            target_tier,
            billing_cadence,
        )
        return {
            "checkout_url": session.url,
            "session_id": session.id,
        }

    # -----------------------------------------------------------------
    # Downgrade scheduling — Arc 6 Commit 8.5b.
    # -----------------------------------------------------------------

    def schedule_downgrade(
        self,
        *,
        admin_id: str,
        target_tier: str,
        audit_ctx: AuditContext,
    ) -> dict[str, Any]:
        """Arm a deferred tier downgrade for ``admin_id``.

        Two-sided write within one committed transaction:
          1. Stripe-side: ``stripe.Subscription.modify(
             cancel_at_period_end=True)`` on the admin's active sub.
          2. Local-side: set
             ``subscriptions.pending_downgrade_target = target_tier``
             on the same row + record an
             ``ACTION_SUBSCRIPTION_DOWNGRADE_SCHEDULED`` audit row.

        The actual tier-flip + overflow archive does NOT run here. It
        runs in the webhook V2 branch of ``_on_subscription_deleted``
        when Stripe fires the boundary event at
        ``current_period_end``. This is the lock established in
        CANONICAL_RECAP §17 Commit 8.5b:
          * Timing = deferred (cancel_at_period_end)
          * Buyer keeps current-tier entitlements until the boundary
          * Reversible: a buyer can re-upgrade before the boundary and
            this method's effects are undone by the upgrade path
            (which calls stripe.Subscription.modify(
            cancel_at_period_end=False) + nulls out
            pending_downgrade_target -- wired in Commit 8.5b's upgrade
            integration, separate slice).

        Ent→Pro path (cancel-and-email-rebuy lock):
          For Enterprise admins downgrading to Pro, ``target_tier`` is
          ``'pro'`` here but the webhook V2 branch will detect that
          the post-cancel admin needs to be re-shopped (Pro is a paid
          tier, not a free fall-back) and fire the transactional
          ``/signup?tier=pro`` magic-link email. That branching lives
          downstream in the webhook -- this method only stores the
          target.

        Idempotency:
          A second call with the same target_tier on an admin whose
          sub already has pending_downgrade_target set is a noop on
          both sides (Stripe accepts a redundant modify; the local
          column UPDATE is a no-op self-write). A different target
          (Ent→Free called after an existing Ent→Pro schedule, or
          vice versa) overwrites the column and emits a fresh audit
          row so the lifecycle stays inspectable -- the schema CHECK
          on the column is the only legality gate.

        Raises:
          ValueError    -- unknown target_tier, or admin has no active
                           subscription (Free admins cannot downgrade --
                           they're already at the bottom).
          BillingNotConfiguredError -- Stripe not wired (env var
                                       missing). Same gate as checkout.
        """
        self.require_configured()

        if not admin_id:
            raise ValueError("admin_id is required for schedule_downgrade.")
        if target_tier not in (TIER_FREE, TIER_PRO):
            # Enterprise is never a downgrade target. Same three-layer
            # gate as tier_provisioning_service.downgrade_admin_tier:
            # route validates, service validates here, schema CHECK
            # validates at the DB. Three layers of the same invariant
            # because mis-routing into this method with target='enterprise'
            # would mean a logic bug we want to surface early.
            raise ValueError(
                f"schedule_downgrade: target_tier={target_tier!r} is not a "
                f"legal downgrade destination. Expected one of "
                f"{{{TIER_FREE!r}, {TIER_PRO!r}}}."
            )

        sub = self.get_active_subscription_for_tenant(admin_id=admin_id)
        if sub is None:
            # Free admins have no Subscription row by design. A
            # downgrade request from a Free admin would be a route-
            # layer bug; we 4xx here defensively.
            raise ValueError(
                f"schedule_downgrade: admin {admin_id!r} has no active "
                f"subscription; downgrade is not applicable."
            )

        old_tier = sub.tier
        old_pending = sub.pending_downgrade_target

        # Stripe-side: schedule cancellation at the current period end.
        # The local UPDATE rides the same transaction as the audit row
        # so a failure on either rolls back the other (Invariant 4).
        # Stripe's call is on the external side and cannot be in the
        # same DB transaction, but its idempotency is its own:
        # cancel_at_period_end is a state, not an event -- a redundant
        # modify is a no-op.
        stripe_sub = self.stripe.schedule_cancellation_at_period_end(
            stripe_subscription_id=sub.stripe_subscription_id,
        )

        # Mirror Stripe's view back into our local row.
        sub.pending_downgrade_target = target_tier
        sub.cancel_at_period_end = True
        # If Stripe reported a refreshed period_end, mirror it (the
        # frontend modal will surface this date as "effective on …").
        period_end_raw = getattr(stripe_sub, "current_period_end", None)
        if period_end_raw is not None:
            sub.current_period_end = datetime.fromtimestamp(
                int(period_end_raw), tz=timezone.utc,
            )

        # Audit row with the boundary timestamp in the after_json so a
        # forensic engineer can answer "when was this downgrade armed,
        # and what date did the buyer see?" from the audit row alone.
        AdminAuditRepository(self.db).record(
            ctx=audit_ctx,
            admin_id=admin_id,
            action=ACTION_SUBSCRIPTION_DOWNGRADE_SCHEDULED,
            resource_type=RESOURCE_SUBSCRIPTION,
            resource_natural_id=sub.stripe_subscription_id,
            before={
                "tier": old_tier,
                "pending_downgrade_target": old_pending,
                "cancel_at_period_end": False,
            },
            after={
                "tier": old_tier,  # unchanged until boundary fires
                "pending_downgrade_target": target_tier,
                "cancel_at_period_end": True,
                "effective_at": (
                    sub.current_period_end.isoformat()
                    if sub.current_period_end else None
                ),
            },
            note=(
                f"Tier downgrade scheduled {old_tier} -> {target_tier} "
                f"at period_end"
            ),
            autocommit=False,
        )
        self.db.commit()

        logger.info(
            "billing: downgrade scheduled admin=%s sub=%s old_tier=%s "
            "target_tier=%s effective_at=%s",
            admin_id, sub.stripe_subscription_id, old_tier, target_tier,
            sub.current_period_end.isoformat() if sub.current_period_end else None,
        )
        return {
            "admin_id": admin_id,
            "old_tier": old_tier,
            "target_tier": target_tier,
            "effective_at": (
                sub.current_period_end.isoformat()
                if sub.current_period_end else None
            ),
            "stripe_subscription_id": sub.stripe_subscription_id,
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

    def get_active_subscription_for_tenant(self, *, admin_id: str) -> Subscription | None:
        stmt = (
            select(Subscription)
            .where(Subscription.admin_id == admin_id, Subscription.active.is_(True))
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
        return self.db.execute(stmt).scalars().first()

    # -----------------------------------------------------------------
    # Arc 10 -- closure / reactivation helpers.
    # -----------------------------------------------------------------
    # These three methods are called by ClosureService and
    # ReactivationService respectively. They are thin facades over the
    # existing Stripe primitives (cancel_subscription,
    # schedule_cancellation_at_period_end, create_checkout_session,
    # retrieve_checkout_session) so the closure / reactivation
    # services do not couple directly to the Stripe SDK.

    def cancel_for_closure(
        self,
        *,
        admin_id: str,
        cancel_mode: str,
        audit_ctx,
    ) -> bool:
        """Cancel the admin's Stripe subscription as part of closure.

        cancel_mode:
          'immediate'  -- cancel now, no proration (StripeClient.
                          cancel_subscription).
          'period_end' -- cancel at current_period_end (StripeClient.
                          schedule_cancellation_at_period_end).

        Returns True iff a Stripe modification was actually issued.
        False on Free admins (no subscription) or admins whose
        subscription is already cancelled.

        Failure modes:
          * No subscription found -> return False (Free admin).
          * Subscription already cancelled (status='canceled') ->
            return False (idempotent against re-closure).
          * Stripe API exception -> let it bubble; the caller
            (ClosureService.initiate_closure) catches and continues
            with the local closure regardless.

        We do NOT mirror the Stripe state into the local
        subscriptions row here -- the webhook
        ``_on_subscription_deleted`` is the canonical writer for
        the local cancel state. This keeps the source-of-truth
        contract clean: Stripe owns billing state, our DB mirrors it
        on webhook receipt.
        """
        sub = self.get_active_subscription_for_tenant(admin_id=admin_id)
        if sub is None or not sub.stripe_subscription_id:
            return False
        if sub.status == "canceled":
            # Idempotent against re-closure: Stripe has already
            # cancelled this sub via some prior path (manual
            # Dashboard cancel, prior closure attempt).
            return False

        if cancel_mode == "immediate":
            self.stripe.cancel_subscription(
                stripe_subscription_id=sub.stripe_subscription_id,
            )
        elif cancel_mode == "period_end":
            self.stripe.schedule_cancellation_at_period_end(
                stripe_subscription_id=sub.stripe_subscription_id,
            )
        else:
            # ClosureService.initiate_closure validates cancel_mode
            # before this method is called; reaching here means a
            # programmer error, not a user error. Loud failure is
            # the right posture.
            raise ValueError(
                f"BillingService.cancel_for_closure: cancel_mode "
                f"{cancel_mode!r} is not legal."
            )
        return True

    def create_reactivation_checkout(
        self,
        *,
        admin_id: str,
        target_tier: str,
        success_url: str,
        cancel_url: str,
    ):
        """Create a fresh Stripe Checkout session for reactivation.

        Per Vision §6.4: reactivation requires resubscribe -- a fresh
        Stripe checkout, not a resurrection of the old subscription.
        The previous subscription row stays in the DB as historical
        record; the new checkout will create a new Subscription row
        when the webhook fires.

        Looks up the existing admin to source customer_email (so the
        buyer does not re-type it) and uses metadata to carry admin_id
        so the complete_reactivation phase can verify the session
        belongs to the right admin.
        """
        # Look up the admin to get customer_email. We import Admin
        # here rather than at module top so this method composes
        # cleanly even if a future cycle changes the module layout.
        from app.models.admin import Admin
        admin = self.db.get(Admin, admin_id)
        if admin is None:
            raise ValueError(
                f"BillingService.create_reactivation_checkout: "
                f"admin {admin_id!r} not found."
            )
        # The admin's name has been redacted to '[REDACTED]' only
        # post-hard-delete; pre-hard-delete it's still the real name.
        # We don't need customer_email here strictly -- Stripe will
        # collect one on the checkout page -- but we pass it through
        # if the admin row carries it via a related user record. For
        # the v1 reactivation flow, we let Stripe collect.
        price_id = self.resolve_price_id(tier=target_tier, cadence="monthly")
        session = self.stripe.create_checkout_session(
            customer_email="",  # let Stripe collect
            price_id=price_id,
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "admin_id": admin_id,
                "purpose": "arc10_reactivation",
                "target_tier": target_tier,
            },
            idempotency_key=f"reactivate:{admin_id}",
        )
        return session

    def retrieve_checkout_session(self, *, session_id: str):
        """Look up a Stripe checkout session by id.

        Thin facade around StripeClient.retrieve_checkout_session so
        ReactivationService.complete_reactivation does not import
        StripeClient directly.
        """
        return self.stripe.retrieve_checkout_session(session_id)

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
            admin_id=sub.admin_id,
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
            from app.services.instance_service import InstanceService

            admin = AdminService(self.db)
            agent_repo = AgentRepository(self.db)
            luciel_service = InstanceService(self.db, admin_service=admin)
            admin.deactivate_tenant_with_cascade(
                sub.admin_id,
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
                sub.admin_id, sub.stripe_subscription_id,
            )
            raise

        self.db.commit()

        # Step 30a.2-pilot Commit 3j -- best-effort courtesy email to the
        # buyer. The refund, cancel, and cascade have ALREADY been committed
        # above; this email is the third confirmation leg (the first two
        # being the on-page success surface and Stripe's optional account-
        # level refund receipt). A SES failure here MUST NOT roll back the
        # cascade -- the customer has been made whole financially and the
        # audit row at action=ACTION_SUBSCRIPTION_PILOT_REFUNDED is the
        # single source of truth for the refund. On SES failure we log
        # loudly, write a follow-up audit row with
        # action=ACTION_PILOT_REFUND_EMAIL_SEND_FAILED so an operator can
        # manually retry, and swallow the exception so the route layer
        # returns the locked success payload unchanged. This mirrors the
        # swallow-and-audit posture the webhook handlers use against Stripe.
        try:
            send_pilot_refund_email(
                to_email=sub.customer_email,
                refund_id=refund_id,
                amount_cents=self._PILOT_REFUND_AMOUNT_CENTS,
                currency=self._PILOT_REFUND_CURRENCY,
                display_name=getattr(user, "display_name", None),
            )
        except RefundEmailError as email_exc:
            logger.warning(
                "billing: pilot-refund email send FAILED user_id=%s sub=%s "
                "refund=%s to_email=%s error=%s -- refund cascade already "
                "committed; writing follow-up audit row",
                user.id, sub.id, refund_id, sub.customer_email, email_exc,
            )
            try:
                followup_repo = AdminAuditRepository(self.db)
                followup_repo.record(
                    ctx=ctx,
                    admin_id=sub.admin_id,
                    action=ACTION_PILOT_REFUND_EMAIL_SEND_FAILED,
                    resource_type=RESOURCE_SUBSCRIPTION,
                    resource_pk=sub.id,
                    resource_natural_id=sub.stripe_subscription_id,
                    before=None,
                    after={
                        "stripe_refund_id": refund_id,
                        "error_class": type(email_exc).__name__,
                        "error_message_truncated": str(email_exc)[:200],
                        "to_email": sub.customer_email,
                    },
                    note=(
                        "pilot-refund cascade committed successfully; "
                        "courtesy email to customer FAILED -- operator "
                        "must manually relay refund confirmation"
                    ),
                )
                self.db.commit()
            except Exception:  # pragma: no cover - defensive
                logger.exception(
                    "billing: pilot-refund email-failure audit row write also "
                    "FAILED user_id=%s sub=%s refund=%s -- the financial "
                    "refund still succeeded; check CloudWatch for the "
                    "[pilot-refund-email] log line for manual relay",
                    user.id, sub.id, refund_id,
                )
                # Do NOT re-raise: the financial refund succeeded, the
                # route layer must still return the success payload.
        except Exception:  # pragma: no cover - any non-Refund exception
            # An unexpected exception from the email helper (e.g. import
            # error, programmer error). Log and swallow; the refund cascade
            # already committed and must not be rolled back.
            logger.exception(
                "billing: pilot-refund email send raised UNEXPECTED "
                "exception user_id=%s sub=%s refund=%s -- swallowed; "
                "refund cascade already committed",
                user.id, sub.id, refund_id,
            )

        logger.info(
            "billing: pilot-refund completed user_id=%s sub=%s tenant=%s "
            "refund=%s charge=%s amount_cents=%s",
            user.id, sub.id, sub.admin_id, refund_id, charge_id,
            self._PILOT_REFUND_AMOUNT_CENTS,
        )
        return {
            "refund_id": refund_id,
            "charge_id": charge_id,
            "refunded_amount_cents": self._PILOT_REFUND_AMOUNT_CENTS,
            "currency": self._PILOT_REFUND_CURRENCY,
            "admin_id": sub.admin_id,
            "deactivated_at": now,
        }
