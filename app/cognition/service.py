"""Cognition service — interim host for the three always-on behaviours.

TODO(ARC14): This module is the interim host for cognition.
Absorbed into ``LucielOrchestrator.run`` at Arc 14 (the agentic
loop). Behaviour PRESERVED from the pre-WU7 tool implementations
(``EscalateTool``, ``SaveMemoryTool``, ``SessionSummaryTool``)
exactly as-is — no extension, no redesign. See ``app/cognition/
__init__.py`` for the founder-ruling header.

Intent recognition mirrors the pre-WU7 substring/TOOL_CALL chain
literally so the same LLM outputs that fired cognition before
WU7 fire it after WU7. The chat path no longer branches on
substring matching itself — it calls ``process_turn`` once and
acts on the returned ``CognitionOutcome``.

Cognition is non-tier-gated (§3.4): every Luciel, every tier,
always-on. No broker, no registry, no authorisation lookup, no
classification gate. Architecture §3.4 names cognition as the
behaviour that survives outside the configurable tool catalog.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.policy.escalation import EscalationService

logger = logging.getLogger(__name__)


# The three cognition intents — frozen as-is from the pre-WU7
# tool_ids so behaviour is literally preserved across the move.
INTENT_ESCALATE = "escalate_to_human"
INTENT_SAVE_MEMORY = "save_memory"
INTENT_SESSION_SUMMARY = "get_session_summary"

_COGNITION_INTENTS = frozenset({
    INTENT_ESCALATE,
    INTENT_SAVE_MEMORY,
    INTENT_SESSION_SUMMARY,
})


@dataclass
class CognitionOutcome:
    """What the cognition module did this turn.

    Returned by ``CognitionService.process_turn``. The chat path
    consumes:

      * ``intent`` — which cognition behaviour fired, or None.
      * ``handled`` — True if cognition produced an output the
        chat path should surface (either as a follow-up tool
        result for the LLM to incorporate, or as an escalation
        notice).
      * ``output`` — short human-readable string the follow-up
        LLM turn references (mirrors the pre-WU7 ``ToolResult.
        output`` shape).
      * ``escalated`` — True iff escalation fired this turn.
      * ``escalation_reason`` — non-empty when escalated.
      * ``memory_payload`` — for ``save_memory``: ``{category,
        content}`` so the chat path can run the existing
        PolicyEngine.evaluate_memory_write check + persist via
        MemoryRepository. (We keep the persistence decision in
        the chat path so the existing consent + policy gates
        stay on the same call site they were before WU7.)
      * ``metadata`` — full payload for trace / audit shape
        equivalence with the pre-WU7 ``ToolResult.metadata``.
    """

    intent: str | None = None
    handled: bool = False
    output: str = ""
    escalated: bool = False
    escalation_reason: str = ""
    memory_payload: dict[str, str] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class CognitionService:
    """Always-on cognition behaviours, invoked directly by chat_service.

    Three behaviours, preserved from pre-WU7:

      * **escalate** — flags the conversation for human handoff
        (intent name ``escalate_to_human``). Delegates to
        ``EscalationService.handle_escalation`` for the
        side-effect, exactly as the pre-WU7 chat_service did.
      * **save_memory** — surfaces a memory payload for the chat
        path to persist (intent name ``save_memory``). The
        payload shape ``{category, content}`` matches the pre-WU7
        ``SaveMemoryTool`` exactly so the chat path's
        ``PolicyEngine.evaluate_memory_write`` + repository write
        works without modification.
      * **session_summary** — returns a recap of the supplied
        message history (intent name ``get_session_summary``).

    No tier-gating; no broker; no registry. Founder ruling 4c.
    """

    def __init__(
        self,
        escalation_service: EscalationService | None = None,
    ) -> None:
        # EscalationService has no constructor args today; we accept
        # an injected instance so tests can substitute a stub.
        self.escalation_service = escalation_service or EscalationService()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def process_turn(
        self,
        *,
        raw_reply: str,
        messages: list[dict] | None = None,
        session_id: str,
        user_id: str | None,
        admin_id: str,
    ) -> CognitionOutcome:
        """Inspect ``raw_reply`` for a cognition intent and execute it.

        Returns a ``CognitionOutcome`` describing what (if anything)
        fired. The chat path:

          * appends a follow-up LLM turn with ``outcome.output`` when
            ``handled`` and the intent is NOT escalation (matches the
            pre-WU7 "Tool Result: ... — respond to the user" loop);
          * persists the memory row via the existing memory
            repository when ``intent == INTENT_SAVE_MEMORY`` and the
            existing PolicyEngine.evaluate_memory_write check passes
            (matches the pre-WU7 save-memory follow-through);
          * runs ``EscalationService.handle_escalation`` when
            ``escalated`` — actually the escalation side-effect ALREADY
            fired here in ``process_turn``; the chat path only needs
            to render the customer-facing default message via the
            existing PolicyEngine path (which keys off
            ``tool_name='escalate_to_human'``).
        """
        intent = self._detect_intent(raw_reply)
        if intent is None:
            return CognitionOutcome()

        if intent == INTENT_ESCALATE:
            return self._handle_escalate(
                raw_reply=raw_reply,
                session_id=session_id,
                user_id=user_id,
                admin_id=admin_id,
            )
        if intent == INTENT_SAVE_MEMORY:
            return self._handle_save_memory(raw_reply=raw_reply)
        if intent == INTENT_SESSION_SUMMARY:
            return self._handle_session_summary(messages=messages or [])

        # Defensive: detect returned a name we don't handle. Shouldn't
        # happen given _COGNITION_INTENTS gate, but stay closed.
        return CognitionOutcome()

    # ------------------------------------------------------------------
    # Intent detection
    # ------------------------------------------------------------------

    def _detect_intent(self, raw_reply: str) -> str | None:
        """Recognise one of the three cognition intents in ``raw_reply``.

        Pre-WU7 behaviour:
          - The broker first checked for ``TOOL_CALL:`` JSON and
            executed the named tool.
          - The chat_service then substring-matched the raw_reply
            to identify which tool fired and branch.

        We preserve both branches literally:
          1. Parse a ``TOOL_CALL:`` envelope first; if the ``tool``
             field names one of the three cognition intents, return
             it. This is the load-bearing path — every well-formed
             LLM output uses it.
          2. Fall back to the substring check the pre-WU7
             chat_service used. This is intentionally redundant
             with (1) for behaviour-equivalence: any raw_reply that
             pre-WU7 chat_service would have matched still matches.

        Returns the intent name or ``None``.
        """
        # (1) Structured TOOL_CALL envelope.
        if "TOOL_CALL:" in raw_reply:
            try:
                json_str = raw_reply.split("TOOL_CALL:", 1)[1].strip()
                call_data = json.loads(json_str)
                tool_name = call_data.get("tool", "")
                if tool_name in _COGNITION_INTENTS:
                    return tool_name
            except (json.JSONDecodeError, Exception) as exc:
                logger.debug("cognition: TOOL_CALL parse failed: %s", exc)

        # (2) Substring fallback — preserves pre-WU7 detection shape.
        # Order matches the pre-WU7 if/elif chain in chat_service.
        if INTENT_ESCALATE in raw_reply:
            return INTENT_ESCALATE
        if INTENT_SAVE_MEMORY in raw_reply:
            return INTENT_SAVE_MEMORY
        if INTENT_SESSION_SUMMARY in raw_reply:
            return INTENT_SESSION_SUMMARY

        return None

    # ------------------------------------------------------------------
    # Behaviour handlers — preserve pre-WU7 tool bodies as-is
    # ------------------------------------------------------------------

    def _parse_tool_parameters(self, raw_reply: str) -> dict[str, Any]:
        """Extract ``parameters`` dict from a TOOL_CALL envelope.

        Returns ``{}`` if no envelope is present or parsing fails —
        mirrors the pre-WU7 broker behaviour of treating malformed
        JSON as "no tool call" rather than raising.
        """
        if "TOOL_CALL:" not in raw_reply:
            return {}
        try:
            json_str = raw_reply.split("TOOL_CALL:", 1)[1].strip()
            call_data = json.loads(json_str)
            params = call_data.get("parameters", {})
            return params if isinstance(params, dict) else {}
        except (json.JSONDecodeError, Exception):
            return {}

    def _handle_escalate(
        self,
        *,
        raw_reply: str,
        session_id: str,
        user_id: str | None,
        admin_id: str,
    ) -> CognitionOutcome:
        """Preserve EscalateTool behaviour.

        Pre-WU7: the tool returned ``{success, output, escalated,
        escalation_reason}``; the chat_service then ran the policy
        engine which set ``decision.escalated`` and called
        ``EscalationService.handle_escalation``.

        We collapse the two steps: the cognition module fires the
        escalation side-effect itself (so chat_service no longer
        needs to). The chat path still runs the policy engine for
        the user-facing default message, which keys off
        ``tool_name='escalate_to_human'`` (unchanged).
        """
        params = self._parse_tool_parameters(raw_reply)
        reason = params.get("reason", "No reason provided")

        try:
            self.escalation_service.handle_escalation(
                session_id=session_id,
                user_id=user_id,
                admin_id=admin_id,
                reason=reason,
            )
        except Exception as exc:
            # Pre-WU7 the EscalationService call was inside the
            # chat_service; we keep the chat-turn-doesn't-die guarantee.
            logger.warning(
                "cognition: escalation handler failed: %s", exc,
            )

        output = f"Escalation requested: {reason}"
        return CognitionOutcome(
            intent=INTENT_ESCALATE,
            handled=True,
            output=output,
            escalated=True,
            escalation_reason=reason,
            metadata={
                "success": True,
                "output": output,
                "escalated": True,
                "escalation_reason": reason,
            },
        )

    def _handle_save_memory(self, *, raw_reply: str) -> CognitionOutcome:
        """Preserve SaveMemoryTool behaviour.

        Pre-WU7 the tool returned ``{success, output, category,
        content}`` without writing the DB itself — the chat_service
        then ran ``PolicyEngine.evaluate_memory_write`` and
        ``memory_service.repository.save_memory(...)``. We keep that
        split: cognition returns the ``memory_payload`` and the
        chat path persists, so the existing policy + consent gates
        stay on the same call site.
        """
        params = self._parse_tool_parameters(raw_reply)
        category = params.get("category", "")
        content = params.get("content", "")

        if not category or not content:
            output = ""
            metadata = {
                "success": False,
                "output": "",
                "error": "Both 'category' and 'content' are required.",
                "category": category,
                "content": content,
            }
            return CognitionOutcome(
                intent=INTENT_SAVE_MEMORY,
                handled=True,
                output=output,
                memory_payload=None,
                metadata=metadata,
            )

        output = f"Memory saved: [{category}] {content}"
        return CognitionOutcome(
            intent=INTENT_SAVE_MEMORY,
            handled=True,
            output=output,
            memory_payload={"category": category, "content": content},
            metadata={
                "success": True,
                "output": output,
                "category": category,
                "content": content,
            },
        )

    def _handle_session_summary(
        self, *, messages: list[dict],
    ) -> CognitionOutcome:
        """Preserve SessionSummaryTool behaviour.

        Returns a short recap of the supplied conversation messages.
        Same formatting as the pre-WU7 tool body (150-char preview
        per message, role-uppercased prefix).
        """
        if not messages:
            output = "No messages in this session yet."
            return CognitionOutcome(
                intent=INTENT_SESSION_SUMMARY,
                handled=True,
                output=output,
                metadata={"success": True, "output": output},
            )

        summary_parts = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            preview = (
                content[:150] + "..." if len(content) > 150 else content
            )
            summary_parts.append(f"{role.upper()}: {preview}")

        summary = "\n".join(summary_parts)
        output = f"Session summary ({len(messages)} messages):\n{summary}"
        return CognitionOutcome(
            intent=INTENT_SESSION_SUMMARY,
            handled=True,
            output=output,
            metadata={"success": True, "output": output},
        )
