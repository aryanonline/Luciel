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
         - call ``OnboardingService.onboard_tenant`` to mint a fresh
           tenant (one tenant per Individual subscriber at v1)
         - INSERT a Subscription row
         - mint a magic-link JWT and send the email
  * customer.subscription.updated
      -- ``status``, cycle dates, ``cancel_at_period_end`` flip.
  * customer.subscription.deleted
      -- cancel: flip ``active=False`` on the Subscription, then
         call ``AdminService.deactivate_tenant_with_cascade`` so the
         tenant's children deactivate as documented in ARCHITECTURE
         §4.5.
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
    ACTION_SUBSCRIPTION_UPDATE,
    RESOURCE_SUBSCRIPTION,
)
from app.models.subscription import (
    ALLOWED_BILLING_CADENCES,
    ALLOWED_TIERS,
    BILLING_CADENCE_MONTHLY,
    STATUS_CANCELED,
    Subscription,
    TIER_INDIVIDUAL,
    TIER_INSTANCE_CAPS,
)
from app.models.user import User
from app.repositories.admin_audit_repository import AdminAuditRepository, AuditContext
from app.services.email_service import send_magic_link_email
from app.services.magic_link_service import build_magic_link_url, mint_magic_link_token

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Tenant id minting
# ---------------------------------------------------------------------

# Tier-aware tenant-id prefix. The prefix tags self-serve tenants by
# tier at a glance, so a grep / DB query against ``tenant_configs``
# separates Individual-self-serve from Team-self-serve from Company-
# self-serve without joining ``subscriptions``. Sales-assisted tenants
# (created outside the webhook) carry no tier prefix.
_TIER_PREFIX = {
    "individual": "ind",
    "team":       "team",
    "company":    "co",
}


