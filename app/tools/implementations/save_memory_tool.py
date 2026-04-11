"""
Save Memory tool.

Allows Luciel to explicitly save a fact about the user
when the conversation makes it clear something should be remembered.

This is different from automatic memory extraction (Step 5).
Automatic extraction runs after every turn silently.
This tool lets Luciel deliberately choose to save something
when the model decides it is important.
"""

from __future__ import annotations

from typing import Any

from app.tools.base import LucielTool, ToolResult


class SaveMemoryTool(LucielTool):

    @property
    def name(self) -> str:
        return "save_memory"

    @property
    def description(self) -> str:
        return (
            "Save a durable fact about the user for future reference. "
            "Use this when the user shares an important preference, "
            "constraint, goal, or personal fact worth remembering."
        )

    @property
    def parameter_schema(self) -> dict[str, Any]:
        return {
            "category": {
                "type": "string",
                "description": "One of: preference, constraint, goal, fact, operational",
                "required": True,
            },
            "content": {
                "type": "string",
                "description": "The fact to remember, as a short clear sentence",
                "required": True,
            },
        }

    def execute(self, **kwargs: Any) -> ToolResult:
        """
        This tool does not write to the DB directly.
        It returns the memory data so the orchestration layer
        can handle persistence with the right user/tenant context.
        """
        category = kwargs.get("category", "")
        content = kwargs.get("content", "")

        if not category or not content:
            return ToolResult(
                success=False,
                output="",
                error="Both 'category' and 'content' are required.",
            )

        return ToolResult(
            success=True,
            output=f"Memory saved: [{category}] {content}",
            metadata={"category": category, "content": content},
        )