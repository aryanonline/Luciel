"""
Session Summary tool — §3.3.1-conformant shim.

WU1 migrated this tool onto the new §3.3.1 base contract. Behaviour
is unchanged from v1: the tool returns a short summary of the
current conversation's messages.

This tool will be evicted from the registry at WU7 and relocated to
the cognition module (Arc 12 founder ruling 4).
"""

from __future__ import annotations

from typing import Any

from app.policy.action_classification import ActionTier
from app.tools.base import LucielTool, ToolContext


class SessionSummaryTool(LucielTool):

    # Step 30c -- ROUTINE. Reading the current session's own messages
    # is the reading-shaped work Recap §4 names as not consequential.
    declared_tier = ActionTier.ROUTINE

    @property
    def tool_id(self) -> str:
        return "get_session_summary"

    @property
    def display_name(self) -> str:
        return "Get session summary"

    @property
    def description(self) -> str:
        return (
            "Get a summary of the current conversation. "
            "Use this when you need to recap what has been discussed."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        # ``messages`` is the conversation history threaded through
        # by the chat_service follow-up call. It is not LLM-author'd
        # so we mark it additional/extra rather than required.
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        }

    @property
    def output_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "output": {"type": "string"},
            },
            "required": ["success", "output"],
            "additionalProperties": True,
        }

    @property
    def requires_tier(self) -> tuple[str, ...]:
        return ("free", "pro", "enterprise")

    @property
    def execution_mode(self) -> str:
        return "in_process"

    async def execute(
        self,
        input: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        # ``messages`` is passed through by chat_service as a kwarg
        # alongside the LLM-author'd parameters; the broker merges it
        # into ``input``. We also accept the legacy ``_messages``
        # alias the v1 tool used.
        messages = input.get("messages") or input.get("_messages") or []

        if not messages:
            return {
                "success": True,
                "output": "No messages in this session yet.",
            }

        summary_parts = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            preview = (
                content[:150] + "..." if len(content) > 150 else content
            )
            summary_parts.append(f"{role.upper()}: {preview}")

        summary = "\n".join(summary_parts)
        return {
            "success": True,
            "output": (
                f"Session summary ({len(messages)} messages):\n{summary}"
            ),
        }
