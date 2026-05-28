"""
Tool registry.

Central place where all available tools are registered. The registry
is what the broker and the LLM prompt use to know what tools exist
and what they can do. Lookups are keyed by ``tool_id`` (the §3.3.1
identifier).

Arc 12 WU1 migrated the registry off the v1 ``tool.name`` key onto
``tool.tool_id`` and onto the §3.3.1 contract surface (``description``,
``input_schema``, ``requires_tier``, ``requires_channels``,
``execution_mode``). The default registration set (the three
"cognition" tools: escalate / save_memory / session_summary) is kept
in place for WU1 -- WU7 evicts them when the cognition module lands.

WU2 will add a per-instance authorisation overlay so the registry's
contents are only the universe of *available* tools; the *authorised*
set is computed per (admin_id, instance_id) via the WU2 authorisation
table.
"""

from __future__ import annotations

from app.tools.base import LucielTool
from app.tools.implementations.escalate_tool import EscalateTool
from app.tools.implementations.save_memory_tool import SaveMemoryTool
from app.tools.implementations.session_summary_tool import SessionSummaryTool


class ToolRegistry:
    """Holds all registered tools and provides lookup methods."""

    def __init__(self) -> None:
        self._tools: dict[str, LucielTool] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        """Register the built-in tools.

        The three "cognition" tools (save_memory, session_summary,
        escalate_to_human) are still registered here at WU1; WU7
        evicts them when the cognition module subsumes their
        behaviour.
        """
        self.register(SaveMemoryTool())
        self.register(SessionSummaryTool())
        self.register(EscalateTool())

    def register(self, tool: LucielTool) -> None:
        """Add a tool to the registry, keyed by ``tool_id``."""
        self._tools[tool.tool_id] = tool

    def get(self, name: str) -> LucielTool | None:
        """Look up a tool by ``tool_id``."""
        return self._tools.get(name)

    def list_tools(self) -> list[LucielTool]:
        """Return all registered tools."""
        return list(self._tools.values())

    def get_tool_descriptions(
        self,
        allowed: list[str] | None = None,
    ) -> str:
        """Format tools as a text block for injection into the LLM
        prompt.

        If ``allowed`` is None, all tools are included (no
        restriction). If ``allowed`` is a list, only tools whose
        ``tool_id`` is in the list are included.

        The §3.3.1 contract uses JSON Schema for input. We render a
        compact ``properties`` summary so the LLM still sees a
        usable parameter hint.
        """
        tools = list(self._tools.values())
        if allowed is not None:
            tools = [t for t in tools if t.tool_id in allowed]

        if not tools:
            return ""

        descriptions = []
        for tool in tools:
            props = tool.input_schema.get("properties", {}) or {}
            if props:
                params = ", ".join(
                    f"{k} ({v.get('type', 'string')}): "
                    f"{v.get('description', '')}"
                    for k, v in props.items()
                )
                param_str = f"  Parameters: {params}"
            else:
                param_str = "  Parameters: none"
            descriptions.append(
                f"- {tool.tool_id}: {tool.description}\n{param_str}"
            )
        return "\n\n".join(descriptions)
