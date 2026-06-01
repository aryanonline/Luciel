"""Arc 14 U4 — COGNITION FINALIZATION (Half B, step 7 of §3.4.1).

This module is the always-on cognition the agentic loop runs AFTER it
has decided on a reply, on EVERY turn, EVERY tier (§3.4 — not a tool,
not admin-configurable, not tier-gated). It folds together three U4
behaviours plus the pre-existing ``CognitionService`` behaviours:

  * §3.4.4 LEAD CAPTURE — if the conversation crossed the lead threshold
    (``lead_capture.detect``), write ONE structured lead row to the
    dashboard lead view (VantageMind's OWN record — decoupled from the
    ``push_to_crm`` tool, which would later READ this row).
  * §3.4.7 SUMMARIZATION — persist a structured conversation summary
    (``summarizer.summarize``) alongside the lead row.
  * §3.4.6 LIVE HUMAN HANDOFF — when an escalation fired AND a real-time
    human takeover is warranted (the customer asked for a person, or the
    judge decided), assemble a CONTEXT BUNDLE (full transcript +
    structured summary + the captured lead row) and mark the session
    handoff state, delivering the bundle to the escalation notification
    path. "Transfer" in the LOCAL build = mark handoff + hand the bundle
    to EscalationService's routing (the real channel transport is a
    later unit's hook).

Behaviour-equivalence with ``CognitionService`` (§ the fold)
------------------------------------------------------------
``CognitionService`` (escalate / save_memory / get_session_summary)
remains intact and its ``chat_service`` call site is unchanged — the
legacy chat path keeps working verbatim. The orchestrator loop does NOT
go through ``CognitionService``; instead this finalizer reproduces the
two cognition behaviours that belong in the agentic-loop finalization:
  * summarization — ``summarizer.summarize`` produces the SAME recap
    shape as ``CognitionService._handle_session_summary``;
  * escalation side-effect — already fired by the loop's escalation
    gates via ``EscalationService.record_escalation`` (the U2 flow), so
    the finalizer does not re-fire it; it only adds the live-handoff
    bundle on top when a takeover is warranted.
save_memory stays exclusively on the chat path (it needs the
PolicyEngine consent gate that lives there) — it is NOT pulled into the
loop, by design, so no consent gate is bypassed.

Doctrine: finalization is a SIDE-EFFECT half. It must never crash the
turn (§5.1) — every leg degrades to a logged warning. The reply has
already been chosen; nothing here changes it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.cognition.lead_capture import LeadCandidate, detect
from app.cognition.summarizer import summarize

logger = logging.getLogger(__name__)


@dataclass
class HandoffBundle:
    """The §3.4.6 live-handoff context bundle.

    Carried to the escalation contact on the customer's channel when a
    real-time human takeover is warranted. Asserted in tests by CONTENTS
    (transcript + summary + lead present), not by real delivery.
    """

    session_id: str
    admin_id: str
    luciel_instance_id: int | None
    transcript: list[dict] = field(default_factory=list)
    summary: str = ""
    lead: dict | None = None
    channel: str | None = None


@dataclass
class FinalizationResult:
    """What COGNITION FINALIZATION did this turn.

    All fields are best-effort / optional — finalization fires on a turn
    that may produce a lead, a handoff, both, or neither.
    """

    lead_id: int | None = None
    lead_captured: bool = False
    summary: str = ""
    handoff: HandoffBundle | None = None


class CognitionFinalizer:
    """Always-on COGNITION FINALIZATION for the agentic loop.

    Dependencies are injectable so tests drive deterministic fakes (no
    DB, no network, no LLM — founder decision #2). Built lazily so the
    orchestrator's pre-U4 construction keeps working.
    """

    def __init__(
        self,
        *,
        session_factory=None,
        audit_context=None,
    ) -> None:
        self._session_factory = session_factory
        self._audit_context = audit_context

    # ------------------------------------------------------------------
    # Public entry point — called from the orchestrator's FINALIZE step.
    # ------------------------------------------------------------------

    def finalize(
        self,
        *,
        admin_id: str,
        session_id: str,
        luciel_instance_id: int | None,
        user_id: str | None,
        current_message: str,
        prior_customer_messages: list[str] | None,
        assistant_reply: str,
        inbound_channel: str | None,
        escalation_fired: bool,
        handoff_requested: bool,
    ) -> FinalizationResult:
        """Run lead capture + summarization + live handoff.

        ``handoff_requested`` is the §3.4.6 takeover signal: True when
        the escalation that fired warrants a REAL-TIME human takeover
        (customer asked for a person, or the judge decided). The bundle
        is only built when an escalation fired AND a takeover is wanted.

        Returns a ``FinalizationResult`` describing what happened. Never
        raises — every leg degrades to a warning so finalization cannot
        crash the turn (§5.1).
        """
        result = FinalizationResult()

        # 1. §3.4.7 SUMMARY — always computed (cheap, deterministic).
        transcript = self._build_transcript(
            prior_customer_messages=prior_customer_messages,
            current_message=current_message,
            assistant_reply=assistant_reply,
        )
        result.summary = summarize(transcript)

        # 2. §3.4.4 LEAD CAPTURE — detect threshold, persist row + summary.
        candidate = self._detect(
            current_message=current_message,
            prior_customer_messages=prior_customer_messages,
            inbound_channel=inbound_channel,
        )
        lead_row_dict: dict | None = None
        if candidate is not None:
            lead_id, lead_row_dict = self._persist_lead(
                admin_id=admin_id,
                session_id=session_id,
                luciel_instance_id=luciel_instance_id,
                user_id=user_id,
                candidate=candidate,
                summary=result.summary,
            )
            result.lead_id = lead_id
            result.lead_captured = lead_id is not None

        # 3. §3.4.6 LIVE HUMAN HANDOFF — bundle only when escalation fired
        #    AND a real-time takeover is warranted.
        if escalation_fired and handoff_requested:
            result.handoff = self._build_handoff_bundle(
                admin_id=admin_id,
                session_id=session_id,
                luciel_instance_id=luciel_instance_id,
                transcript=transcript,
                summary=result.summary,
                lead=lead_row_dict,
                channel=inbound_channel,
            )
            self._deliver_handoff(result.handoff)

        return result

    # ------------------------------------------------------------------
    # §3.4.7 transcript assembly
    # ------------------------------------------------------------------

    @staticmethod
    def _build_transcript(
        *,
        prior_customer_messages: list[str] | None,
        current_message: str,
        assistant_reply: str,
    ) -> list[dict]:
        """Assemble the role/content turn list for summary + bundle.

        Prior customer turns (oldest→newest), then the current customer
        message, then the assistant reply Luciel chose this turn. This is
        the transcript surface the loop has today; a fuller persisted
        history is a later hook (it slots in here without touching the
        lead row or bundle shape).
        """
        transcript: list[dict] = []
        for m in prior_customer_messages or []:
            if m:
                transcript.append({"role": "user", "content": m})
        if current_message:
            transcript.append({"role": "user", "content": current_message})
        if assistant_reply:
            transcript.append({"role": "assistant", "content": assistant_reply})
        return transcript

    # ------------------------------------------------------------------
    # §3.4.4 lead capture
    # ------------------------------------------------------------------

    @staticmethod
    def _detect(
        *,
        current_message: str,
        prior_customer_messages: list[str] | None,
        inbound_channel: str | None,
    ) -> LeadCandidate | None:
        """Run the deterministic threshold detector. Never raises."""
        try:
            return detect(
                message=current_message,
                prior_customer_messages=prior_customer_messages,
                inbound_channel=inbound_channel,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "lead detection failed: exc_class=%s — no lead this turn",
                type(exc).__name__,
            )
            return None

    def _persist_lead(
        self,
        *,
        admin_id: str,
        session_id: str,
        luciel_instance_id: int | None,
        user_id: str | None,
        candidate: LeadCandidate,
        summary: str,
    ) -> tuple[int | None, dict | None]:
        """Write the lead row + an audit row in one transaction.

        Best-effort: a DB failure degrades to ``(None, the row dict)`` so
        the handoff bundle can still carry the lead facts even if the row
        did not persist. The lead dict is built FIRST (pure) so it is
        available to the bundle regardless of the DB outcome.
        """
        lead_dict = {
            "name": candidate.name,
            "contact_channel": candidate.contact_channel,
            "contact_identifier": candidate.contact_identifier,
            "intent": candidate.intent,
            "key_facts": list(candidate.key_facts),
            "next_step": candidate.next_step,
            "triggers": list(candidate.triggers),
            "lead_value": candidate.lead_value,
            "summary": summary,
        }

        db = None
        try:
            db = self._open_session()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "lead capture: could not open DB session: exc_class=%s — "
                "lead facts kept for bundle, row not persisted",
                type(exc).__name__,
            )
            return None, lead_dict

        try:
            from app.models.admin_audit_log import (
                ACTION_LEAD_CAPTURED,
                RESOURCE_LEAD,
            )
            from app.models.lead import Lead
            from app.repositories.admin_audit_repository import (
                AdminAuditRepository,
                AuditContext,
            )
            from app.repositories.lead_repository import LeadRepository

            row = Lead(
                admin_id=admin_id,
                luciel_instance_id=luciel_instance_id,
                session_id=session_id,
                user_id=user_id,
                name=candidate.name,
                contact_channel=candidate.contact_channel,
                contact_identifier=candidate.contact_identifier,
                intent=candidate.intent,
                key_facts=list(candidate.key_facts) or None,
                next_step=candidate.next_step,
                summary=summary,
            )
            LeadRepository(db).add(row)
            lead_id = row.id

            ctx = self._audit_context or AuditContext.system()
            AdminAuditRepository(db).record(
                ctx=ctx,
                admin_id=admin_id,
                action=ACTION_LEAD_CAPTURED,
                resource_type=RESOURCE_LEAD,
                resource_pk=lead_id,
                resource_natural_id=session_id,
                luciel_instance_id=luciel_instance_id,
                after={
                    "triggers": list(candidate.triggers),
                    "contact_channel": candidate.contact_channel,
                    "intent": candidate.intent,
                    "lead_value": candidate.lead_value,
                },
                note=f"lead:{','.join(candidate.triggers)}",
            )
            db.commit()
            return lead_id, lead_dict
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "lead capture: persistence failed: exc_class=%s — rolling back",
                type(exc).__name__,
            )
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            return None, lead_dict
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # §3.4.6 live human handoff
    # ------------------------------------------------------------------

    @staticmethod
    def _build_handoff_bundle(
        *,
        admin_id: str,
        session_id: str,
        luciel_instance_id: int | None,
        transcript: list[dict],
        summary: str,
        lead: dict | None,
        channel: str | None,
    ) -> HandoffBundle:
        """Assemble the §3.4.6 context bundle (transcript+summary+lead)."""
        return HandoffBundle(
            session_id=session_id,
            admin_id=admin_id,
            luciel_instance_id=luciel_instance_id,
            transcript=list(transcript),
            summary=summary,
            lead=lead,
            channel=channel,
        )

    def _deliver_handoff(self, bundle: HandoffBundle) -> None:
        """Mark session handoff + deliver the bundle to the notify path.

        LOCAL build: "transfer" = log the handoff intent with the bundle
        contents summarised. The real channel transport (carrying the
        bundle to the escalation contact on the customer's channel) is a
        later unit's hook that reuses U2's EscalationService routing. The
        notify SEND itself stays gated behind the live-switch in
        ``EscalationService`` — finalization only marks + records intent
        so a test asserts the bundle, not a real delivery.

        Best-effort: never raises.
        """
        try:
            logger.info(
                "live handoff (local): session=%s instance=%s "
                "transcript_turns=%d has_summary=%s has_lead=%s channel=%s",
                bundle.session_id,
                bundle.luciel_instance_id,
                len(bundle.transcript),
                bool(bundle.summary),
                bundle.lead is not None,
                bundle.channel,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "live handoff delivery failed: exc_class=%s",
                type(exc).__name__,
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _open_session(self):
        if self._session_factory is not None:
            return self._session_factory()
        from app.db.session import SessionLocal

        return SessionLocal()
