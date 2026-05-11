"""
Escalate tool.

Allows Luciel to flag a conversation for human review.
This is used when Luciel is uncertain, when the user is frustrated,
or when the request is outside Luciel's capabilities.

For now this just flags the intent. Later it can trigger
real notifications, ticket creation, or handoff flows.
"""

from __future__ import annotations

from typing import Any

from app.policy.action_classification import ActionTier
from app.tools.base import LucielTool, ToolResult


class EscalateTool(LucielTool):

    # Step 30c: NOTIFY_AND_PROCEED. Escalation runs without blocking
    # (we never want Luciel to pause for approval to reach a human
    # when it has already judged the situation needs one) but the
    # customer must see that an escalation happened, because their
    # next turn will likely be with a human rather than Luciel. The
    # tier records that surfacing requirement — the Runtime layer
    # is responsible for actually rendering the notification frame
    # to the customer; the broker just hands it the tier signal.
    declared_tier = ActionTier.NOTIFY_AND_PROCEED

    @property
    def name(self) -> str:
        return "escalate_to_human"

    @property
    def description(self) -> str:
        return (
            "Escalate this conversation to a human agent. "
            "Use this when you cannot confidently help the user, "
            "when the user explicitly asks for a human, "
            "or when the situation requires human judgment."
        )

    @property
    def parameter_schema(self) -> dict[str, Any]:
        return {
            "reason": {
                "type": "string",
                "description": "Why this conversation needs human attention",
                "required": True,
            },
        }

    def execute(self, **kwargs: Any) -> ToolResult:
        reason = kwargs.get("reason", "No reason provided")

        return ToolResult(
            success=True,
            output=f"Escalation requested: {reason}",
            metadata={"escalation_reason": reason, "escalated": True},
        )