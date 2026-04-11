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

from app.tools.base import LucielTool, ToolResult


class EscalateTool(LucielTool):

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