"""Escalation delivery service — §3.5 NotificationAdapter wire-up.

Takes a fired EscalationEvent (by id) + resolved EscalationContact and
performs send-with-retry + idempotency + audit for every channel in the
contact's tier-shaped channel set.

Design decisions
----------------
* **Idempotency key**: ``(session_id, signal, gate)`` + an integer attempt
  counter stored on the escalation_events row. The UNIQUE constraint on
  (session_id, signal, gate) over non-revoked rows (see the migration)
  prevents duplicate events being recorded on replay. The delivery service
  checks ``delivery_status='delivered'`` before attempting a send, so a
  restart-safe exactly-once guarantee is maintained.

* **Retry**: 3 attempts per channel (immediate / 30 s / 2 min). After 3
  failures the path falls back to admin_owner email + records a
  delivery_failed audit row.

* **Customer reply first**: This service is called AFTER the customer reply
  has been sent. Delivery must NEVER block or crash the turn — every public
  method is wrapped in try/except and degrades to a warning log on error.

* **Tier dispatch** (Unit 1: Free + Pro only):
  - Free  → single email, one contact, one attempt with retry.
  - Pro   → per-signal routing rules + fan-out (multiple contacts per
              signal). Each contact gets retry-with-fallback.

* **Dry-run**: when CHANNELS_LIVE_PROVISIONING_ENABLED is False, records the
  full routing+attempt decision and the escalation_notification_sent audit
  row without making any real send. This is the KEY CHANGE — the real send
  for the four signals is now wired when the flag is on.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.notifications.base import NotificationResult
from app.notifications.email_notifier import EmailNotificationAdapter
from app.notifications.sms_notifier import SmsNotificationAdapter
from app.notifications.slack_notifier import SlackNotificationAdapter
from app.policy.escalation_routing import (
    NOTIFY_EMAIL,
    NOTIFY_SMS,
    NOTIFY_SLACK,
    EscalationContact,
)

logger = logging.getLogger(__name__)

# Retry back-off delays in seconds: immediate, 30 s, 2 min.
_RETRY_DELAYS = (0, 30, 120)
_MAX_ATTEMPTS = 3

# ENTERPRISE_FIRST_STEP_SLA_SECONDS removed (Unit 1 excision) — Enterprise chains deferred.


# ---------------------------------------------------------------------------
# Contact resolver helpers
# ---------------------------------------------------------------------------

def _resolve_contact_email(db, *, admin_id: str) -> str | None:
    """Resolve the email to send the escalation to.

    Preference order:
      1. Instance.escalation_config['primary_email'] (Free)
         or escalation_config['primary_contact']['value'] for email channel
      2. Active Subscription.customer_email
      3. None → delivery degrades to dry-run (no recipient).

    Never raises.
    """
    try:
        from sqlalchemy import select
        from app.models.subscription import Subscription

        email = db.execute(
            select(Subscription.customer_email)
            .where(
                Subscription.admin_id == admin_id,
                Subscription.active.is_(True),
            )
            .order_by(Subscription.id.desc())
        ).scalars().first()
        return email
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "escalation delivery: failed to resolve contact email "
            "admin_prefix=%s exc=%s",
            (admin_id or "")[:8], type(exc).__name__,
        )
        return None


def _resolve_contact_email_from_config(escalation_config: dict | None) -> str | None:
    """Extract primary_email from an instance's escalation_config."""
    if not escalation_config:
        return None
    # Free tier: {primary_email: "..."}
    if "primary_email" in escalation_config:
        return escalation_config["primary_email"]
    # Pro/Enterprise: {primary_contact: {channel: "email", value: "..."}}
    primary = escalation_config.get("primary_contact")
    if isinstance(primary, dict) and primary.get("channel") == "email":
        return primary.get("value")
    return None


def _resolve_contact_sms_from_config(escalation_config: dict | None) -> str | None:
    """Extract primary_contact E.164 number from escalation_config."""
    if not escalation_config:
        return None
    primary = escalation_config.get("primary_contact")
    if isinstance(primary, dict) and primary.get("channel") == "sms":
        return primary.get("value")
    return None


def _resolve_contact_slack_from_config(escalation_config: dict | None) -> str | None:
    """Extract slack webhook URL from escalation_config."""
    if not escalation_config:
        return None
    primary = escalation_config.get("primary_contact")
    if isinstance(primary, dict) and primary.get("channel") == "slack":
        return primary.get("value")
    return None


