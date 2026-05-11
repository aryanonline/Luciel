"""
Session Summary tool.

Provides a summary of the current conversation so far.
Useful when Luciel needs to recap what has been discussed,
or when the user asks for a summary.
"""

from __future__ import annotations

from typing import Any

from app.policy.action_classification import ActionTier
from app.tools.base import LucielTool, ToolResult


class SessionSummaryTool(LucielTool):

    # Step 30c: ROUTINE. Reading the current session's own messages
    # is the reading-shaped work Recap §4 names as not consequential.
    # No external surface, no write side-effect.
    declared_tier = ActionTier.ROUTINE

    @property
    def name(self) -> str:
        return "get_session_summary"

    @property
    def description(self) -> str:
        return (
            "Get a summary of the current conversation. "
            "Use this when you need to recap what has been discussed."
        )

    @property
    def parameter_schema(self) -> dict[str, Any]:
        return {}

    def execute(self, **kwargs: Any) -> ToolResult:
        """
        The actual summarization is handled by the broker
        which has access to session messages.
        This tool just signals intent.
        """
        messages = kwargs.get("_messages", [])

        if not messages:
            return ToolResult(
                success=True,
                output="No messages in this session yet.",
            )

        summary_parts = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            # Truncate long messages for the summary
            preview = content[:150] + "..." if len(content) > 150 else content
            summary_parts.append(f"{role.upper()}: {preview}")

        summary = "\n".join(summary_parts)

        return ToolResult(
            success=True,
            output=f"Session summary ({len(messages)} messages):\n{summary}",
        )