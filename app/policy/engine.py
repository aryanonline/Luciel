"""
Policy engine.

Governs what Luciel may and may not do.
This is the trust and safety layer that sits between
the LLM output and the final user-facing response.

Responsibilities:
- Clean tool call syntax from user-facing replies.
- Enforce escalation behavior.
- Flag low-confidence or risky responses.
- Restrict memory writes for inappropriate content.
- Apply guardrails before final output.

Future additions:
- Per-tenant policy rules.
- Per-domain restrictions.
- Rate limiting on tool calls.
- Content filtering.
- Audit logging for policy decisions.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PolicyDecision:
    """
    The result of running a response through the policy engine.

    allowed:          Whether the response is permitted.
    modified_reply:   The cleaned/modified reply text.
    escalated:        Whether this turn triggered escalation.
    escalation_reason: Why escalation was triggered.
    flags:            Any warnings or notes from policy checks.
    """
    allowed: bool = True
    modified_reply: str = ""
    escalated: bool = False
    escalation_reason: str = ""
    flags: list[str] = field(default_factory=list)


DEFAULT_ESCALATION_MESSAGE = (
    "I understand you'd like to speak with someone directly. "
    "I'm flagging this conversation for a team member to follow up with you. "
    "They'll be in touch shortly."
)


class PolicyEngine:
    """
    Evaluates LLM output and tool results against policy rules.
    Returns a PolicyDecision that the chat service uses to
    determine the final user-facing response.
    """

    def evaluate_response(
        self,
        *,
        raw_reply: str,
        tool_was_called: bool = False,
        tool_name: str | None = None,
        tool_result_metadata: dict | None = None,
    ) -> PolicyDecision:
        """
        Run all policy checks on a response.
        """
        decision = PolicyDecision()
        reply = raw_reply

        # --- Check 1: Handle escalation ---
        if tool_was_called and tool_name == "escalate_to_human":
            reason = ""
            if tool_result_metadata:
                reason = tool_result_metadata.get("escalation_reason", "")

            decision.escalated = True
            decision.escalation_reason = reason

            # For escalation, always use the clean default message.
            # The LLM response is likely just the TOOL_CALL JSON,
            # so trying to salvage text from it is unreliable.
            decision.modified_reply = DEFAULT_ESCALATION_MESSAGE
            decision.flags.append(f"escalation_triggered: {reason}")
            logger.info("Policy: escalation triggered — %s", reason)
            return decision

        # --- Check 2: Clean any stray tool call text from reply ---
        cleaned = self._clean_tool_call_text(reply)
        if cleaned != reply:
            decision.flags.append("tool_call_text_stripped")
            reply = cleaned

        # --- Check 3: Empty or too-short response check ---
        if len(reply.strip()) < 10:
            reply = "I'm not sure how to help with that. Could you rephrase your question?"
            decision.flags.append("empty_response_replaced")

        # --- Check 4: Response length guardrail ---
        if len(reply) > 10000:
            reply = reply[:10000] + "\n\n[Response truncated for length.]"
            decision.flags.append("response_truncated")

        decision.modified_reply = reply
        return decision

    def evaluate_memory_write(
        self,
        *,
        category: str,
        content: str,
    ) -> bool:
        """
        Check whether a memory item is appropriate to save.
        Returns True if allowed, False if blocked.
        """
        if not content.strip():
            logger.info("Policy: blocked empty memory write")
            return False

        if len(content.strip()) < 5:
            logger.info("Policy: blocked too-short memory write")
            return False

        valid_categories = {"preference", "constraint", "goal", "fact", "operational"}
        if category not in valid_categories:
            logger.info("Policy: blocked invalid memory category '%s'", category)
            return False

        return True

    def _clean_tool_call_text(self, text: str) -> str:
        """
        Remove all TOOL_CALL patterns from text.
        Handles multiline JSON, nested braces, and trailing fragments.
        """
        # First try: remove TOOL_CALL: followed by any JSON object
        # This pattern matches TOOL_CALL: { ... } including nested braces
        cleaned = re.sub(
            r"TOOL_CALL:\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}",
            "",
            text,
            flags=re.DOTALL,
        )

        # Second pass: remove any remaining JSON-like fragments
        # that start with { and look like leftover tool call data
        cleaned = re.sub(
            r'\{\s*"tool"\s*:.*?\}',
            "",
            cleaned,
            flags=re.DOTALL,
        )

        # Clean up leftover whitespace and fragments
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        cleaned = cleaned.strip()

        # Remove any trailing lone braces or dots
        cleaned = re.sub(r"^[}\s.]+$", "", cleaned)
        cleaned = cleaned.strip()

        return cleaned