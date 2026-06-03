"""
Tool registry.

Central place where all available tools are registered. The registry
is what the broker and the LLM prompt use to know what tools exist
and what they can do. Lookups are keyed by ``tool_id`` (the §3.3.1
identifier).

Arc 12 WU7 evicted the three cognition tools (escalate /
save_memory / session_summary) per founder ruling 4 + Decision #20:
cognition is NOT in the tool registry. Their behaviour now lives in
``app.cognition`` and is invoked directly by chat_service. The
registry holds ONLY the 8 configurable v1 catalog tools (§3.3.2).

WU2's per-instance authorisation overlay computes the *authorised*
set per (admin_id, instance_id) from this universe of *available*
tools.
"""

from __future__ import annotations

from app.tools.base import LucielTool
from app.tools.implementations.book_appointment_tool import (
    BookAppointmentTool,
)
from app.tools.implementations.bring_your_own_webhook_tool import (
    BringYourOwnWebhookTool,
)
from app.tools.implementations.call_sibling_luciel_tool import (
    CallSiblingLucielTool,
)
from app.tools.implementations.lookup_record_tool import (
    LookupRecordTool,
)
from app.tools.implementations.push_to_crm_tool import PushToCrmTool
from app.tools.implementations.schedule_callback_tool import (
    ScheduleCallbackTool,
)
from app.tools.implementations.send_email_tool import SendEmailTool
from app.tools.implementations.send_sms_tool import SendSmsTool


class ToolRegistry:
    """Holds all registered tools and provides lookup methods."""

    def __init__(self) -> None:
        self._tools: dict[str, LucielTool] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        """Register the built-in tools.

        The registry holds the v1 catalog (Arc 12 WU3, §3.3.2): the
        8 configurable tools every Pro/Enterprise instance can opt
        into via the per-instance authorisation table (WU2). Some
        carry interim execute() bodies per the 00_MASTER
        "interim-body rule" (see each tool's module docstring for
        the owning arc).

        Cognition (escalate / save_memory / session_summary) is NOT
        registered here — Decision #20 + founder ruling 4. Its
        behaviour lives in ``app.cognition`` and is called directly
        by chat_service (no broker, no registry, no tier-gating).
        """
        # v1 catalog (WU3) — exactly 8 tools, nothing cognition-shaped.
        self.register(BookAppointmentTool())
        self.register(SendEmailTool())
        self.register(SendSmsTool())
        self.register(LookupRecordTool())
        self.register(ScheduleCallbackTool())
        self.register(PushToCrmTool())
        self.register(CallSiblingLucielTool())
        self.register(BringYourOwnWebhookTool())

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
