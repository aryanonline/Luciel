"""Escalation service.

Arc 14 U2 (§3.4.5) extends the original log-only MVP stub into the real
escalation flow WITHOUT forking it:

  * the legacy ``handle_escalation(session_id, user_id, admin_id, reason)``
    entry point is PRESERVED verbatim (the cognition path in
    ``app/cognition/service.py`` still calls it and still gets a logged
    side-effect — no behaviour change for that caller);

  * a new ``record_escalation(decision, ...)`` entry point is the §3.4.5
    flow the orchestrator's escalation gates call. It does three things,
    best-effort and in one DB transaction:
       1. EVENT STORE — write one ``escalation_events`` row capturing
          which signal fired, its confidence, a model-reasoning excerpt,
          the raw inputs, and the (admin, instance, session) scope.
       2. TIER ROUTING — resolve the tier-shaped admin-notification
          channel set (Free→email; Pro→email+SMS; Enterprise→
          email+SMS+Slack+custom). The ACTUAL send is gated behind the
          ``channels_live_provisioning_enabled`` live-switch so tests
          never send real email/SMS — the ROUTING DECISION is what this
          unit produces and records.
       3. AUDIT — write one ``admin_audit_log`` row
          (``ACTION_ESCALATION_FIRED``) in the same transaction (§5.1).

Doctrine: escalation must never crash the turn. Every leg is wrapped so
a DB/notify failure degrades to a warning rather than propagating — the
orchestrator has already decided to escalate; the side-effects are
observability + delivery, not control flow.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EscalationDecision:
    """The §3.4.5 judge's decision to escalate, ready for recording.

    Carries everything the event store + audit row need. ``gate`` and
    ``signal`` use the doctrinal vocabularies from
    ``app.models.escalation_event``. ``signal_inputs`` is the raw
    evidence the judge evaluated (message, classifier outputs, loop
    confidence, grounding, retrieval-failure flag, etc.).
    """

    signal: str
    gate: str
    admin_id: str
    session_id: str
    luciel_instance_id: int | None = None
    user_id: str | None = None
    signal_confidence: float | None = None
    reasoning_excerpt: str | None = None
    signal_inputs: dict = field(default_factory=dict)


@dataclass
class EscalationRouting:
    """The routing decision produced for an escalation: which admin-
    notification channels the tier entitles, and whether the real send
    leg was attempted (gated by the live-switch). Returned by
    ``record_escalation`` so a caller / test can assert the decision
    without inspecting the DB or sending anything."""

    tier: str
    channels: tuple[str, ...]
    notified_live: bool = False
    event_id: int | None = None


class EscalationService:
    """Processes escalation events after the §3.4.5 judge decides one is
    needed."""

    def __init__(self, *, session_factory=None, audit_context=None) -> None:
        # Injectable so tests can substitute a session factory / audit
        # context; lazily built so the legacy no-arg construction in
        # cognition/service.py keeps working unchanged.
        self._session_factory = session_factory
        self._audit_context = audit_context

    # ------------------------------------------------------------------
    # Legacy entry point — PRESERVED verbatim (cognition path).
    # ------------------------------------------------------------------

    def handle_escalation(
        self,
        *,
        session_id: str,
        user_id: str | None,
        admin_id: str,
        reason: str,
    ) -> None:
        """Process an escalation event (legacy MVP behaviour).

        Still log-only so the pre-Arc-14 cognition call site is
        unchanged. The §3.4.5 runtime path uses ``record_escalation``.
        """
        logger.warning(
            "ESCALATION | session=%s | user=%s | tenant=%s | reason=%s",
            session_id,
            user_id,
            admin_id,
            reason,
        )

    # ------------------------------------------------------------------
    # Arc 14 U2 — the §3.4.5 flow: event store + tier routing + audit.
    # ------------------------------------------------------------------

    def record_escalation(self, decision: EscalationDecision) -> EscalationRouting:
        """Persist the escalation event, resolve tier routing, audit it.

        Best-effort: never raises. Returns the routing decision (tier +
        channel set + whether a live send was attempted + the event id
        if the row was written).
        """
        # Always resolve the legacy log line too, so existing log-based
        # alerting keeps firing.
        self.handle_escalation(
            session_id=decision.session_id,
            user_id=decision.user_id,
            admin_id=decision.admin_id,
            reason=decision.signal,
        )

        from app.policy.escalation_routing import resolve_contact

        db = None
        try:
            db = self._open_session()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "escalation: could not open DB session: exc_class=%s — "
                "routing decision computed without persistence",
                type(exc).__name__,
            )

        if db is None:
            # No DB: still produce the routing decision (tier defaults to
            # Free without a lookup) so the caller sees a coherent result.
            contact = resolve_contact(
                _NullDB(), admin_id=decision.admin_id,
                luciel_instance_id=decision.luciel_instance_id,
            )
            routing = EscalationRouting(tier=contact.tier, channels=contact.channels)
            routing.notified_live = self._maybe_notify(decision, contact=contact)
            return routing

        # Resolve routing first so a later persistence failure still
        # leaves us with the tier-shaped channel set to return + notify.
        contact = resolve_contact(
            db,
            admin_id=decision.admin_id,
            luciel_instance_id=decision.luciel_instance_id,
        )
        routing = EscalationRouting(tier=contact.tier, channels=contact.channels)
        try:
            event_id = self._write_event(db, decision)
            self._write_audit(db, decision, contact=contact, event_id=event_id)
            db.commit()
            routing.event_id = event_id
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "escalation: persistence failed: exc_class=%s — rolling back",
                type(exc).__name__,
            )
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass

        # Notify leg — gated behind the live-switch. Tests assert the
        # ROUTING DECISION (routing.channels), not real delivery.
        routing.notified_live = self._maybe_notify(decision, contact=contact)
        return routing

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _open_session(self):
        if self._session_factory is not None:
            return self._session_factory()
        from app.db.session import SessionLocal

        return SessionLocal()

    def _write_event(self, db, decision: EscalationDecision) -> int | None:
        from app.models.escalation_event import EscalationEvent

        row = EscalationEvent(
            admin_id=decision.admin_id,
            luciel_instance_id=decision.luciel_instance_id,
            session_id=decision.session_id,
            user_id=decision.user_id,
            signal=decision.signal,
            gate=decision.gate,
            signal_confidence=decision.signal_confidence,
            reasoning_excerpt=decision.reasoning_excerpt,
            signal_inputs=decision.signal_inputs or None,
        )
        db.add(row)
        db.flush()
        return row.id

    def _write_audit(self, db, decision: EscalationDecision, *, contact, event_id):
        from app.models.admin_audit_log import (
            ACTION_ESCALATION_FIRED,
            RESOURCE_ESCALATION_EVENT,
        )
        from app.repositories.admin_audit_repository import (
            AdminAuditRepository,
            AuditContext,
        )

        ctx = self._audit_context or AuditContext.system()
        repo = AdminAuditRepository(db)
        repo.record(
            ctx=ctx,
            admin_id=decision.admin_id,
            action=ACTION_ESCALATION_FIRED,
            resource_type=RESOURCE_ESCALATION_EVENT,
            resource_pk=event_id,
            resource_natural_id=decision.session_id,
            luciel_instance_id=decision.luciel_instance_id,
            after={
                "signal": decision.signal,
                "gate": decision.gate,
                "signal_confidence": decision.signal_confidence,
                "tier": contact.tier,
                "notify_channels": list(contact.channels),
            },
            note=f"escalation:{decision.signal}",
        )

    @staticmethod
    def _maybe_notify(decision: EscalationDecision, *, contact) -> bool:
        """Attempt the real admin-notification send IFF the platform
        live-switch is on. Returns True only when a live send was
        actually attempted. In tests / dev the switch is False so this is
        a no-op and the routing decision is asserted instead."""
        from app.core.config import settings

        if not getattr(settings, "channels_live_provisioning_enabled", False):
            logger.info(
                "escalation notify (dry-run): tier=%s channels=%s session=%s",
                contact.tier, contact.channels, decision.session_id,
            )
            return False

        # Live path: a later unit binds concrete SES/SMS/Slack senders.
        # Until those are wired, log the live intent without claiming a
        # send we cannot make. Kept deliberately minimal — the unit that
        # adds the contact-address surface owns the actual transport.
        logger.warning(
            "escalation notify (LIVE): tier=%s channels=%s session=%s",
            contact.tier, contact.channels, decision.session_id,
        )
        return True


class _NullDB:
    """A do-nothing DB stand-in so ``resolve_contact`` can run its
    fail-closed path (returns Free tier) when no session is available."""

    def execute(self, *_args, **_kwargs):  # pragma: no cover - trivial
        raise RuntimeError("no database session available")
