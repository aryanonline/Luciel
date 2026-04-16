"""
Tool registry.

Central place where all available tools are registered.
The registry is what the broker and the LLM use to know
what tools exist and what they can do.

To add a new tool:
1. Create the tool class in app/tools/implementations/
2. Import and register it here.

PATCHED: get_tool_descriptions() now accepts an optional allowed
list so domain configs can restrict which tools the LLM sees.
"""

from __future__ import annotations

from app.tools.base import LucielTool
from app.tools.implementations.escalate_tool import EscalateTool
from app.tools.implementations.save_memory_tool import SaveMemoryTool
from app.tools.implementations.session_summary_tool import SessionSummaryTool


class ToolRegistry:
    """
    Holds all registered tools and provides lookup methods.
    """

    def __init__(self) -> None:
        self._tools: dict[str, LucielTool] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        """Register the built-in MVP tools."""
        self.register(SaveMemoryTool())
        self.register(SessionSummaryTool())
        self.register(EscalateTool())

    def register(self, tool: LucielTool) -> None:
        """Add a tool to the registry."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> LucielTool | None:
        """Look up a tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> list[LucielTool]:
        """Return all registered tools."""
        return list(self._tools.values())

    def get_tool_descriptions(
        self,
        allowed: list[str] | None = None,
    ) -> str:
        """
        Format tools as a text block for injection into the LLM prompt.

        If allowed is None, all tools are included (no restriction).
        If allowed is a list, only tools whose name is in the list
        are included. This is how domain configs restrict tool access.
        """
        tools = self._tools.values()
        if allowed is not None:
            tools = [t for t in tools if t.name in allowed]

        if not tools:
            return ""

        descriptions = []
        for tool in tools:
            params = ", ".join(
                f"{k} ({v.get('type', 'string')}): {v.get('description', '')}"
                for k, v in tool.parameter_schema.items()
            )
            param_str = f"  Parameters: {params}" if params else "  Parameters: none"
            descriptions.append(
                f"- {tool.name}: {tool.description}\n{param_str}"
            )
        return "\n\n".join(descriptions)
