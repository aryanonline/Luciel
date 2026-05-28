"""
Escalate tool — §3.3.1-conformant shim.

WU1 migrated this tool onto the new §3.3.1 base contract. The
behaviour is unchanged from v1: the tool flags the intent to
escalate; the runtime layer hands the conversation off to a human.

This tool will be evicted from the registry at WU7 and relocated to
the cognition module (Arc 12 founder ruling 4). Until then it keeps
its existing place + its existing behaviour, only conforming to the
new base contract.
"""

from __future__ import annotations

from typing import Any

from app.policy.action_classification import ActionTier
from app.tools.base import LucielTool, ToolContext


class EscalateTool(LucielTool):

    # Step 30c -- NOTIFY_AND_PROCEED. Escalation runs without blocking
    # the customer-facing turn (we never want Luciel to pause for
    # approval to reach a human when it has already judged the
    # situation needs one) but the customer must see that the
    # escalation happened, because their next turn will likely be
    # with a human rather than Luciel.
    declared_tier = ActionTier.NOTIFY_AND_PROCEED

    @property
    def tool_id(self) -> str:
        return "escalate_to_human"

    @property
    def display_name(self) -> str:
        return "Escalate to human"

    @property
    def description(self) -> str:
        return (
            "Escalate this conversation to a human agent. "
            "Use this when you cannot confidently help the user, "
            "when the user explicitly asks for a human, "
            "or when the situation requires human judgment."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "Why this conversation needs human attention"
                    ),
                },
            },
            "required": ["reason"],
            "additionalProperties": True,
        }

    @property
    def output_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "output": {"type": "string"},
                "escalated": {"type": "boolean"},
                "escalation_reason": {"type": "string"},
            },
            "required": ["success", "output"],
            "additionalProperties": True,
        }

    @property
    def requires_tier(self) -> tuple[str, ...]:
        # Cognition tool -- available on every tier today; WU7 moves
        # the behaviour to the always-on cognition module per
        # ARCHITECTURE §3.4 (cognition is non-tier-gated).
        return ("free", "pro", "enterprise")

    @property
    def execution_mode(self) -> str:
        return "in_process"

    async def execute(
        self,
        input: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        reason = input.get("reason", "No reason provided")
        return {
            "success": True,
            "output": f"Escalation requested: {reason}",
            "escalation_reason": reason,
            "escalated": True,
        }