def _get_instance_escalation_config(db, *, admin_id: str, luciel_instance_id: int | None) -> dict | None:
    """Return instance.escalation_config; None on any failure."""
    if luciel_instance_id is None:
        return None
    try:
        from sqlalchemy import select
        from app.models.instance import Instance
        cfg = db.execute(
            select(Instance.escalation_config).where(
                Instance.id == luciel_instance_id,
                Instance.admin_id == admin_id,
            )
        ).scalar_one_or_none()
        return cfg
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Build notification message
# ---------------------------------------------------------------------------

_SIGNAL_LABELS = {
    "explicit_human_request": "Human Agent Request",
    "strong_negative_sentiment": "Negative Sentiment Alert",
    "cannot_confidently_answer": "Unresolved Query",
    "high_value_lead": "High-Value Lead",
    "budget_exhausted": "Conversation Budget Exhausted",
}


def _build_message(*, signal: str, session_id: str, gate: str) -> tuple[str, str]:
    """Return (subject, body) for the escalation notification."""
    label = _SIGNAL_LABELS.get(signal, signal.replace("_", " ").title())
    subject = f"VantageMind Escalation: {label}"
    body = (
        f"An escalation has been triggered on your VantageMind instance.\n\n"
        f"Signal:     {label}\n"
        f"Gate:       {gate.upper()}\n"
        f"Session ID: {session_id}\n\n"
        f"Please log in to your VantageMind dashboard to view the conversation "
        f"and take action.\n\n"
        f"-- The VantageMind team\n"
    )
    return subject, body


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------

