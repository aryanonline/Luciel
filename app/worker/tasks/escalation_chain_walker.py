"""Enterprise escalation chain walker — §3.5.4 SLA advance Celery task.

Invoked by EscalationDeliveryService._enqueue_chain_advance after
notifying the first chain step. Fires after the SLA window elapses.

Behaviour per §3.5.4:
  1. Poll the escalation_events row for ack (delivery_status='acked').
     If acked → record escalation_acked audit + stop.
  2. If not acked → advance to the next chain step, notify it, record
     escalation_chain_step audit, enqueue the next SLA task.
  3. If chain is exhausted → owner-email fallback, record
     escalation_chain_end_fallback audit.

This task is best-effort: any exception is caught, logged, and the
task does NOT retry automatically (permanent failure posture). The
customer reply has already been sent before this task runs.

Idempotency: if delivery_status='acked' at task execution time the task
is a no-op (the chain was already resolved by an explicit ack).

Schedule: NOT beat-scheduled. Enqueued by EscalationDeliveryService with
a countdown equal to the step's SLA window (default 5 minutes = 300 s).
"""
from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    name="app.worker.tasks.escalation_chain_walker.advance_escalation_chain",
    bind=True,
    max_retries=0,  # Best-effort: no auto-retry (customer reply already sent).
    ignore_result=True,
)
def advance_escalation_chain(
    self,
    *,
    event_id: int,
    admin_id: str,
    luciel_instance_id: int | None,
    session_id: str,
    signal: str,
    gate: str,
    current_step: int,
    chain: list[dict],
    email_to: str | None,
    subject: str,
    body: str,
) -> None:
    """Advance the Enterprise escalation chain after the SLA window.

    Called with countdown=sla_seconds from EscalationDeliveryService.
    """
    try:
        _advance(
            event_id=event_id,
            admin_id=admin_id,
            luciel_instance_id=luciel_instance_id,
            session_id=session_id,
            signal=signal,
            gate=gate,
            current_step=current_step,
            chain=chain,
            email_to=email_to,
            subject=subject,
            body=body,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "escalation_chain_walker: unhandled exception event_id=%s "
            "session=%s step=%s exc=%s",
            event_id, session_id, current_step, type(exc).__name__,
        )


