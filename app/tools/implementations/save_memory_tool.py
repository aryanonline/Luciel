"""
Save Memory tool — §3.3.1-conformant shim.

WU1 migrated this tool onto the new §3.3.1 base contract. Behaviour
is unchanged from v1: the tool does NOT write to the DB directly; it
returns the memory payload so the orchestration layer can handle
persistence with the right user/tenant context.

This tool will be evicted from the registry at WU7 and relocated to
the cognition module (Arc 12 founder ruling 4).
"""

from __future__ import annotations

from typing import Any

from app.policy.action_classification import ActionTier
from app.tools.base import LucielTool, ToolContext


class SaveMemoryTool(LucielTool):

    # Step 30c -- ROUTINE. Writing a memory row is reversible
    # (Pattern E retention), low-blast-radius (one row keyed to the
    # current user/tenant scope), and is exactly the senior-advisor
    # work Recap §4 names as not consequential.
    declared_tier = ActionTier.ROUTINE

    @property
    def tool_id(self) -> str:
        return "save_memory"

    @property
    def display_name(self) -> str:
        return "Save memory"

    @property
    def description(self) -> str:
        return (
            "Save a durable fact about the user for future reference. "
            "Use this when the user shares an important preference, "
            "constraint, goal, or personal fact worth remembering."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": (
                        "One of: preference, constraint, goal, fact, "
                        "operational"
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "The fact to remember, as a short clear sentence"
                    ),
                },
            },
            "required": ["category", "content"],
            "additionalProperties": True,
        }

    @property
    def output_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "output": {"type": "string"},
                "category": {"type": "string"},
                "content": {"type": "string"},
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
        category = input.get("category", "")
        content = input.get("content", "")

        if not category or not content:
            return {
                "success": False,
                "output": "",
                "error": "Both 'category' and 'content' are required.",
            }

        return {
            "success": True,
            "output": f"Memory saved: [{category}] {content}",
            "category": category,
            "content": content,
        }
