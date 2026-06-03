"""Step 30a: BillingWebhookService.

The single place that mutates ``subscriptions`` rows. Called from the
``POST /api/v1/billing/webhook`` route after Stripe-signature
verification has succeeded; this service:

  1. Resolves the event type to a handler.
  2. Dedupes on ``stripe_event_id`` (every successful apply records
     ``Subscription.last_event_id``; a redelivered event whose id
     matches is recorded as ACTION_BILLING_WEBHOOK_REPLAY_REJECTED
     and otherwise no-ops -- the audit chain still sees the replay).
  3. Performs the mutation + writes the matching audit row in the
     SAME DB transaction (Invariant 4 -- audit-before-commit).
  4. Fails closed on unknown event types: returns a marker so the
     route can record an audit row and a 200 (Stripe documents that
     non-2xx replies trigger redelivery, so we MUST 200 to break
     the loop, while still capturing the unknown event in audit).

Event types handled at v1:

  * checkout.session.completed
      -- atomically:
         - resolve or create a ``User`` row from the buyer's email
         - call ``OnboardingService.onboard_tenant`` (external API name
           retained from Arc 5; the noun on our side is now "admin")
           to mint a fresh admin (one admin per Pro subscriber at V2)
         - INSERT a Subscription row
         - mint a magic-link JWT and send the email
  * customer.subscription.updated
      -- ``status``, cycle dates, ``cancel_at_period_end`` flip.
  * customer.subscription.deleted
      -- cancel: flip ``active=False`` on the Subscription, then
         call ``AdminService.deactivate_tenant_with_cascade`` (external
         API name retained from Arc 5) so the admin's children
         deactivate as documented in ARCHITECTURE §4.5.
  * invoice.payment_failed
      -- update status to whatever Stripe says (typically 'past_due'
         or 'unpaid'); we do NOT cancel on first failure -- Stripe
         drives the dunning cadence.

Non-goals at v1 (deliberate; tracked in DRIFTS):
  - invoice.paid / invoice.finalized -- we do not double-book invoices.
  - customer.* events -- the Subscription row carries everything we need.
  - charge.refunded -- the portal exposes refunds; our audit chain
    sees the resulting subscription.updated.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.admin_audit_log import (
    ACTION_BILLING_WEBHOOK_REPLAY_REJECTED,
    ACTION_SUBSCRIPTION_CANCEL,
    ACTION_SUBSCRIPTION_CREATE,
    ACTION_SUBSCRIPTION_DOWNGRADE_APPLIED,
    ACTION_SUBSCRIPTION_UPDATE,
    RESOURCE_SUBSCRIPTION,
)
from app.models.subscription import (
    ALLOWED_BILLING_CADENCES,
    ALLOWED_TIERS,
    BILLING_CADENCE_MONTHLY,
    STATUS_CANCELED,
    Subscription,
    TIER_INSTANCE_CAPS,
    TIER_PRO,
)
from app.models.user import User
from app.repositories.admin_audit_repository import AdminAuditRepository, AuditContext
# Step 30a.3: signup welcome-email mechanic (Option B). The webhook no
# longer mints a one-shot magic-link cookie post-checkout; instead it
# mints a ``set_password`` token and emails the buyer a link to
# ``/auth/set-password``. The buyer must redeem that link before any
# cookied /app session exists for them -- that is the load-bearing
# enforcement of "password mandatory at signup".
from app.integrations.stripe import StripeClient, get_stripe_client
from app.services.email_service import send_welcome_set_password_email
from app.services.magic_link_service import (
    build_set_password_url,
    mint_set_password_token,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Upgrade-branch fall-through signal (Arc 6 / Commit 8.5a)
# ---------------------------------------------------------------------

class _UpgradeBranchFallthrough(Exception):
    """Raised by ``_on_checkout_completed_upgrade`` when the upgrade
    preconditions fail (admin missing / inactive / owner mismatch).

    The main ``_on_checkout_completed`` traps this and falls back to
    the default mint path so the paid checkout still produces a
    working Admin (defensive against a hand-crafted Stripe event with
    a stale ``luciel_admin_id`` -- the buyer must not be left without
    an Admin to log into).
    """
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------
# Admin id minting (Arc 6 Commit 6 — webhook-noun rename Tenant→Admin).
# The DB column ``subscriptions.admin_id`` is deliberately retained
# physically per Arc 5 Path A; this file's local vocab now matches the
# Arc 5 B8 Admin rename, but the column attribute access
# ``Subscription.admin_id`` and the external service APIs
# ``OnboardingService.onboard_tenant`` /
# ``AdminService.deactivate_tenant_with_cascade`` keep their Arc-5 names.
# ---------------------------------------------------------------------

# Tier-aware admin-id prefix (V2 — Arc 5 B8 rename). The prefix tags
# self-serve admins by tier at a glance so a grep / DB query against
# ``admins`` separates Pro-self-serve from Enterprise-self-serve
# without joining ``subscriptions``. Sales-assisted admins (created
# outside the webhook) carry no tier prefix. Free admins lazy-mint via
# the signup flow, not the webhook, and use the ``free`` prefix.
_TIER_PREFIX = {
    "free":       "free",
    "pro":        "pro",
    "enterprise": "ent",
}


def _mint_admin_id_from_email(email: str, tier: str = TIER_PRO) -> str:
    """Generate a URL-safe, collision-resistant admin slug from an email.

    Shape: ``<tier-prefix>-<8 hex chars>``. The prefix is one of
    ``free`` / ``pro`` / ``ent`` per ``_TIER_PREFIX`` (V2 — Arc 5 B8
    Admin rename + Arc 6 SKU restructure). The 8 hex chars give 32 bits
    of randomness — collision probability is negligible at the expected
    scale of self-serve subscribers, and a collision is caught by the
    existing ``admins.id`` unique constraint.

    Default fallback for an unknown tier is ``pro`` (matches the
    function's default arg ``tier=TIER_PRO``) — V2 dropped the V1 ``ind``
    fallback at Arc 6 Commit 5 because ``individual`` is no longer a
    valid tier string anywhere in the system.
    """
    prefix = _TIER_PREFIX.get(tier, "pro")
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _ts(epoch: int | None) -> datetime | None:
    """Stripe timestamps are unix seconds; convert to aware UTC."""
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _webhook_audit_ctx() -> AuditContext:
    """Synthetic audit context for the webhook path.

    actor_key_prefix is None (no API key was used), permissions are
    ``('system',)``, and the label distinguishes this from other system
    callers (retention, onboarding direct).
    """
    return AuditContext.system(label="stripe_webhook")


# ---------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------

class BillingWebhookService:
    """Apply Stripe webhook events to the subscriptions table.

    The route layer is responsible for:
      - Verifying the Stripe signature (raises before we are called).
      - Reading the raw event dict and passing it to ``handle(event)``.
      - Returning HTTP 200 on every non-signature outcome so Stripe
        does not redeliver indefinitely.
    """

    def __init__(
        self,
        db: Session,
        stripe_client: StripeClient | None = None,
        budget_meter=None,
    ) -> None:
        self.db = db
        # Arc 18 — the invoice.paid handler reads the per-instance
        # conversation counter to compute overage at cycle close. Injectable
        # (tests pass a BudgetMeter over an InMemoryBackend); lazily built
        # from settings.redis_url on first use in production.
        self._budget_meter = budget_meter
        # Arc 2 (2026-05-20) -- D-billing-webhook-service-stripe-attribute-error-2026-05-18:
        # `_on_checkout_completed` calls `self.stripe.retrieve_subscription(...)`
        # to read the canonical Subscription object (status, period dates,
        # trial_end) when the checkout.session inline data is insufficient.
        # Prior to this fix, `__init__` set only `self.db`, so the first
        # `self.stripe` read raised AttributeError; the surrounding
        # try/except caught it and the fallback path silently became the
        # de-facto primary on every checkout. We now ALWAYS bind
        # `self.stripe` (either the injected client for tests, or the
        # process-singleton from `get_stripe_client()` for live traffic).
        # The retrieve-subscription call site at L330 still wraps its read
        # in try/except so genuine Stripe-unreachable conditions degrade
        # to inline data + a subsequent `customer.subscription.updated`
        # backfill, exactly as the original design intended.
        self.stripe: StripeClient = (
            stripe_client if stripe_client is not None else get_stripe_client()
        )

    # -----------------------------------------------------------------
    # Top-level dispatch
    # -----------------------------------------------------------------

    def handle(self, event: dict) -> dict:
        """Return a small dict the route can echo into its response.

        The dict shape is for observability only -- Stripe ignores the
        body of our 200 response.
        """
        event_id = event.get("id", "<no-id>")
        event_type = event.get("type", "<no-type>")
        data_object = (event.get("data") or {}).get("object") or {}

        logger.info("billing-webhook: received event id=%s type=%s", event_id, event_type)

        handler = {
            "checkout.session.completed": self._on_checkout_completed,
            "customer.subscription.updated": self._on_subscription_updated,
            "customer.subscription.deleted": self._on_subscription_deleted,
            "invoice.payment_failed": self._on_invoice_payment_failed,
            # Arc 18 (§3.4.1b): cycle close. GUARDRAIL — this handler ONLY
            # (a) reports conversation-overage usage records and (b) resets
            # the per-instance budget counter + advances the period anchor.
            # It does NOT touch base-invoice handling and does NOT book the
            # base subscription invoice (Arc 7 retired base metering; the
            # base invoice is already settled by Stripe). See
            # ARC18_BACKEND_REPORT.md "supersedes" note.
            "invoice.paid": self._on_invoice_paid,
            # Stripe emits subscription cycle renewal via invoice.paid; some
            # accounts also surface customer.subscription.renewed. Route both
            # to the same idempotent close so neither double-books.
            "customer.subscription.renewed": self._on_invoice_paid,
        }.get(event_type)

        if handler is None:
            # Unknown event type -- fail closed in the audit-trail sense
            # (we record it) but return 200 so Stripe stops redelivering.
            self._record_unknown_event(event_id=event_id, event_type=event_type)
            return {"applied": False, "reason": "unknown_event_type", "event_type": event_type}

        return handler(event_id=event_id, data_object=data_object, event=event)

    # -----------------------------------------------------------------
    # checkout.session.completed
    # -----------------------------------------------------------------

    def _on_checkout_completed(self, *, event_id: str, data_object: dict, event: dict) -> dict:
        """Mint admin + user + subscription atomically.

        Idempotency:
          If the Stripe subscription id already has a row in
          ``subscriptions``, this is a redelivery; record the replay
          and return without mutating.
        """
        stripe_subscription_id = data_object.get("subscription")
        stripe_customer_id = data_object.get("customer")

        if not stripe_subscription_id or not stripe_customer_id:
            logger.warning(
                "billing-webhook: checkout.session.completed missing ids event=%s",
                event_id,
            )
            return {"applied": False, "reason": "missing_ids"}

        # Idempotency check
        existing = self.db.execute(
            select(Subscription).where(
                Subscription.stripe_subscription_id == stripe_subscription_id
            )
        ).scalars().first()
        if existing is not None:
            # ``existing.admin_id`` is the DB column attribute (Arc 5
            # Path A kept it physically). Locally we treat the value as
            # the admin id; the dict key remains ``admin_id`` for
            # observability symmetry with the column.
            self._record_replay(
                event_id=event_id,
                admin_id=existing.admin_id,
                stripe_subscription_id=stripe_subscription_id,
            )
            return {"applied": False, "reason": "replay", "admin_id": existing.admin_id}

        # Extract identity from metadata
        metadata = data_object.get("metadata") or {}
        # ``customer_details.email`` is Stripe's normalized form;
        # ``metadata.luciel_email`` is the original we captured at
        # checkout creation. Prefer the metadata copy so a Stripe-side
        # email canonicalization does not desynchronize us.
        email = (
            metadata.get("luciel_email")
            or (data_object.get("customer_details") or {}).get("email")
            or data_object.get("customer_email")
        )
        display_name = (
            metadata.get("luciel_display_name")
            or (data_object.get("customer_details") or {}).get("name")
            or (email or "Unknown")
        )
        # V2 default: any checkout that lands without explicit tier metadata
        # is treated as Pro (the only flat-monthly self-serve V2 tier).
        # Free has no Subscription row at all (Gap 1 lock); Enterprise
        # is contract-signed, not webhook-minted.
        tier = metadata.get("luciel_tier") or TIER_PRO
        if tier not in ALLOWED_TIERS:
            # Defensive — schema-validated upstream, but a hand-crafted
            # Stripe event could arrive with garbage. Fall back to the
            # safest tier (Pro — Arc 6 Commit 5 update; was Individual
            # at V1) and log loudly; do NOT raise here because Stripe
            # must get a 200 to stop redelivering. Pro is the safest
            # paid-tier fallback because (a) it does not over-provision
            # like Enterprise would (Enterprise has unlimited Instance
            # cap), and (b) it does not under-provision like Free would
            # (Free is a $0 tier and downgrading a paid subscriber to
            # Free would silently disable paid features). Pro's $149/mo
            # rate has already been authorized via the Stripe event we
            # are processing, so we hand the buyer the matching surface.
            logger.error(
                "billing-webhook: unknown tier %r in metadata; "
                "falling back to %s. event=%s sub=%s",
                tier, TIER_PRO, event_id, stripe_subscription_id,
            )
            tier = TIER_PRO
        billing_cadence = metadata.get("luciel_billing_cadence") or BILLING_CADENCE_MONTHLY
        if billing_cadence not in ALLOWED_BILLING_CADENCES:
            logger.error(
                "billing-webhook: unknown billing_cadence %r in metadata; "
                "falling back to %s. event=%s sub=%s",
                billing_cadence, BILLING_CADENCE_MONTHLY, event_id, stripe_subscription_id,
            )
            billing_cadence = BILLING_CADENCE_MONTHLY
        # Per-tier instance cap is fully derivable from the tier; we do
        # NOT read it from metadata (the buyer cannot influence it).
        instance_count_cap = TIER_INSTANCE_CAPS.get(
            tier, TIER_INSTANCE_CAPS[TIER_PRO]
        )

        if not email:
            logger.error(
                "billing-webhook: checkout.session.completed has no resolvable "
                "email event=%s sub=%s",
                event_id, stripe_subscription_id,
            )
            return {"applied": False, "reason": "no_email"}

        # Arc 6 / Commit 8.5a -- upgrade-branch detection.
        #
        # The marketing-site signup funnel does NOT stamp
        # ``luciel_admin_id`` into Stripe metadata; it relies on the
        # webhook to mint a fresh Admin/User pair. The Account/Billing
        # upgrade funnel DOES stamp it (via
        # ``BillingService.create_upgrade_checkout``). The presence of
        # this key is the sole signal that this checkout is an upgrade
        # of an existing Admin rather than a fresh mint.
        #
        # If the metadata claims an admin_id but it doesn't resolve to
        # an active Admin row, we fall through to the mint path -- this
        # is defensive against a hand-crafted Stripe event with a stale
        # or forged admin_id. The mint path will create a fresh Admin
        # with a different id; the buyer is paid for and gets a working
        # Admin one way or another.
        upgrade_admin_id = (metadata.get("luciel_admin_id") or "").strip() or None
        ctx = _webhook_audit_ctx()
        if upgrade_admin_id is not None:
            try:
                return self._on_checkout_completed_upgrade(
                    event_id=event_id,
                    data_object=data_object,
                    admin_id=upgrade_admin_id,
                    email=email,
                    display_name=display_name,
                    tier=tier,
                    billing_cadence=billing_cadence,
                    instance_count_cap=instance_count_cap,
                    stripe_subscription_id=stripe_subscription_id,
                    stripe_customer_id=stripe_customer_id,
                    ctx=ctx,
                )
            except _UpgradeBranchFallthrough as fall:
                # Upgrade branch detected a precondition failure
                # (admin missing, admin inactive, or owner mismatch).
                # Log and fall through to the standard mint path so
                # the paid checkout still produces a working Admin.
                logger.warning(
                    "billing-webhook: upgrade-branch fall-through admin_id=%s "
                    "reason=%s event=%s; falling back to mint path",
                    upgrade_admin_id, fall.reason, event_id,
                )

        try:
            # Resolve or create the User row first -- the onboard
            # service needs the user id for the subscription row.
            user = self._resolve_or_create_user(email=email, display_name=display_name)

            # Mint the admin via the existing onboarding primitive.
            # ``OnboardingService.onboard_tenant`` keeps its Arc-5
            # external method name; the ``admin_id`` kwarg threads
            # through to the DB column of the same name.
            admin_id = _mint_admin_id_from_email(email, tier=tier)
            from app.services.onboarding_service import OnboardingService

            onboarding = OnboardingService(self.db)
            # We override the default api key name + created_by so the
            # admin audit trail is honest about the origin. The full
            # onboard runs in the same transaction we are in.
            # Arc 6 Commit 8 -- thread the V2 tier vocab explicitly so
            # the Admin row is born with the right ``tier`` /
            # ``tier_source`` columns. Pre-Arc-6 this relied on the
            # ``tier`` server-default of "free" and a follow-up update,
            # which left a (brief) window where the row was tier="free"
            # despite an active paid checkout -- the unified-signup
            # design tightens this to a single atomic write.
            onboarding.onboard_tenant(
                admin_id=admin_id,
                display_name=display_name,
                tier=tier,
                tier_source="stripe_webhook",
                description=f"Self-serve {tier} subscription -- "
                            f"Stripe sub={stripe_subscription_id}",
                api_key_display_name=f"{display_name} -- Subscription admin key",
                created_by="stripe_webhook",
                audit_ctx=ctx,
            )
            # onboard_tenant commits -- we still want the Subscription
            # row + its audit row in a single transaction, so we
            # explicitly open the next leg below. (Same-transaction
            # would require re-architecting onboard_tenant to NOT
            # commit, which is a larger Step 30a.1 concern.)

            # Now write the Subscription row + its audit row.
            now_iso = datetime.now(timezone.utc).isoformat()
            # Period fields live on subscription items in Stripe basil
            # (2025-03-31) and later. They have never lived on the
            # checkout.session object, so reads off ``data_object`` return
            # None. Step 30a.2-pilot Commit 3f introduces an authoritative
            # fetch of the Subscription object itself (below) so we read
            # ``status``, ``trial_end``, and ``current_period_*`` from the
            # source of truth rather than from the unrelated
            # ``checkout.session.status`` field. Helper kept for read-site
            # uniformity with the load-bearing _on_subscription_updated
            # path. See D-stripe-subscription-period-fields-moved-to-items-2026-05-14
            # and D-stripe-webhook-checkout-vs-subscription-field-source-2026-05-15.
            _period_start, _period_end = self._extract_period_fields(data_object)

            # Fetch the canonical Subscription object. checkout.session
            # only carries the id; status/trial_end/period live on the
            # Subscription. If Stripe is unreachable we degrade to the
            # inline data so we still 200 the webhook (Stripe MUST get
            # a 200 to stop redelivering), and the subsequent
            # ``customer.subscription.updated`` event will backfill.
            stripe_subscription_obj = None
            try:
                stripe_subscription_obj = self.stripe.retrieve_subscription(
                    stripe_subscription_id
                )
            except Exception as fetch_exc:  # noqa: BLE001 -- graceful degrade
                logger.warning(
                    "billing-webhook: retrieve_subscription failed sub=%s err=%s; "
                    "falling back to checkout.session fields",
                    stripe_subscription_id, fetch_exc,
                )

            def _from_sub(field: str, default=None):
                """Read field from the Subscription object if available,
                else from the inline checkout.session data_object."""
                if stripe_subscription_obj is not None:
                    val = (
                        stripe_subscription_obj.get(field)
                        if hasattr(stripe_subscription_obj, "get")
                        else getattr(stripe_subscription_obj, field, default)
                    )
                    if val is not None:
                        return val
                return data_object.get(field, default)

            sub = Subscription(
                # DB column attribute ``Subscription.admin_id`` retained
                # physically by Arc 5 Path A; the value is the minted
                # admin id.
                admin_id=admin_id,
                user_id=user.id,
                customer_email=email,
                stripe_customer_id=stripe_customer_id,
                stripe_subscription_id=stripe_subscription_id,
                stripe_price_id=self._extract_price_id(data_object),
                tier=tier,
                # Commit 3f: read status from Subscription, not from
                # checkout.session (whose ``status`` field is the SESSION
                # status, e.g. 'complete', not a subscription status).
                status=_from_sub("status") or "incomplete",
                # Step 30a.1: new columns from webhook metadata + per-tier defaults.
                billing_cadence=billing_cadence,
                instance_count_cap=instance_count_cap,
                current_period_start=_ts(_from_sub("current_period_start", _period_start)),
                current_period_end=_ts(_from_sub("current_period_end", _period_end)),
                trial_end=_ts(_from_sub("trial_end")),
                cancel_at_period_end=bool(_from_sub("cancel_at_period_end") or False),
                canceled_at=_ts(_from_sub("canceled_at")),
                active=True,
                last_event_id=event_id,
                provider_snapshot=dict(data_object),
            )
            self.db.add(sub)
            self.db.flush()

            audit_repo = AdminAuditRepository(self.db)
            audit_repo.record(
                ctx=ctx,
                # ``AdminAuditRepository.record`` keeps its Arc-5
                # ``admin_id`` kwarg (column-mirrored); we pass
                # ``admin_id`` into it.
                admin_id=admin_id,
                action=ACTION_SUBSCRIPTION_CREATE,
                resource_type=RESOURCE_SUBSCRIPTION,
                resource_pk=sub.id,
                resource_natural_id=stripe_subscription_id,
                after={
                    # Audit-row payload key retained as ``admin_id``
                    # for column symmetry; the value is the admin id.
                    "admin_id": admin_id,
                    "user_id": str(user.id),
                    "customer_email": email,
                    "tier": tier,
                    "billing_cadence": billing_cadence,
                    "instance_count_cap": instance_count_cap,
                    "status": sub.status,
                    "stripe_customer_id": stripe_customer_id,
                    "stripe_subscription_id": stripe_subscription_id,
                    "stripe_event_id": event_id,
                    "minted_at": now_iso,
                },
                note=(
                    f"stripe checkout.session.completed -> minted "
                    f"admin {admin_id} tier={tier} cadence={billing_cadence}"
                ),
            )

            self.db.commit()

            # Step 30a.1 pre-mint of tier-differentiating LucielInstances.
            # Happens AFTER the subscription commit so the cap-enforcement
            # path can see the subscription row. A pre-mint failure does
            # NOT roll back the subscription (the admin is still paid
            # for); we log loudly and let a follow-up reconciliation
            # handle it.
            try:
                from app.services.tier_provisioning_service import (
                    TierProvisioningService,
                )
                premint = TierProvisioningService(self.db)
                premint.premint_for_tier(
                    # ``TierProvisioningService.premint_for_tier`` retains
                    # its Arc-5 ``admin_id`` kwarg (column-mirrored).
                    admin_id=admin_id,
                    tier=tier,
                    primary_user=user,
                    audit_ctx=ctx,
                )
            except Exception:  # pragma: no cover - best-effort post-commit
                logger.exception(
                    "billing-webhook: tier pre-mint failed (subscription "
                    "already committed) admin=%s tier=%s",
                    admin_id, tier,
                )
        except Exception:
            self.db.rollback()
            logger.exception(
                "billing-webhook: checkout.session.completed failed event=%s sub=%s",
                event_id, stripe_subscription_id,
            )
            raise

        # Step 30a.3 (Option B): mint a set-password token and email the
        # buyer a welcome link to ``/auth/set-password``. This REPLACES
        # the pre-30a.3 one-shot magic-link cookie path. The buyer
        # cannot reach a cookied /app session until they click this
        # link and type a password -- that is the enforcement point of
        # "password mandatory at signup".
        #
        # Sent AFTER commit so a transient SES failure cannot roll back
        # the subscription row. Email is best-effort; if it raises we
        # catch+audit but still ACK Stripe (the buyer can recover via
        # POST /api/v1/auth/forgot-password against the same email).
        try:
            token = mint_set_password_token(
                user_id=user.id,
                email=email,
                # ``mint_set_password_token`` retains its Arc-5
                # ``admin_id`` kwarg (column-mirrored).
                admin_id=admin_id,
                purpose="signup",
            )
            url = build_set_password_url(token)
            send_welcome_set_password_email(
                to_email=email,
                set_password_url=url,
                display_name=display_name,
                purpose="signup",
            )
        except Exception:  # pragma: no cover - email is best-effort post-commit
            logger.exception(
                "billing-webhook: welcome-set-password email send failed "
                "(admin minted ok) admin=%s",
                admin_id,
            )

        # Return-dict key ``admin_id`` retained for column symmetry
        # (the value is the admin id). Stripe ignores the 200 body;
        # the key is purely observability.
        return {"applied": True, "admin_id": admin_id, "stripe_subscription_id": stripe_subscription_id}

    # -----------------------------------------------------------------
    # checkout.session.completed -- UPGRADE branch (Arc 6 / Commit 8.5a)
    # -----------------------------------------------------------------

    def _on_checkout_completed_upgrade(
        self,
        *,
        event_id: str,
        data_object: dict,
        admin_id: str,
        email: str,
        display_name: str,
        tier: str,
        billing_cadence: str,
        instance_count_cap: int,
        stripe_subscription_id: str,
        stripe_customer_id: str,
        ctx: AuditContext,
    ) -> dict:
        """Upgrade-branch handler for ``checkout.session.completed``.

        Called from ``_on_checkout_completed`` when Stripe metadata
        carries ``luciel_admin_id``. Distinct from the mint path in
        three ways:

          1. We RESOLVE an existing User (not create) and verify the
             User is the active owner of the named Admin. Owner-check
             is via the ScopeAssignment table; a missing owner row is
             a fall-through (not a mint of a new owner).
          2. We UPDATE the existing Admin row's tier via
             ``TierProvisioningService.upgrade_admin_tier`` -- no
             re-running of ``onboard_tenant`` (which would attempt to
             INSERT a duplicate Admin row and fail on PK conflict).
          3. We SKIP the welcome-set-password email send -- the
             upgrading user already has a password (Free admins set
             one at signup-free post-mint).

        Everything else mirrors the mint path: Subscription row +
        audit row land in the same commit; pre-mint of the primary
        Instance is skipped (one already exists from the Admin's
        original tier provisioning at Free signup).

        Raises ``_UpgradeBranchFallthrough`` on:
          * admin_id does not resolve to an active Admin row
          * resolved User is not the active owner of that Admin
          * resolved User's email does not match the buyer email (a
             forensic safety check -- an attacker who knew an admin_id
             could otherwise upgrade somebody else's Admin by paying)

        The caller traps the fall-through and proceeds with the mint
        path. We deliberately do NOT raise on the tier-noop case
        (replay) -- a redelivered upgrade event whose tier-flip already
        landed is a normal idempotency outcome, handled below via
        TierUpgradeNoopError trap.
        """
        from app.models.admin import Admin as AdminModel
        from app.models.scope_assignment import ScopeAssignment
        from app.services.tier_provisioning_service import (
            TierProvisioningService,
            TierUpgradeNoopError,
        )

        # 1. Resolve User by email (existing -- Free signup created it).
        #    If the User somehow doesn't exist (forensic edge case --
        #    someone manually deleted the row between signup-free and
        #    upgrade), we let _resolve_or_create_user re-create it so
        #    the upgrade still works.
        user = self._resolve_or_create_user(
            email=email, display_name=display_name,
        )

        # 2. Resolve and validate the named Admin.
        admin = self.db.get(AdminModel, admin_id)
        if admin is None:
            raise _UpgradeBranchFallthrough("admin_not_found")
        if not getattr(admin, "active", False):
            raise _UpgradeBranchFallthrough("admin_inactive")

        # 3. Verify the buyer owns this Admin (active owner-role
        #    ScopeAssignment). This closes the cross-Admin upgrade-
        #    attack vector: even if the buyer somehow guesses or
        #    intercepts another tenant's admin_id, Stripe metadata
        #    is buyer-influenceable only via the upgrade route, and
        #    that route derives admin_id from the cookied session.
        #    Here at the webhook we re-verify.
        from app.models.scope_assignment import ScopeRole
        owner_row = self.db.execute(
            select(ScopeAssignment).where(
                ScopeAssignment.admin_id == admin_id,
                ScopeAssignment.user_id == user.id,
                ScopeAssignment.role == ScopeRole.ADMIN_OWNER,
                ScopeAssignment.active.is_(True),
            )
        ).scalars().first()
        if owner_row is None:
            raise _UpgradeBranchFallthrough("user_not_owner_of_admin")

        # 4. Write the Subscription row + its audit row in one commit.
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            _period_start, _period_end = self._extract_period_fields(data_object)

            stripe_subscription_obj = None
            try:
                stripe_subscription_obj = self.stripe.retrieve_subscription(
                    stripe_subscription_id
                )
            except Exception as fetch_exc:  # noqa: BLE001 -- graceful degrade
                logger.warning(
                    "billing-webhook(upgrade): retrieve_subscription failed "
                    "sub=%s err=%s; falling back to checkout.session fields",
                    stripe_subscription_id, fetch_exc,
                )

            def _from_sub(field: str, default=None):
                if stripe_subscription_obj is not None:
                    val = (
                        stripe_subscription_obj.get(field)
                        if hasattr(stripe_subscription_obj, "get")
                        else getattr(stripe_subscription_obj, field, default)
                    )
                    if val is not None:
                        return val
                return data_object.get(field, default)

            sub = Subscription(
                admin_id=admin_id,
                user_id=user.id,
                customer_email=email,
                stripe_customer_id=stripe_customer_id,
                stripe_subscription_id=stripe_subscription_id,
                stripe_price_id=self._extract_price_id(data_object),
                tier=tier,
                status=_from_sub("status") or "incomplete",
                billing_cadence=billing_cadence,
                instance_count_cap=instance_count_cap,
                current_period_start=_ts(_from_sub("current_period_start", _period_start)),
                current_period_end=_ts(_from_sub("current_period_end", _period_end)),
                trial_end=_ts(_from_sub("trial_end")),
                cancel_at_period_end=bool(_from_sub("cancel_at_period_end") or False),
                canceled_at=_ts(_from_sub("canceled_at")),
                active=True,
                last_event_id=event_id,
                provider_snapshot=dict(data_object),
            )
            self.db.add(sub)
            self.db.flush()

            audit_repo = AdminAuditRepository(self.db)
            audit_repo.record(
                ctx=ctx,
                admin_id=admin_id,
                action=ACTION_SUBSCRIPTION_CREATE,
                resource_type=RESOURCE_SUBSCRIPTION,
                resource_pk=sub.id,
                resource_natural_id=stripe_subscription_id,
                after={
                    "admin_id": admin_id,
                    "user_id": str(user.id),
                    "customer_email": email,
                    "tier": tier,
                    "billing_cadence": billing_cadence,
                    "instance_count_cap": instance_count_cap,
                    "status": sub.status,
                    "stripe_customer_id": stripe_customer_id,
                    "stripe_subscription_id": stripe_subscription_id,
                    "stripe_event_id": event_id,
                    "minted_at": now_iso,
                    "flow": "upgrade",
                },
                note=(
                    f"stripe checkout.session.completed UPGRADE -> attached "
                    f"subscription to existing admin {admin_id} "
                    f"tier={tier} cadence={billing_cadence}"
                ),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            logger.exception(
                "billing-webhook(upgrade): subscription write failed event=%s "
                "sub=%s admin=%s",
                event_id, stripe_subscription_id, admin_id,
            )
            raise

        # 5. Tier-flip on the Admin row. Best-effort post-commit: a
        #    failure here means the buyer has paid + has a Subscription
        #    row, but admins.tier is still "free". A reconciler is
        #    expected to re-attempt. We do NOT roll back the
        #    Subscription -- Stripe must get a 200 to stop redelivering.
        try:
            premint = TierProvisioningService(self.db)
            premint.upgrade_admin_tier(
                admin_id=admin_id,
                new_tier=tier,
                new_tier_source="stripe_upgrade",
                audit_ctx=ctx,
            )
        except TierUpgradeNoopError:
            # Replay of an already-applied upgrade. Benign; log + move on.
            logger.info(
                "billing-webhook(upgrade): tier already at target on replay "
                "admin=%s tier=%s event=%s",
                admin_id, tier, event_id,
            )
        except Exception:  # pragma: no cover - best-effort post-commit
            logger.exception(
                "billing-webhook(upgrade): tier-flip failed (subscription "
                "already committed) admin=%s target_tier=%s",
                admin_id, tier,
            )

        # 6. NO welcome-set-password email. The upgrading user already
        #    has a password (Free signup minted a magic link they
        #    redeemed at /auth/set-password). Sending a second one
        #    here would be confusing UX ("you're already signed in,
        #    why are we emailing you a password link?").

        return {
            "applied": True,
            "admin_id": admin_id,
            "stripe_subscription_id": stripe_subscription_id,
            "flow": "upgrade",
        }

    # -----------------------------------------------------------------
    # customer.subscription.updated
    # -----------------------------------------------------------------

    def _on_subscription_updated(self, *, event_id: str, data_object: dict, event: dict) -> dict:
        stripe_subscription_id = data_object.get("id")
        if not stripe_subscription_id:
            return {"applied": False, "reason": "missing_subscription_id"}

        sub = self.db.execute(
            select(Subscription).where(
                Subscription.stripe_subscription_id == stripe_subscription_id
            )
        ).scalars().first()
        if sub is None:
            # We received an update for a subscription we have no row
            # for -- could happen if the checkout.session.completed
            # event has not yet been processed. Record + return; Stripe
            # will redeliver and we'll catch up.
            logger.warning(
                "billing-webhook: customer.subscription.updated for unknown sub=%s event=%s",
                stripe_subscription_id, event_id,
            )
            return {"applied": False, "reason": "unknown_subscription"}

        if sub.last_event_id == event_id:
            self._record_replay(
                event_id=event_id,
                admin_id=sub.admin_id,
                stripe_subscription_id=stripe_subscription_id,
            )
            return {"applied": False, "reason": "replay"}

        before = {
            "status": sub.status,
            "cancel_at_period_end": sub.cancel_at_period_end,
            "current_period_end": sub.current_period_end.isoformat()
            if sub.current_period_end else None,
        }

        sub.status = data_object.get("status") or sub.status
        # Period fields moved from subscription-level to subscription-item-level
        # in Stripe API version 2025-03-31.basil; the account's resolved API
        # version on webhook delivery is what determines payload shape. The
        # _extract_period_fields helper reads items[0] first, falls back to
        # the old top-level fields. See D-stripe-subscription-period-fields-
        # moved-to-items-2026-05-14.
        _period_start, _period_end = self._extract_period_fields(data_object)
        sub.current_period_start = _ts(_period_start) or sub.current_period_start
        sub.current_period_end = _ts(_period_end) or sub.current_period_end
        sub.trial_end = _ts(data_object.get("trial_end")) or sub.trial_end
        sub.cancel_at_period_end = bool(data_object.get("cancel_at_period_end") or False)
        sub.canceled_at = _ts(data_object.get("canceled_at")) or sub.canceled_at
        sub.stripe_price_id = self._extract_price_id(data_object) or sub.stripe_price_id
        sub.last_event_id = event_id
        sub.provider_snapshot = dict(data_object)

        ctx = _webhook_audit_ctx()
        audit_repo = AdminAuditRepository(self.db)
        audit_repo.record(
            ctx=ctx,
            admin_id=sub.admin_id,
            action=ACTION_SUBSCRIPTION_UPDATE,
            resource_type=RESOURCE_SUBSCRIPTION,
            resource_pk=sub.id,
            resource_natural_id=stripe_subscription_id,
            before=before,
            after={
                "status": sub.status,
                "cancel_at_period_end": sub.cancel_at_period_end,
                "current_period_end": sub.current_period_end.isoformat()
                if sub.current_period_end else None,
                "stripe_event_id": event_id,
            },
            note="stripe customer.subscription.updated",
        )
        self.db.commit()
        return {"applied": True, "admin_id": sub.admin_id}

    # -----------------------------------------------------------------
    # customer.subscription.deleted
    # -----------------------------------------------------------------

    def _on_subscription_deleted(self, *, event_id: str, data_object: dict, event: dict) -> dict:
        """Branching handler for ``customer.subscription.deleted``.

        Two distinct meanings flow through the same Stripe event:

        **V2 downgrade path** — ``sub.pending_downgrade_target`` is set.
          The buyer earlier clicked "downgrade" in the Account UI;
          ``BillingService.schedule_downgrade`` set the column and
          called ``stripe.Subscription.modify(cancel_at_period_end=True)``;
          Stripe has now reached ``current_period_end`` and fired the
          delete event. We:
            1. Flip the Subscription row inactive + canceled.
            2. Call ``TierProvisioningService.downgrade_admin_tier``
               to change the Admin row's tier to the target.
            3. Call ``DowngradeArchiveService.archive_overflow_for_admin``
               to LRU-archive any rows that exceed the destination
               tier's caps (instances / embed keys / CNAMEs / seats).
            4. Emit ``ACTION_SUBSCRIPTION_DOWNGRADE_APPLIED`` audit row
               with the per-axis overflow tally + the archive
               timestamp.
            5. NULL out ``pending_downgrade_target`` (the action is no
               longer pending; this is what makes a re-run of the
               handler a noop).
          The Admin row stays ``active=True`` — a downgrade is not a
          deactivation. The buyer keeps their account; they just have
          less of it.

        **V1 hard-cancel path** — ``sub.pending_downgrade_target`` is
          NULL. Either a manual Stripe-Dashboard cancel or a
          ``BillingService.process_pilot_refund`` call. We preserve
          the Arc 5 / Step 30a behaviour:
            1. Flip the Subscription row inactive + canceled.
            2. Emit ``ACTION_SUBSCRIPTION_CANCEL`` audit row.
            3. Cascade-deactivate the Admin via
               ``AdminService.deactivate_tenant_with_cascade``.
          The Admin row is set ``active=False``; the buyer loses
          access entirely.

        Idempotency:
          The ``last_event_id == event_id`` guard short-circuits a
          Stripe redeliver of the same event. On the V2 path a
          replay would re-attempt downgrade_admin_tier, which raises
          ``TierDowngradeNoopError`` because the tier was already
          flipped on the first apply — the handler catches that as a
          benign no-op. On the V1 path the deactivate-cascade is
          itself idempotent (admin.active is already False on second
          run).
        """
        stripe_subscription_id = data_object.get("id")
        if not stripe_subscription_id:
            return {"applied": False, "reason": "missing_subscription_id"}

        sub = self.db.execute(
            select(Subscription).where(
                Subscription.stripe_subscription_id == stripe_subscription_id
            )
        ).scalars().first()
        if sub is None:
            return {"applied": False, "reason": "unknown_subscription"}

        if sub.last_event_id == event_id:
            self._record_replay(
                event_id=event_id,
                admin_id=sub.admin_id,
                stripe_subscription_id=stripe_subscription_id,
            )
            return {"applied": False, "reason": "replay"}

        ctx = _webhook_audit_ctx()
        admin_id = sub.admin_id  # legacy column name; semantically admin_id.
        target_tier = sub.pending_downgrade_target  # branch discriminant

        # Shared step — flip the Subscription row inactive + canceled.
        # Both branches do this; do it once, with the same shape.
        before_status = sub.status
        before_pending = target_tier  # snapshot before we null it
        sub.status = STATUS_CANCELED
        sub.active = False
        sub.canceled_at = (
            _ts(data_object.get("canceled_at")) or datetime.now(timezone.utc)
        )
        sub.last_event_id = event_id
        sub.provider_snapshot = dict(data_object)

        # --------------------------------------------------------------
        # V2 downgrade branch — pending_downgrade_target is set.
        # --------------------------------------------------------------
        if target_tier is not None:
            return self._apply_v2_downgrade(
                sub=sub,
                admin_id=admin_id,
                target_tier=target_tier,
                before_status=before_status,
                before_pending=before_pending,
                event_id=event_id,
                ctx=ctx,
            )

        # --------------------------------------------------------------
        # V1 hard-cancel branch — manual Stripe-Dashboard cancels and
        # pilot-refund teardown. Preserves Step 30a behaviour verbatim.
        # --------------------------------------------------------------
        return self._apply_v1_hard_cancel(
            sub=sub,
            admin_id=admin_id,
            stripe_subscription_id=stripe_subscription_id,
            before_status=before_status,
            event_id=event_id,
            ctx=ctx,
        )

    # ------------------------------------------------------------------
    # V2 downgrade apply — Arc 6 Commit 8.5b.
    # ------------------------------------------------------------------

    def _apply_v2_downgrade(
        self,
        *,
        sub: Subscription,
        admin_id: str,
        target_tier: str,
        before_status: str,
        before_pending: str,
        event_id: str,
        ctx: AuditContext,
    ) -> dict:
        """V2 deferred-downgrade boundary apply.

        Touches:
          * sub row — already flipped inactive + canceled by caller;
            we null pending_downgrade_target as the final step.
          * admins.tier              — via TierProvisioningService
          * instances.active + stamp — via DowngradeArchiveService
          * api_keys.active + stamp  — via DowngradeArchiveService
          * admin_widget_domains.stamp — via DowngradeArchiveService
          * scope_assignments.ended_* — via DowngradeArchiveService

        Atomicity: every service call here uses ``autocommit=False``;
        the final ``self.db.commit()`` lands the entire boundary apply
        as one transaction. A failure rolls back the sub-row mutation
        too.
        """
        # Lazy imports — these services have heavier dependency graphs
        # than this module, so we defer them to the V2 path only.
        from app.services.downgrade_archive_service import (
            DowngradeArchiveService,
        )
        from app.services.tier_provisioning_service import (
            TierDowngradeNoopError,
            TierProvisioningService,
        )

        audit_repo = AdminAuditRepository(self.db)

        # 1. Flip the Admin row's tier via the provisioning service.
        # The service handles the strictly-lower guard and emits its
        # own ACTION_UPDATE audit row on admins. Catch the noop case
        # so a Stripe redeliver after a partial apply doesn't 5xx.
        provisioning = TierProvisioningService(self.db)
        try:
            tier_result = provisioning.downgrade_admin_tier(
                admin_id=admin_id,
                new_tier=target_tier,
                new_tier_source="stripe_webhook_downgrade",
                audit_ctx=ctx,
            )
            old_tier = tier_result["old_tier"]
        except TierDowngradeNoopError:
            # Replay path: the admin row is already at target_tier from
            # a prior apply. Log + continue — we still want to run the
            # archive sweep (idempotent) and emit the boundary audit.
            logger.info(
                "billing-webhook: v2-downgrade noop on admin=%s "
                "(tier already at %s); continuing with archive sweep",
                admin_id, target_tier,
            )
            old_tier = target_tier  # for audit shape; no-op

        # 2. Run the LRU overflow archive sweep across all 4 axes.
        # Autocommit=False so the entire boundary apply lands as one txn.
        archive_service = DowngradeArchiveService(self.db)
        summary = archive_service.archive_overflow_for_admin(
            admin_id=admin_id,
            target_tier=target_tier,
            autocommit=False,
        )

        # 3. Null the pending marker — the action is no longer pending.
        sub.pending_downgrade_target = None

        # 4. Emit the boundary audit row. The after_json captures the
        # full per-axis archive tally so a forensic engineer can
        # reconstruct "what disappeared at this boundary?" without
        # joining four other tables.
        audit_repo.record(
            ctx=ctx,
            admin_id=admin_id,
            action=ACTION_SUBSCRIPTION_DOWNGRADE_APPLIED,
            resource_type=RESOURCE_SUBSCRIPTION,
            resource_pk=sub.id,
            resource_natural_id=sub.stripe_subscription_id,
            before={
                "status": before_status,
                "active": True,
                "tier": old_tier,
                "pending_downgrade_target": before_pending,
            },
            after={
                "status": sub.status,
                "active": False,
                "tier": target_tier,
                "pending_downgrade_target": None,
                "stripe_event_id": event_id,
                "archived_at": (
                    summary.archived_at.isoformat()
                    if summary.archived_at else None
                ),
                "overflow": {
                    axis: {
                        "cap": tally.cap,
                        "current": tally.current,
                        "overflow": tally.overflow,
                    }
                    for axis, tally in summary.axes.items()
                },
            },
            note=(
                f"stripe customer.subscription.deleted -> v2 downgrade "
                f"{old_tier} -> {target_tier} (overflow archived: "
                f"{summary.total_overflow})"
            ),
            autocommit=False,
        )

        self.db.commit()
        logger.info(
            "billing-webhook: v2-downgrade applied admin=%s old_tier=%s "
            "target_tier=%s overflow_total=%d",
            admin_id, old_tier, target_tier, summary.total_overflow,
        )
        return {
            "applied": True,
            "branch": "v2_downgrade",
            "admin_id": admin_id,
            "target_tier": target_tier,
            "overflow_total": summary.total_overflow,
        }

    # ------------------------------------------------------------------
    # V1 hard-cancel apply — Step 30a behaviour, preserved verbatim.
    # ------------------------------------------------------------------

    def _apply_v1_hard_cancel(
        self,
        *,
        sub: Subscription,
        admin_id: str,
        stripe_subscription_id: str,
        before_status: str,
        event_id: str,
        ctx: AuditContext,
    ) -> dict:
        """V1 hard-cancel apply — manual Stripe-Dashboard cancels and
        pilot-refund teardown.

        Behaviour is the pre-Arc-6 Step 30a path, lifted verbatim:
          1. Audit row ACTION_SUBSCRIPTION_CANCEL.
          2. AdminService.deactivate_tenant_with_cascade.

        The Admin row is flipped active=False; the buyer loses access.
        This is the right behaviour for a manual Stripe cancel because
        the buyer (or Stripe ops, on a chargeback teardown) explicitly
        chose to end the relationship — not to step down a tier.
        """
        audit_repo = AdminAuditRepository(self.db)
        audit_repo.record(
            ctx=ctx,
            admin_id=admin_id,
            action=ACTION_SUBSCRIPTION_CANCEL,
            resource_type=RESOURCE_SUBSCRIPTION,
            resource_pk=sub.id,
            resource_natural_id=stripe_subscription_id,
            before={"status": before_status, "active": True},
            after={
                "status": sub.status,
                "active": False,
                "stripe_event_id": event_id,
            },
            note="stripe customer.subscription.deleted -> cancel + deactivate admin cascade",
        )

        # Cascade deactivate the admin.
        # ``AdminService.deactivate_tenant_with_cascade`` retains its
        # Arc-5 external method name; semantically it now deactivates
        # the admin (Arc 5 B8 rename).
        # The cascade method commits if autocommit=True; we pass
        # autocommit=False so the subscription mutation and cascade
        # land in one transaction.
        try:
            from app.repositories.agent_repository import AgentRepository
            from app.services.admin_service import AdminService
            from app.services.instance_service import InstanceService

            admin = AdminService(self.db)
            agent_repo = AgentRepository(self.db)
            # D-webhook-luciel-instance-service-missing-kwarg-2026-05-14:
            # InstanceService.__init__ is (self, db, *, admin_service).
            # The kwarg is keyword-only and required; without it the
            # constructor raises TypeError during the cancel-webhook cascade.
            luciel_service = InstanceService(self.db, admin_service=admin)

            admin.deactivate_tenant_with_cascade(
                admin_id,
                audit_ctx=ctx,
                luciel_instance_service=luciel_service,
                agent_repo=agent_repo,
                updated_by="stripe_webhook",
                autocommit=False,
            )
        except Exception:
            self.db.rollback()
            logger.exception(
                "billing-webhook: cascade-deactivate failed admin=%s sub=%s",
                admin_id, stripe_subscription_id,
            )
            raise

        self.db.commit()
        return {
            "applied": True,
            "branch": "v1_hard_cancel",
            "admin_id": admin_id,
        }

    # -----------------------------------------------------------------
    # invoice.payment_failed
    # -----------------------------------------------------------------

    def _on_invoice_payment_failed(self, *, event_id: str, data_object: dict, event: dict) -> dict:
        """Update status if Stripe has flipped the subscription to past_due.

        We do not initiate any side effect ourselves -- Stripe's
        dunning sequence is the authoritative cadence. The subsequent
        ``customer.subscription.updated`` event drives the status
        change. This handler exists so the event is recorded in audit.
        """
        stripe_subscription_id = data_object.get("subscription")
        if not stripe_subscription_id:
            return {"applied": False, "reason": "missing_subscription"}

        sub = self.db.execute(
            select(Subscription).where(
                Subscription.stripe_subscription_id == stripe_subscription_id
            )
        ).scalars().first()
        if sub is None:
            return {"applied": False, "reason": "unknown_subscription"}

        if sub.last_event_id == event_id:
            self._record_replay(
                event_id=event_id,
                admin_id=sub.admin_id,
                stripe_subscription_id=stripe_subscription_id,
            )
            return {"applied": False, "reason": "replay"}

        ctx = _webhook_audit_ctx()
        audit_repo = AdminAuditRepository(self.db)
        audit_repo.record(
            ctx=ctx,
            admin_id=sub.admin_id,
            action=ACTION_SUBSCRIPTION_UPDATE,
            resource_type=RESOURCE_SUBSCRIPTION,
            resource_pk=sub.id,
            resource_natural_id=stripe_subscription_id,
            after={
                "invoice_payment_failed": True,
                "amount_due": data_object.get("amount_due"),
                "attempt_count": data_object.get("attempt_count"),
                "stripe_event_id": event_id,
            },
            note="stripe invoice.payment_failed -- dunning pending",
        )
        sub.last_event_id = event_id
        self.db.commit()
        return {"applied": True, "admin_id": sub.admin_id}

    # -----------------------------------------------------------------
    # Arc 18 — invoice.paid: conversation-overage report + counter reset
    # -----------------------------------------------------------------

    def _budget_meter_inst(self):
        if self._budget_meter is None:
            from app.runtime.budget_meter import BudgetMeter

            self._budget_meter = BudgetMeter()
        return self._budget_meter

    def _on_invoice_paid(self, *, event_id: str, data_object: dict, event: dict) -> dict:
        """Cycle close (§3.4.1b). GUARDRAIL-bounded: this handler does NOT
        alter base-invoice handling and does NOT book the base subscription
        invoice (Stripe has already settled it). It ONLY:

          (a) computes per-instance conversation overage for the closing
              period and reports a metered usage record to Stripe, and
          (b) writes a durable overage-ledger row + audit, then resets the
              Redis counter and advances ``current_period_start``.

        Dedup: the ``last_event_id == event_id`` guard short-circuits a
        redelivered event; the ledger's unique
        ``(admin_id, instance_id, period_start)`` is a second idempotency
        wall, and the Stripe usage record is reported with ``action='set'``
        under an idempotency key so a race cannot double-bill.

        Free admins (no Subscription row) never reach here — invoice.paid is
        a paying-subscription event. A non-paying or unknown subscription is
        a benign no-op.
        """
        stripe_subscription_id = data_object.get("subscription")
        if not stripe_subscription_id:
            return {"applied": False, "reason": "missing_subscription"}

        sub = self.db.execute(
            select(Subscription).where(
                Subscription.stripe_subscription_id == stripe_subscription_id
            )
        ).scalars().first()
        if sub is None:
            return {"applied": False, "reason": "unknown_subscription"}

        if sub.last_event_id == event_id:
            self._record_replay(
                event_id=event_id,
                admin_id=sub.admin_id,
                stripe_subscription_id=stripe_subscription_id,
            )
            return {"applied": False, "reason": "replay"}

        from app.models.admin import Admin
        from app.models.conversation_overage_ledger import ConversationOverageLedger
        from app.models.instance import Instance
        from app.models.admin_audit_log import ACTION_OVERAGE_REPORTED
        from app.policy.entitlements import (
            CADENCE_MONTHLY,
            conversation_budget,
            overage_price_config_key,
            overage_rate_per_100_cents,
        )
        from app.runtime.billing_period import period_start_iso
        from app.services.overage_billing import (
            overage_count,
            overage_line_item_description,
            overage_units,
            rate_string_from_cents,
        )

        tier = sub.tier
        cadence = sub.billing_cadence or CADENCE_MONTHLY
        cap = conversation_budget(tier, cadence)
        # The CLOSING period anchor = the period_start the counter keyed on
        # for the cycle that just billed (Stripe's prior current_period_start).
        closing_period_dt = sub.current_period_start
        closing_period_iso = period_start_iso(closing_period_dt)

        meter = self._budget_meter_inst()
        ctx = _webhook_audit_ctx()
        audit_repo = AdminAuditRepository(self.db)

        # Overage bills only when the (tier, cadence) carries a rate AND the
        # founder has provisioned both the metered overage Price (gates the
        # rate) and the Stripe Meter event_name (the report target).
        rate_cents = overage_rate_per_100_cents(tier, cadence)
        price_key = overage_price_config_key(tier, cadence)
        overage_price_id = getattr(settings, price_key, "") if price_key else ""
        meter_event_name = getattr(settings, "stripe_meter_event_overage", "") or ""
        customer_id = self.db.execute(
            select(Admin.stripe_customer_id).where(Admin.id == sub.admin_id)
        ).scalar_one_or_none()
        can_report = bool(
            rate_cents is not None
            and overage_price_id
            and meter_event_name
            and customer_id
        )

        instances = self.db.execute(
            select(Instance.id, Instance.display_name).where(
                Instance.admin_id == sub.admin_id
            )
        ).all()

        closed_instances: list[dict] = []
        for inst_id, inst_name in instances:
            used = meter.current_count(
                admin_id=sub.admin_id,
                instance_id=inst_id,
                period_start=closing_period_iso,
            )
            over = overage_count(conversations_used=used, budget_cap=cap)
            units = overage_units(over)

            usage_record_id = None
            reported_at = None
            # Report metered usage only when the tier bills overage, there IS
            # overage, and the meter + customer are provisioned.
            if units > 0 and can_report:
                rate_str = rate_string_from_cents(rate_cents)
                description = overage_line_item_description(
                    instance_name=inst_name or f"Instance {inst_id}",
                    additional=over,
                    rate_str=rate_str,
                )
                try:
                    rec = self.stripe.report_overage_usage(
                        customer_id=customer_id,
                        event_name=meter_event_name,
                        value=units,
                        idempotency_key=(
                            f"overage:{event_id}:{inst_id}:{closing_period_iso}"
                        ),
                    )
                    if rec is not None:
                        usage_record_id = (
                            rec.get("identifier") or rec.get("id")
                            if isinstance(rec, dict)
                            else getattr(rec, "identifier", None) or getattr(rec, "id", None)
                        )
                        reported_at = datetime.now(timezone.utc)
                    logger.info(
                        "invoice.paid: reported overage usage admin=%s inst=%s "
                        "units=%s desc=%r", sub.admin_id, inst_id, units, description,
                    )
                except Exception as exc:  # noqa: BLE001 — period still resets
                    logger.warning(
                        "invoice.paid: usage-record report failed exc_class=%s "
                        "admin=%s inst=%s — period still resets",
                        type(exc).__name__, sub.admin_id, inst_id,
                    )

            # Durable ledger row (idempotent on the unique period key).
            self.db.add(
                ConversationOverageLedger(
                    admin_id=sub.admin_id,
                    instance_id=inst_id,
                    billing_period_start=closing_period_dt or _ts(0),
                    conversations_used=used,
                    budget_cap=cap,
                    overage_count=over,
                    overage_units_reported=units,
                    tier_at_close=tier,
                    cadence_at_close=cadence,
                    stripe_usage_record_id=usage_record_id,
                    reported_at=reported_at,
                )
            )
            closed_instances.append(
                {"instance_id": inst_id, "used": used, "overage": over, "units": units}
            )

        audit_repo.record(
            ctx=ctx,
            admin_id=sub.admin_id,
            action=ACTION_OVERAGE_REPORTED,
            resource_type=RESOURCE_SUBSCRIPTION,
            resource_pk=sub.id,
            resource_natural_id=stripe_subscription_id,
            after={
                "stripe_event_id": event_id,
                "tier": tier,
                "cadence": cadence,
                "cap": cap,
                "closing_period_start": closing_period_iso,
                "instances": closed_instances,
            },
            note="stripe invoice.paid -- conversation overage reported + period reset",
        )

        # Advance the period anchor to Stripe's NEW cycle start, then reset
        # the Redis counters for the closed period.
        new_period_start = _ts(
            (data_object.get("lines") or {}).get("data", [{}])[0].get("period", {}).get("start")
            if (data_object.get("lines") or {}).get("data")
            else None
        )
        if new_period_start is not None:
            sub.current_period_start = new_period_start
        sub.last_event_id = event_id
        self.db.commit()

        # Belt-and-suspenders Redis reset (the NEW period_start already keys
        # a fresh counter; deleting the old key is cleanup). Best-effort.
        for inst in closed_instances:
            try:
                meter.reset(
                    admin_id=sub.admin_id,
                    instance_id=inst["instance_id"],
                    period_start=closing_period_iso,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "invoice.paid: counter reset failed exc_class=%s admin=%s inst=%s",
                    type(exc).__name__, sub.admin_id, inst["instance_id"],
                )

        return {"applied": True, "admin_id": sub.admin_id, "instances": len(closed_instances)}

    # -----------------------------------------------------------------
    # Replay + unknown event recording
    # -----------------------------------------------------------------

    def _record_replay(self, *, event_id: str, admin_id: str, stripe_subscription_id: str) -> None:
        ctx = _webhook_audit_ctx()
        audit_repo = AdminAuditRepository(self.db)
        audit_repo.record(
            ctx=ctx,
            admin_id=admin_id,
            action=ACTION_BILLING_WEBHOOK_REPLAY_REJECTED,
            resource_type=RESOURCE_SUBSCRIPTION,
            resource_natural_id=stripe_subscription_id,
            after={"stripe_event_id": event_id},
            note=f"stripe webhook replay rejected event={event_id}",
        )
        self.db.commit()

    def _record_unknown_event(self, *, event_id: str, event_type: str) -> None:
        ctx = _webhook_audit_ctx()
        audit_repo = AdminAuditRepository(self.db)
        # The ``admin_id`` column value for an unknown event is
        # unknowable (no admin can be resolved); use the 'platform'
        # sentinel reserved by AdminAuditRepository.
        audit_repo.record(
            ctx=ctx,
            admin_id="platform",
            action=ACTION_BILLING_WEBHOOK_REPLAY_REJECTED,
            resource_type=RESOURCE_SUBSCRIPTION,
            resource_natural_id=f"unknown:{event_type}",
            after={"stripe_event_id": event_id, "event_type": event_type},
            note=f"stripe webhook unknown event_type={event_type}",
        )
        self.db.commit()

    # -----------------------------------------------------------------
    # User identity resolve-or-create
    # -----------------------------------------------------------------

    def _resolve_or_create_user(self, *, email: str, display_name: str) -> User:
        """Return an existing User row by email, or create a new one.

        Email comparison is case-insensitive at the DB layer (LOWER(email)
        expression index, Step 24.5b migration). We compare lowercased
        here too for symmetry with the schema, then store the
        Stripe-provided casing verbatim if a new row is being created.
        """
        from sqlalchemy import func
        existing = self.db.execute(
            select(User).where(func.lower(User.email) == email.lower())
        ).scalars().first()
        if existing is not None:
            return existing
        user = User(
            email=email,
            display_name=display_name,
            synthetic=False,
            active=True,
        )
        self.db.add(user)
        self.db.flush()
        return user

    # -----------------------------------------------------------------
    # Stripe payload helpers
    # -----------------------------------------------------------------

    @staticmethod
    def _extract_price_id(data_object: dict) -> str | None:
        """Pull the first price id out of a Stripe subscription or session payload."""
        # subscription.object shape
        items = (data_object.get("items") or {}).get("data") or []
        if items:
            price = (items[0] or {}).get("price") or {}
            pid = price.get("id")
            if pid:
                return pid
        # checkout.session shape — no items inline. Arc 6 Commit 5 (Path A)
        # removed the V1 single-SKU fallback ``settings.stripe_price_individual``
        # because V2 has multiple Prices (pro_monthly, pro_annual,
        # enterprise_monthly, enterprise_annual -- Arc 7 doctrine pivot
        # retired the prior enterprise_floor_annual metered shape) and
        # there is no defensible default to pick blindly.
        # Modern Stripe checkout.session.completed payloads
        # always have line items resolvable via Stripe API expansion at
        # the caller layer; if we get a payload without inline items we
        # return None and let the caller decide (typically: log + leave
        # ``stripe_price_id`` NULL on the Subscription row, which the
        # reconciler can later backfill by retrieving the session).
        return None

    @staticmethod
    def _extract_period_fields(
        data_object: dict,
    ) -> tuple[int | None, int | None]:
        """Pull current_period_{start,end} from a Stripe subscription payload.

        Stripe API version 2025-03-31.basil moved these fields from the
        top level of the Subscription resource to per-subscription-item
        (`items.data[0].current_period_*`). The account's resolved API
        version on webhook delivery is what determines the payload
        shape, regardless of any `stripe.api_version` pin in the SDK.

        We read items-level first (basil and later) and fall back to
        the top-level fields (pre-basil), so the handler is correct
        under either shape. For multi-item subscriptions (not Step 30a
        v1, where each subscription has exactly one item), the first
        item's period defines the subscription's period -- this matches
        Stripe's documented guidance for the single-item case and is
        the safest default for a hypothetical mixed-interval future.

        Returns a tuple of unix-epoch ints (or Nones). The caller wraps
        each value with ``_ts(...)`` to produce a ``datetime | None``.

        See D-stripe-subscription-period-fields-moved-to-items-2026-05-14.
        """
        items = (data_object.get("items") or {}).get("data") or []
        if items:
            item0 = items[0] or {}
            start = item0.get("current_period_start")
            end = item0.get("current_period_end")
            if start is not None or end is not None:
                return start, end
        # Pre-basil fallback (or non-subscription payloads like
        # checkout.session, where both reads are None by design).
        return (
            data_object.get("current_period_start"),
            data_object.get("current_period_end"),
        )
