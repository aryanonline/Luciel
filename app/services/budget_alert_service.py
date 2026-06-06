"""Conversation-budget alert dispatch — Arc 18 (§3.4.1b).

Sends the admin-facing budget notifications that accompany the runtime
budget gate (``app/runtime/orchestrator.py``):

  * **80%** threshold — email only (Pro heads-up).
  * **100%** threshold — email + SMS (paying tiers over cap; conversations
    are billed as overage, never blocked).
  * **Free exhausted** — email only (the Free admin upgrade nudge). The
    end customer never sees this; they get the graceful handoff copy from
    ``app.runtime.budget_ack``.

Tier-shaped channel sets come from ``app.policy.entitlements`` (Vision §7
notify doctrine), so this service does NOT decide WHICH channels fire — it
executes the entitlements decision. Idempotency (each threshold fires once
per billing period) is owned by ``BudgetMeter.mark_alert_fired_once`` at
the call site in the orchestrator; this service performs the dispatch.

Every dispatch is best-effort: a transport failure logs a warning and is
swallowed (mirrors ``EscalationService`` notify posture). The metering
counter and the customer's reply have already happened; an alert is an
observability/courtesy leg, never a transactional one. An
``ACTION_BUDGET_ALERT_SENT`` audit row records the attempt regardless of
transport outcome.

Recipient resolution: the admin's billing contact is the active
``Subscription.customer_email``. Free admins have no Subscription row
(Gap 1 lock), so the Free exhausted alert has no billing email to target;
in that case the dispatch degrades to an audit-only record (the
escalation routing path already logged the tier-shaped notify intent).
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class BudgetAlertService:
    """Dispatches conversation-budget alerts on the tier-shaped channels.

    Stateless; a fresh DB session is opened per call so the service can be
    constructed once and reused (the orchestrator builds one lazily).
    Injectable for tests via the ``email_sender`` / ``sms_sender`` hooks.
    """

    def __init__(
        self,
        *,
        email_sender=None,
        sms_sender=None,
        session_factory=None,
    ) -> None:
        self._email_sender = email_sender
        self._sms_sender = sms_sender
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_budget_alert(
        self,
        *,
        admin_id: str,
        instance_id: Optional[int],
        tier: str,
        threshold: int,
        current: int,
        cap: int,
        exhausted: bool = False,
    ) -> None:
        """Dispatch a single budget alert. Best-effort: never raises.

        ``threshold`` is the crossed percentage (80 or 100). ``exhausted``
        flags the Free at-cap graceful-handoff notification (admin upgrade
        nudge), which always uses the 100-style email subject.
        """
        try:
            self._dispatch(
                admin_id=admin_id,
                instance_id=instance_id,
                tier=tier,
                threshold=threshold,
                current=current,
                cap=cap,
                exhausted=exhausted,
            )
        except Exception as exc:  # noqa: BLE001 — alert is best-effort
            logger.warning(
                "budget alert dispatch failed: exc_class=%s tier=%s "
                "threshold=%s admin_prefix=%s",
                type(exc).__name__,
                tier,
                threshold,
                (admin_id or "")[:8],
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _open_session(self):
        if self._session_factory is not None:
            return self._session_factory()
        from app.db.session import SessionLocal

        return SessionLocal()

    def _dispatch(
        self,
        *,
        admin_id: str,
        instance_id: Optional[int],
        tier: str,
        threshold: int,
        current: int,
        cap: int,
        exhausted: bool,
    ) -> None:
        from app.policy.entitlements import (
            ESCALATION_NOTIFY_SMS,
            TIER_FREE,
            budget_alert_channels,
        )

        channels = budget_alert_channels(tier, threshold)
        recipient, instance_label = self._resolve_recipient(
            admin_id=admin_id, instance_id=instance_id
        )

        email_ok = False
        sms_ok = False

        # Email leg — every alert level includes email.
        if recipient:
            email_ok = self._send_email(
                to_email=recipient,
                threshold=threshold,
                current=current,
                cap=cap,
                instance_label=instance_label,
                exhausted=exhausted,
            )
        else:
            logger.info(
                "budget alert: no billing email for admin_prefix=%s "
                "(tier=%s) — audit-only record",
                (admin_id or "")[:8],
                tier,
            )

        # Unit 1 excision: CSM CC removed (Enterprise deferred; budget_csm_alert_at_80 deleted).

        # SMS leg — only when the tier-shaped channel set includes SMS
        # (100% for paying tiers). Recipient SMS number resolution is a
        # later transport unit; we log the intent without claiming a send.
        if ESCALATION_NOTIFY_SMS in channels:
            sms_ok = self._send_sms(
                admin_id=admin_id,
                threshold=threshold,
                current=current,
                cap=cap,
                instance_label=instance_label,
            )

        self._record_alert_audit(
            admin_id=admin_id,
            instance_id=instance_id,
            tier=tier,
            threshold=threshold,
            current=current,
            cap=cap,
            channels=channels,
            exhausted=exhausted,
            email_ok=email_ok,
            sms_ok=sms_ok,
        )

    def _resolve_recipient(
        self, *, admin_id: str, instance_id: Optional[int]
    ) -> tuple[Optional[str], Optional[str]]:
        """Return (billing_email, instance_display_name). Billing email is
        the active Subscription's customer_email; Free admins have none."""
        db = self._open_session()
        try:
            from sqlalchemy import select

            from app.models.instance import Instance
            from app.models.subscription import Subscription

            email = db.execute(
                select(Subscription.customer_email)
                .where(
                    Subscription.admin_id == admin_id,
                    Subscription.active.is_(True),
                )
                .order_by(Subscription.id.desc())
            ).scalars().first()

            label: Optional[str] = None
            if instance_id is not None:
                label = db.execute(
                    select(Instance.display_name).where(
                        Instance.id == instance_id,
                        Instance.admin_id == admin_id,
                    )
                ).scalar_one_or_none()
            return email, label
        finally:
            db.close()

    def _send_email(
        self,
        *,
        to_email: str,
        threshold: int,
        current: int,
        cap: int,
        instance_label: Optional[str],
        exhausted: bool,
    ) -> bool:
        sender = self._email_sender
        if sender is None:
            from app.services.email_service import send_budget_alert_email

            sender = send_budget_alert_email
        try:
            sender(
                to_email=to_email,
                threshold=threshold,
                current=current,
                cap=cap,
                instance_label=instance_label,
                exhausted=exhausted,
            )
            return True
        except Exception as exc:  # noqa: BLE001 — courtesy leg
            logger.warning(
                "budget alert email failed: exc_class=%s to=%s threshold=%s",
                type(exc).__name__,
                to_email,
                threshold,
            )
            return False

    # _send_csm_copy removed (Unit 1 excision) — Enterprise CSM deferred.

    def _send_sms(
        self,
        *,
        admin_id: str,
        threshold: int,
        current: int,
        cap: int,
        instance_label: Optional[str],
    ) -> bool:
        """SMS leg at 100%. The admin-SMS contact surface is owned by a
        later transport unit (the customer-facing SmsChannelAdapter is
        instance-routing, not admin-notification). Log the live intent
        without claiming a send we cannot make — mirrors
        EscalationService._maybe_notify."""
        if self._sms_sender is not None:
            try:
                self._sms_sender(
                    admin_id=admin_id,
                    threshold=threshold,
                    current=current,
                    cap=cap,
                    instance_label=instance_label,
                )
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "budget alert SMS failed: exc_class=%s threshold=%s",
                    type(exc).__name__,
                    threshold,
                )
                return False
        logger.info(
            "budget alert SMS intent (no admin-SMS transport wired): "
            "admin_prefix=%s threshold=%s current=%s cap=%s",
            (admin_id or "")[:8],
            threshold,
            current,
            cap,
        )
        return False

    def _record_alert_audit(
        self,
        *,
        admin_id: str,
        instance_id: Optional[int],
        tier: str,
        threshold: int,
        current: int,
        cap: int,
        channels,
        exhausted: bool,
        email_ok: bool,
        sms_ok: bool,
    ) -> None:
        try:
            from app.models.admin_audit_log import (
                ACTION_BUDGET_ALERT_SENT,
                RESOURCE_INSTANCE,
            )
            from app.repositories.admin_audit_repository import (
                AdminAuditRepository,
                AuditContext,
            )

            db = self._open_session()
            try:
                AdminAuditRepository(db).record(
                    ctx=AuditContext.system(label="budget_alert"),
                    admin_id=admin_id,
                    action=ACTION_BUDGET_ALERT_SENT,
                    resource_type=RESOURCE_INSTANCE,
                    resource_pk=instance_id,
                    luciel_instance_id=instance_id,
                    after={
                        "tier": tier,
                        "threshold_pct": threshold,
                        "current": current,
                        "cap": cap,
                        "channels": sorted(channels),
                        "exhausted": exhausted,
                        "email_sent": email_ok,
                        "sms_sent": sms_ok,
                    },
                    note=f"Budget alert {threshold}% (tier={tier}).",
                    autocommit=True,
                )
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "budget alert audit write failed: exc_class=%s threshold=%s",
                type(exc).__name__,
                threshold,
            )