def _mint_tenant_id_from_email(email: str, tier: str = TIER_INDIVIDUAL) -> str:
    """Generate a URL-safe, collision-resistant tenant slug from an email.

    Shape: ``<tier-prefix>-<8 hex chars>``. The prefix is one of
    ``ind`` / ``team`` / ``co`` per ``_TIER_PREFIX`` (Step 30a.1).
    The 8 hex chars give 32 bits of randomness — collision probability
    is negligible at the expected scale of self-serve subscribers, and
    a collision is caught by the existing tenant_configs.tenant_id
    unique constraint.
    """
    prefix = _TIER_PREFIX.get(tier, "ind")
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

    def __init__(self, db: Session) -> None:
        self.db = db

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
        """Mint tenant + user + subscription atomically.

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
            self._record_replay(
                event_id=event_id,
                tenant_id=existing.tenant_id,
                stripe_subscription_id=stripe_subscription_id,
            )
            return {"applied": False, "reason": "replay", "tenant_id": existing.tenant_id}

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
        tier = metadata.get("luciel_tier") or TIER_INDIVIDUAL
        if tier not in ALLOWED_TIERS:
            # Defensive — schema-validated upstream, but a hand-crafted
            # Stripe event could arrive with garbage. Fall back to the
            # safest tier (Individual) and log loudly; do NOT raise here
            # because Stripe must get a 200 to stop redelivering.
            logger.error(
                "billing-webhook: unknown tier %r in metadata; "
                "falling back to %s. event=%s sub=%s",
                tier, TIER_INDIVIDUAL, event_id, stripe_subscription_id,
            )
            tier = TIER_INDIVIDUAL
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
            tier, TIER_INSTANCE_CAPS[TIER_INDIVIDUAL]
        )

        if not email:
            logger.error(
                "billing-webhook: checkout.session.completed has no resolvable "
                "email event=%s sub=%s",
                event_id, stripe_subscription_id,
            )
            return {"applied": False, "reason": "no_email"}

        ctx = _webhook_audit_ctx()
        try:
            # Resolve or create the User row first -- the onboard
            # service needs the user id for the subscription row.
            user = self._resolve_or_create_user(email=email, display_name=display_name)

            # Mint the tenant via the existing onboarding primitive.
            tenant_id = _mint_tenant_id_from_email(email, tier=tier)
            from app.services.onboarding_service import OnboardingService

            onboarding = OnboardingService(self.db)
            # We override the default api key name + created_by so the
            # admin audit trail is honest about the origin. The full
            # onboard runs in the same transaction we are in.
            onboarding.onboard_tenant(
                tenant_id=tenant_id,
                display_name=display_name,
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
            # checkout.session object, so both reads return None here --
            # subscription.updated backfills them. Helper kept for read-
            # site uniformity with the load-bearing _on_subscription_updated
            # path. See D-stripe-subscription-period-fields-moved-to-items-2026-05-14.
            _period_start, _period_end = self._extract_period_fields(data_object)
            sub = Subscription(
                tenant_id=tenant_id,
                user_id=user.id,
                customer_email=email,
                stripe_customer_id=stripe_customer_id,
                stripe_subscription_id=stripe_subscription_id,
                stripe_price_id=self._extract_price_id(data_object),
                tier=tier,
                status=data_object.get("status") or "incomplete",
                # Step 30a.1: new columns from webhook metadata + per-tier defaults.
                billing_cadence=billing_cadence,
                instance_count_cap=instance_count_cap,
                current_period_start=_ts(_period_start),
                current_period_end=_ts(_period_end),
                trial_end=_ts(data_object.get("trial_end")),
                cancel_at_period_end=bool(data_object.get("cancel_at_period_end") or False),
                canceled_at=_ts(data_object.get("canceled_at")),
                active=True,
                last_event_id=event_id,
                provider_snapshot=dict(data_object),
            )
            self.db.add(sub)
            self.db.flush()

            audit_repo = AdminAuditRepository(self.db)
            audit_repo.record(
                ctx=ctx,
                tenant_id=tenant_id,
                action=ACTION_SUBSCRIPTION_CREATE,
                resource_type=RESOURCE_SUBSCRIPTION,
                resource_pk=sub.id,
                resource_natural_id=stripe_subscription_id,
                after={
                    "tenant_id": tenant_id,
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
                    f"tenant {tenant_id} tier={tier} cadence={billing_cadence}"
                ),
            )

            self.db.commit()

            # Step 30a.1 pre-mint of tier-differentiating LucielInstances.
            # Happens AFTER the subscription commit so the cap-enforcement
            # path can see the subscription row. A pre-mint failure does
            # NOT roll back the subscription (the tenant is still paid for);
            # we log loudly and let a follow-up reconciliation handle it.
            try:
                from app.services.tier_provisioning_service import (
                    TierProvisioningService,
                )
                premint = TierProvisioningService(self.db)
                premint.premint_for_tier(
                    tenant_id=tenant_id,
                    tier=tier,
                    primary_user=user,
                    audit_ctx=ctx,
                )
            except Exception:  # pragma: no cover - best-effort post-commit
                logger.exception(
                    "billing-webhook: tier pre-mint failed (subscription "
                    "already committed) tenant=%s tier=%s",
                    tenant_id, tier,
                )
        except Exception:
            self.db.rollback()
            logger.exception(
                "billing-webhook: checkout.session.completed failed event=%s sub=%s",
                event_id, stripe_subscription_id,
            )
            raise

        # Email the magic link AFTER commit so a transient send failure
        # cannot roll back the subscription row. The email service
        # logs+sends synchronously; if it raises we catch and audit
        # but still return success to Stripe.
        try:
            token = mint_magic_link_token(
                user_id=user.id, email=email, tenant_id=tenant_id,
            )
            url = build_magic_link_url(token)
            send_magic_link_email(
                to_email=email, magic_link_url=url, display_name=display_name,
            )
        except Exception:  # pragma: no cover - email is best-effort post-commit
            logger.exception(
                "billing-webhook: magic-link email send failed (tenant minted ok) tenant=%s",
                tenant_id,
            )

        return {"applied": True, "tenant_id": tenant_id, "stripe_subscription_id": stripe_subscription_id}

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
                tenant_id=sub.tenant_id,
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
            tenant_id=sub.tenant_id,
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
        return {"applied": True, "tenant_id": sub.tenant_id}

    # -----------------------------------------------------------------
    # customer.subscription.deleted
    # -----------------------------------------------------------------

    def _on_subscription_deleted(self, *, event_id: str, data_object: dict, event: dict) -> dict:
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
                tenant_id=sub.tenant_id,
                stripe_subscription_id=stripe_subscription_id,
            )
            return {"applied": False, "reason": "replay"}

        ctx = _webhook_audit_ctx()

        # 1. Flip the subscription to inactive + canceled.
        before_status = sub.status
        sub.status = STATUS_CANCELED
        sub.active = False
        sub.canceled_at = _ts(data_object.get("canceled_at")) or datetime.now(timezone.utc)
        sub.last_event_id = event_id
        sub.provider_snapshot = dict(data_object)

        audit_repo = AdminAuditRepository(self.db)
        audit_repo.record(
            ctx=ctx,
            tenant_id=sub.tenant_id,
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
            note="stripe customer.subscription.deleted -> cancel + deactivate tenant cascade",
        )

        # 2. Cascade deactivate the tenant.
        # The cascade method commits if autocommit=True; we pass
        # autocommit=False so the subscription mutation and cascade
        # land in one transaction.
        try:
            from app.repositories.agent_repository import AgentRepository
            from app.services.admin_service import AdminService
            from app.services.luciel_instance_service import LucielInstanceService

            admin = AdminService(self.db)
            agent_repo = AgentRepository(self.db)
            luciel_service = LucielInstanceService(self.db)

            admin.deactivate_tenant_with_cascade(
                sub.tenant_id,
                audit_ctx=ctx,
                luciel_instance_service=luciel_service,
                agent_repo=agent_repo,
                updated_by="stripe_webhook",
                autocommit=False,
            )
        except Exception:
            self.db.rollback()
            logger.exception(
                "billing-webhook: cascade-deactivate failed tenant=%s sub=%s",
                sub.tenant_id, stripe_subscription_id,
            )
            raise

        self.db.commit()
        return {"applied": True, "tenant_id": sub.tenant_id}

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
                tenant_id=sub.tenant_id,
                stripe_subscription_id=stripe_subscription_id,
            )
            return {"applied": False, "reason": "replay"}

        ctx = _webhook_audit_ctx()
        audit_repo = AdminAuditRepository(self.db)
        audit_repo.record(
            ctx=ctx,
            tenant_id=sub.tenant_id,
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
        return {"applied": True, "tenant_id": sub.tenant_id}

    # -----------------------------------------------------------------
    # Replay + unknown event recording
    # -----------------------------------------------------------------

    def _record_replay(self, *, event_id: str, tenant_id: str, stripe_subscription_id: str) -> None:
        ctx = _webhook_audit_ctx()
        audit_repo = AdminAuditRepository(self.db)
        audit_repo.record(
            ctx=ctx,
            tenant_id=tenant_id,
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
        # The tenant_id for an unknown event is unknowable; use the
        # 'platform' sentinel reserved by AdminAuditRepository.
        audit_repo.record(
            ctx=ctx,
            tenant_id="platform",
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
        # checkout.session shape -- no items inline; we fall back to
        # the configured price (Step 30a is single-SKU).
        return settings.stripe_price_individual or None

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
