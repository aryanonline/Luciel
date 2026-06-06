"""book_appointment — v1 catalog tool (§3.3.2).

Books an appointment on a customer-facing calendar (e.g. a property
viewing time slot). Action-classification tier: NOTIFY_AND_PROCEED —
this is an external-facing action that should surface to the
customer but is reversible (cancelable) and expected within the
customer's existing pattern.

Interim-body rule (00_MASTER §"interim-body rule")
==================================================
No calendar integration ships in the tree today. The full §3.3.1
contract is declared so the registry, broker, schema validator, and
authorisation gate (Arc 12 WU2) can all reason about this tool.
``execute()`` performs NO side effect and returns a structured
"not yet available" dict naming the owning arc.

The natural owner is Arc 13 — calendar integration peers with SES /
Twilio (the v1 channel adapter wave). This may be reassigned by the
founder; flagged in the WU3 final report.
"""

# TODO(ARC13): replace this interim body with a real calendar
# integration. The contract above is the steady-state contract;
# only the execute() body needs to be wired.

from __future__ import annotations

from typing import Any

from app.policy.action_classification import ActionTier
from app.tools.base import LucielTool, ToolContext


class BookAppointmentTool(LucielTool):

    declared_tier = ActionTier.NOTIFY_AND_PROCEED

    # Arc 15 WU4/WU5 — connection-contract gate (§3.3.2). Booking needs
    # a LIVE calendar connection. calendar is a DEFERRED connector in
    # this slice: configuring it lands an ``unconfigured`` row, so the
    # WU5 gate will refuse dispatch until Arc 17 ships the real backing.
    requires_connection = "calendar"

    @property
    def tool_id(self) -> str:
        return "book_appointment"

    @property
    def display_name(self) -> str:
        return "Book appointment"

    @property
    def description(self) -> str:
        return (
            "Book an appointment slot for the user on a connected "
            "calendar."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "starts_at": {
                    "type": "string",
                    "description": "ISO-8601 start timestamp.",
                    "minLength": 1,
                },
                "duration_minutes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1440,
                },
                "attendee_name": {"type": "string", "minLength": 1},
                "attendee_contact": {"type": "string", "minLength": 1},
                "notes": {"type": "string"},
            },
            "required": ["starts_at", "attendee_name", "attendee_contact"],
            "additionalProperties": False,
        }

    @property
    def output_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "output": {"type": "string"},
                "not_yet_available": {"type": "boolean"},
                "owning_arc": {"type": "string"},
            },
            "required": ["success", "output"],
            "additionalProperties": True,
        }

    @property
    def requires_tier(self) -> tuple[str, ...]:
        # Action tools are Pro-only; Enterprise tier deferred
        # (Open Decision #8 -- ratified 2-tier Free/Pro model).
        return ("pro",)

    @property
    def execution_mode(self) -> str:
        return "in_process"

    async def execute(
        self,
        input: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        # Interim body — no side effect. The natural calendar
        # adapter ships in the Arc 13 channel-integration wave.
        return {
            "success": False,
            "output": (
                "book_appointment is registered but its calendar "
                "integration has not yet shipped (owning arc: ARC13). "
                "No appointment was booked."
            ),
            "not_yet_available": True,
            "owning_arc": "ARC13",
        }