def _advance(
    *,
    event_id: int,
    admin_id: str,
    luciel_instance_id: int | None,
    session_id: str,
    signal: str,
    gate: str,
    current_step: int,
    chain: list[dict],
    email_to: str | None,
    subject: str,
    body: str,
) -> None:
    from app.db.session import SessionLocal
    from app.models.escalation_event import EscalationEvent
    from app.models.admin_audit_log import (
        ACTION_ESCALATION_ACKED,
        ACTION_ESCALATION_CHAIN_STEP,
        ACTION_ESCALATION_CHAIN_END_FALLBACK,
        ACTION_ESCALATION_NOTIFICATION_SENT,
        RESOURCE_ESCALATION_EVENT,
    )
    from app.repositories.admin_audit_repository import AdminAuditRepository, AuditContext
    from sqlalchemy import select, update
    from app.notifications.email_notifier import EmailNotificationAdapter
    from app.notifications.sms_notifier import SmsNotificationAdapter
    from app.notifications.slack_notifier import SlackNotificationAdapter
    from app.policy.escalation_routing import NOTIFY_EMAIL, NOTIFY_SMS, NOTIFY_SLACK

    db = SessionLocal()
    try:
        # Check for ack.
        status = db.execute(
            select(EscalationEvent.delivery_status).where(
                EscalationEvent.id == event_id
            )
        ).scalar_one_or_none()

        repo = AdminAuditRepository(db)
        ctx = AuditContext.system(label="escalation_chain_walker")

        if status == "acked":
            # Already acked — no-op. Log only.
            logger.info(
                "escalation chain: already acked event_id=%d session=%s step=%d",
                event_id, session_id, current_step,
            )
            repo.record(
                ctx=ctx,
                admin_id=admin_id,
                action=ACTION_ESCALATION_ACKED,
                resource_type=RESOURCE_ESCALATION_EVENT,
                resource_pk=event_id,
                resource_natural_id=session_id,
                luciel_instance_id=luciel_instance_id,
                after={
                    "signal": signal,
                    "gate": gate,
                    "session_id": session_id,
                    "step": current_step,
                    "acked_by": "dashboard",
                },
                note=f"escalation chain acked at step {current_step}",
            )
            db.commit()
            return

        next_step = current_step + 1

        if next_step >= len(chain):
            # Chain exhausted — owner-email fallback.
            logger.info(
                "escalation chain: exhausted at step=%d event_id=%d session=%s "
                "— owner email fallback",
                current_step, event_id, session_id,
            )
            if email_to:
                adapter = EmailNotificationAdapter()
                adapter.send(
                    to=email_to,
                    subject=f"[CHAIN FALLBACK] {subject}",
                    body=body,
                    signal=signal,
                    session_id=session_id,
                )
            repo.record(
                ctx=ctx,
                admin_id=admin_id,
                action=ACTION_ESCALATION_CHAIN_END_FALLBACK,
                resource_type=RESOURCE_ESCALATION_EVENT,
                resource_pk=event_id,
                resource_natural_id=session_id,
                luciel_instance_id=luciel_instance_id,
                after={
                    "signal": signal,
                    "gate": gate,
                    "session_id": session_id,
                    "chain_length": len(chain),
                    "fallback_email": email_to,
                },
                note=f"escalation chain end fallback step {current_step}",
            )
            db.execute(
                update(EscalationEvent)
                .where(EscalationEvent.id == event_id)
                .values(delivery_status="delivered")
            )
            db.commit()
            return

        # Advance to next step.
        next_contact = chain[next_step]
        next_channel = next_contact.get("channel", NOTIFY_EMAIL)
        next_to = next_contact.get("value") or email_to
        next_sla = next_contact.get("sla_minutes", 5) * 60

        # Select adapter.
        if next_channel == NOTIFY_SMS:
            adapter = SmsNotificationAdapter()
        elif next_channel == NOTIFY_SLACK:
            adapter = SlackNotificationAdapter()
        else:
            adapter = EmailNotificationAdapter()

        result = adapter.send(
            to=next_to,
            subject=subject,
            body=body,
            signal=signal,
            session_id=session_id,
        )

        repo.record(
            ctx=ctx,
            admin_id=admin_id,
            action=ACTION_ESCALATION_CHAIN_STEP,
            resource_type=RESOURCE_ESCALATION_EVENT,
            resource_pk=event_id,
            resource_natural_id=session_id,
            luciel_instance_id=luciel_instance_id,
            after={
                "signal": signal,
                "session_id": session_id,
                "step": next_step,
                "contact": next_to,
                "chain_action": "advanced",
                "sla_seconds": next_sla,
                "prev_step": current_step,
                "timeout_action": "advance",
            },
            note=f"escalation chain step {next_step} (advanced from {current_step})",
        )
        repo.record(
            ctx=ctx,
            admin_id=admin_id,
            action=ACTION_ESCALATION_NOTIFICATION_SENT,
            resource_type=RESOURCE_ESCALATION_EVENT,
            resource_pk=event_id,
            resource_natural_id=session_id,
            luciel_instance_id=luciel_instance_id,
            after={
                "signal": signal,
                "gate": gate,
                "channel": next_channel,
                "to": next_to,
                "sent": result.sent,
                "dry_run": result.dry_run,
                "chain_step": next_step,
                "event_id": event_id,
            },
            note=f"escalation:{signal} Enterprise chain step {next_step}",
        )
        db.commit()

        logger.info(
            "escalation chain: advanced to step=%d event_id=%d session=%s",
            next_step, event_id, session_id,
        )

        # Enqueue the next SLA advance.
        try:
            advance_escalation_chain.apply_async(
                kwargs={
                    "event_id": event_id,
                    "admin_id": admin_id,
                    "luciel_instance_id": luciel_instance_id,
                    "session_id": session_id,
                    "signal": signal,
                    "gate": gate,
                    "current_step": next_step,
                    "chain": chain,
                    "email_to": email_to,
                    "subject": subject,
                    "body": body,
                },
                countdown=next_sla,
                queue="luciel-memory-tasks",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "escalation chain: failed to enqueue next SLA advance "
                "event_id=%s step=%d exc=%s",
                event_id, next_step, type(exc).__name__,
            )

    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