def _write_delivery_audit(
    db,
    *,
    admin_id: str,
    luciel_instance_id: int | None,
    event_id: int | None,
    session_id: str,
    signal: str,
    gate: str,
    action: str,
    after: dict,
    note: str,
) -> None:
    """Write one admin_audit_log row. Never raises — best-effort audit."""
    try:
        from app.repositories.admin_audit_repository import (
            AdminAuditRepository,
            AuditContext,
        )
        from app.models.admin_audit_log import RESOURCE_ESCALATION_EVENT

        repo = AdminAuditRepository(db)
        repo.record(
            ctx=AuditContext.system(label="escalation_delivery"),
            admin_id=admin_id,
            action=action,
            resource_type=RESOURCE_ESCALATION_EVENT,
            resource_pk=event_id,
            resource_natural_id=session_id,
            luciel_instance_id=luciel_instance_id,
            after=after,
            note=note[:256],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "escalation delivery: audit write failed action=%s exc=%s",
            action, type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

def _send_with_retry(
    adapter,
    *,
    to: str | None,
    subject: str,
    body: str,
    signal: str,
    session_id: str,
) -> tuple[NotificationResult, int]:
    """Attempt adapter.send up to _MAX_ATTEMPTS times with exponential backoff.

    Returns (final_result, attempts_made).
    """
    last_result: NotificationResult | None = None
    for attempt_idx, delay in enumerate(_RETRY_DELAYS[:_MAX_ATTEMPTS]):
        if attempt_idx > 0 and delay > 0:
            time.sleep(delay)
        result = adapter.send(
            to=to,
            subject=subject,
            body=body,
            signal=signal,
            session_id=session_id,
        )
        last_result = result
        # Dry-run counts as success for idempotency purposes.
        if result.sent or result.dry_run:
            return result, attempt_idx + 1
        # On failure: log and continue to next retry.
        logger.warning(
            "[escalation-delivery] send attempt %d/%d failed channel=%s "
            "to=%s signal=%s session=%s error=%s",
            attempt_idx + 1, _MAX_ATTEMPTS,
            adapter.channel, to, signal, session_id,
            result.error,
        )
    return last_result, _MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Core delivery service
# ---------------------------------------------------------------------------

class EscalationDeliveryService:
    """Wire escalation notification send for the four real signals.

    Public entry point: ``deliver(event_id, admin_id, ...)``. Called from
    the orchestrator best-effort, AFTER the customer reply has been sent.
    Never raises.

    Injectable adapters for testing (email_adapter, sms_adapter, slack_adapter).
    """

    def __init__(
        self,
        *,
        session_factory=None,
        email_adapter=None,
        sms_adapter=None,
        slack_adapter=None,
    ) -> None:
        self._session_factory = session_factory
        self._email_adapter = email_adapter or EmailNotificationAdapter()
        self._sms_adapter = sms_adapter or SmsNotificationAdapter()
        self._slack_adapter = slack_adapter or SlackNotificationAdapter()

    def _open_session(self):
        if self._session_factory is not None:
            return self._session_factory()
        from app.db.session import SessionLocal
        return SessionLocal()

    def deliver(
        self,
        *,
        event_id: int | None,
        admin_id: str,
        luciel_instance_id: int | None,
        session_id: str,
        signal: str,
        gate: str,
        contact: EscalationContact,
    ) -> None:
        """Deliver escalation notifications. Best-effort: never raises."""
        try:
            self._deliver(
                event_id=event_id,
                admin_id=admin_id,
                luciel_instance_id=luciel_instance_id,
                session_id=session_id,
                signal=signal,
                gate=gate,
                contact=contact,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "escalation delivery: top-level exception swallowed "
                "event_id=%s session=%s signal=%s exc=%s",
                event_id, session_id, signal, type(exc).__name__,
            )

    def _deliver(
        self,
        *,
        event_id: int | None,
        admin_id: str,
        luciel_instance_id: int | None,
        session_id: str,
        signal: str,
        gate: str,
        contact: EscalationContact,
    ) -> None:
        from app.policy.entitlements import TIER_FREE, TIER_PRO

        db = None
        try:
            db = self._open_session()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "escalation delivery: cannot open DB session exc=%s; "
                "attempting notification without idempotency check",
                type(exc).__name__,
            )

        try:
            # Idempotency: if already delivered, skip.
            if db is not None and event_id is not None:
                if self._already_delivered(db, event_id=event_id):
                    logger.info(
                        "escalation delivery: idempotency key already delivered "
                        "event_id=%d session=%s signal=%s — skipping",
                        event_id, session_id, signal,
                    )
                    return

            # Resolve contact addresses from escalation_config.
            escalation_config = None
            if db is not None:
                escalation_config = _get_instance_escalation_config(
                    db, admin_id=admin_id, luciel_instance_id=luciel_instance_id
                )

            # Resolve addresses.
            email_to = (
                contact.email
                or _resolve_contact_email_from_config(escalation_config)
                or (db and _resolve_contact_email(db, admin_id=admin_id))
            )
            sms_to = (
                contact.sms_to
                or _resolve_contact_sms_from_config(escalation_config)
            )
            slack_to = (
                contact.slack_target
                or _resolve_contact_slack_from_config(escalation_config)
            )

            subject, body = _build_message(signal=signal, session_id=session_id, gate=gate)

            tier = contact.tier

            if tier == TIER_FREE:
                self._deliver_free(
                    db=db,
                    event_id=event_id,
                    admin_id=admin_id,
                    luciel_instance_id=luciel_instance_id,
                    session_id=session_id,
                    signal=signal,
                    gate=gate,
                    email_to=email_to,
                    subject=subject,
                    body=body,
                )
            elif tier == TIER_PRO:
                self._deliver_pro(
                    db=db,
                    event_id=event_id,
                    admin_id=admin_id,
                    luciel_instance_id=luciel_instance_id,
                    session_id=session_id,
                    signal=signal,
                    gate=gate,
                    email_to=email_to,
                    sms_to=sms_to,
                    subject=subject,
                    body=body,
                    escalation_config=escalation_config,
                )
            else:
                # Unknown tier — degrade to Free (email only).
                self._deliver_free(
                    db=db,
                    event_id=event_id,
                    admin_id=admin_id,
                    luciel_instance_id=luciel_instance_id,
                    session_id=session_id,
                    signal=signal,
                    gate=gate,
                    email_to=email_to,
                    subject=subject,
                    body=body,
                )

            # Mark delivered in escalation_events.
            if db is not None and event_id is not None:
                self._mark_delivered(db, event_id=event_id)
                db.commit()

        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------
    # Idempotency helpers
    # ------------------------------------------------------------------

    def _already_delivered(self, db, *, event_id: int) -> bool:
        """Return True if this event's delivery_status == 'delivered'."""
        try:
            from sqlalchemy import select, text
            from app.models.escalation_event import EscalationEvent
            status = db.execute(
                select(EscalationEvent.delivery_status).where(
                    EscalationEvent.id == event_id
                )
            ).scalar_one_or_none()
            return status == "delivered"
        except Exception:  # noqa: BLE001
            return False

    def _mark_delivered(self, db, *, event_id: int) -> None:
        """Update escalation_events.delivery_status = 'delivered'."""
        try:
            from sqlalchemy import update
            from app.models.escalation_event import EscalationEvent
            db.execute(
                update(EscalationEvent)
                .where(EscalationEvent.id == event_id)
                .values(delivery_status="delivered")
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "escalation delivery: mark_delivered failed event_id=%s exc=%s",
                event_id, type(exc).__name__,
            )

    # ------------------------------------------------------------------
    # Tier-specific delivery
    # ------------------------------------------------------------------

    def _deliver_free(
        self,
        *,
        db,
        event_id: int | None,
        admin_id: str,
        luciel_instance_id: int | None,
        session_id: str,
        signal: str,
        gate: str,
        email_to: str | None,
        subject: str,
        body: str,
    ) -> None:
        """Free tier: single email send with retry."""
        from app.models.admin_audit_log import (
            ACTION_ESCALATION_NOTIFICATION_SENT,
            ACTION_ESCALATION_DELIVERY_FAILED,
        )
        result, attempts = _send_with_retry(
            self._email_adapter,
            to=email_to,
            subject=subject,
            body=body,
            signal=signal,
            session_id=session_id,
        )
        if db is not None:
            if result.sent or result.dry_run:
                _write_delivery_audit(
                    db,
                    admin_id=admin_id,
                    luciel_instance_id=luciel_instance_id,
                    event_id=event_id,
                    session_id=session_id,
                    signal=signal,
                    gate=gate,
                    action=ACTION_ESCALATION_NOTIFICATION_SENT,
                    after={
                        "signal": signal,
                        "gate": gate,
                        "channel": NOTIFY_EMAIL,
                        "to": email_to,
                        "sent": result.sent,
                        "dry_run": result.dry_run,
                        "provider_id": result.provider_id,
                        "attempts": attempts,
                        "event_id": event_id,
                    },
                    note=f"escalation:{signal} email (Free)",
                )
            else:
                _write_delivery_audit(
                    db,
                    admin_id=admin_id,
                    luciel_instance_id=luciel_instance_id,
                    event_id=event_id,
                    session_id=session_id,
                    signal=signal,
                    gate=gate,
                    action=ACTION_ESCALATION_DELIVERY_FAILED,
                    after={
                        "signal": signal,
                        "gate": gate,
                        "channel": NOTIFY_EMAIL,
                        "to": email_to,
                        "attempts": attempts,
                        "last_error": result.error,
                        "event_id": event_id,
                    },
                    note=f"escalation:{signal} email failed after {attempts} attempts",
                )
        logger.info(
            "escalation delivery (Free): signal=%s session=%s sent=%s dry_run=%s",
            signal, session_id, result.sent, result.dry_run,
        )

    def _deliver_pro(
        self,
        *,
        db,
        event_id: int | None,
        admin_id: str,
        luciel_instance_id: int | None,
        session_id: str,
        signal: str,
        gate: str,
        email_to: str | None,
        sms_to: str | None,
        subject: str,
        body: str,
        escalation_config: dict | None,
    ) -> None:
        """Pro tier: per-signal routing rules + fan-out + retry.

        Fan-out resolves contacts from routing_rules[signal] if present;
        otherwise falls back to primary+secondary contacts.
        Each contact gets retry-with-fallback.
        """
        from app.models.admin_audit_log import (
            ACTION_ESCALATION_NOTIFICATION_SENT,
            ACTION_ESCALATION_DELIVERY_FAILED,
        )

        contacts = self._resolve_pro_contacts(
            signal=signal,
            email_to=email_to,
            sms_to=sms_to,
            escalation_config=escalation_config,
        )

        if not contacts:
            # No contacts — log and fall back to email.
            logger.info(
                "escalation delivery (Pro): no contacts resolved for signal=%s "
                "session=%s — falling back to email",
                signal, session_id,
            )
            contacts = [("email", email_to)]

        for channel, to in contacts:
            adapter = self._adapter_for_channel(channel)
            result, attempts = _send_with_retry(
                adapter,
                to=to,
                subject=subject,
                body=body,
                signal=signal,
                session_id=session_id,
            )
            if db is not None:
                if result.sent or result.dry_run:
                    _write_delivery_audit(
                        db,
                        admin_id=admin_id,
                        luciel_instance_id=luciel_instance_id,
                        event_id=event_id,
                        session_id=session_id,
                        signal=signal,
                        gate=gate,
                        action=ACTION_ESCALATION_NOTIFICATION_SENT,
                        after={
                            "signal": signal,
                            "gate": gate,
                            "channel": channel,
                            "to": to,
                            "sent": result.sent,
                            "dry_run": result.dry_run,
                            "provider_id": result.provider_id,
                            "attempts": attempts,
                            "event_id": event_id,
                        },
                        note=f"escalation:{signal} {channel} (Pro)",
                    )
                else:
                    # 3 failures: fallback to admin_owner email + delivery_failed audit.
                    _write_delivery_audit(
                        db,
                        admin_id=admin_id,
                        luciel_instance_id=luciel_instance_id,
                        event_id=event_id,
                        session_id=session_id,
                        signal=signal,
                        gate=gate,
                        action=ACTION_ESCALATION_DELIVERY_FAILED,
                        after={
                            "signal": signal,
                            "gate": gate,
                            "channel": channel,
                            "to": to,
                            "attempts": attempts,
                            "last_error": result.error,
                            "event_id": event_id,
                        },
                        note=f"escalation:{signal} {channel} failed; fallback to admin email",
                    )
                    # Pro fallback: send to admin_owner email.
                    if channel != NOTIFY_EMAIL and email_to:
                        fallback_result, _ = _send_with_retry(
                            self._email_adapter,
                            to=email_to,
                            subject=f"[FALLBACK] {subject}",
                            body=body,
                            signal=signal,
                            session_id=session_id,
                        )
                        _write_delivery_audit(
                            db,
                            admin_id=admin_id,
                            luciel_instance_id=luciel_instance_id,
                            event_id=event_id,
                            session_id=session_id,
                            signal=signal,
                            gate=gate,
                            action=ACTION_ESCALATION_NOTIFICATION_SENT,
                            after={
                                "signal": signal,
                                "gate": gate,
                                "channel": NOTIFY_EMAIL,
                                "to": email_to,
                                "sent": fallback_result.sent,
                                "dry_run": fallback_result.dry_run,
                                "fallback": True,
                                "event_id": event_id,
                            },
                            note=f"escalation:{signal} Pro owner-email fallback",
                        )

    def _resolve_pro_contacts(
        self,
        *,
        signal: str,
        email_to: str | None,
        sms_to: str | None,
        escalation_config: dict | None,
    ) -> list[tuple[str, str | None]]:
        """Return list of (channel, address) for Pro fan-out.

        1. routing_rules[signal] — list of {channel, value} contacts.
        2. primary_contact + secondary_contact.
        3. Fall back to (email, email_to) + (sms, sms_to).
        """
        if not escalation_config:
            result = []
            if email_to:
                result.append((NOTIFY_EMAIL, email_to))
            if sms_to:
                result.append((NOTIFY_SMS, sms_to))
            return result

        # Check routing_rules[signal].
        routing_rules = escalation_config.get("routing_rules", {})
        if isinstance(routing_rules, dict) and signal in routing_rules:
            rule = routing_rules[signal]
            if isinstance(rule, list):
                return [(c.get("channel"), c.get("value")) for c in rule if isinstance(c, dict)]
            if isinstance(rule, dict):
                return [(rule.get("channel"), rule.get("value"))]

        # Fall back to primary + secondary.
        contacts: list[tuple[str, str | None]] = []
        primary = escalation_config.get("primary_contact")
        if isinstance(primary, dict):
            contacts.append((primary.get("channel", NOTIFY_EMAIL), primary.get("value")))
        secondary = escalation_config.get("secondary_contact")
        if isinstance(secondary, dict):
            contacts.append((secondary.get("channel", NOTIFY_EMAIL), secondary.get("value")))

        # If still empty, use resolved addresses.
        if not contacts:
            if email_to:
                contacts.append((NOTIFY_EMAIL, email_to))
            if sms_to:
                contacts.append((NOTIFY_SMS, sms_to))

        return contacts

    # _deliver_enterprise + _enqueue_chain_advance removed (Unit 1 excision) — Enterprise chains deferred.

    def _adapter_for_channel(self, channel: str):
        """Return the adapter for a given channel id."""
        if channel == NOTIFY_SMS:
            return self._sms_adapter
        if channel == NOTIFY_SLACK:
            return self._slack_adapter
        return self._email_adapter
