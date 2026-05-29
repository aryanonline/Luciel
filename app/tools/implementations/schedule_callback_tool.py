"""schedule_callback — v1 catalog tool (§3.3.2).

Schedules a follow-up callback (phone or in-person) for the
customer. Action-classification tier: NOTIFY_AND_PROCEED — the
callback is external-facing (customer expects to be contacted) but
reversible (can be canceled / rescheduled).

Interim-body rule (00_MASTER §"interim-body rule")
==================================================
A real implementation requires an outbound-callback queue + worker
(parallel to the channel-adapter wave). No such queue exists in the
tree today. The full §3.3.1 contract is declared so the registry,
broker, schema validator, and authorisation gate can all reason
about this tool; ``execute()`` performs NO side effect.

Arc anchor: Arc 13 — the outbound queue naturally rides on the same
channel-adapter wave as send_email / send_sms. Flagged for founder
review in the WU3 report.
"""

# TODO(ARC13): replace this interim body once the outbound-callback
# queue ships in the Arc 13 channel-adapter wave.

from __future__ import annotations

from typing import Any

from app.policy.action_classification import ActionTier
from app.tools.base import LucielTool, ToolContext


class ScheduleCallbackTool(LucielTool):

    declared_tier = ActionTier.NOTIFY_AND_PROCEED

    @property
    def tool_id(self) -> str:
        return "schedule_callback"

    @property
    def display_name(self) -> str:
        return "Schedule callback"

    @property
    def description(self) -> str:
        return (
            "Schedule a follow-up callback for the customer at a "
            "specified time."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "callback_at": {
                    "type": "string",
                    "description": "ISO-8601 timestamp.",
                    "minLength": 1,
                },
                "contact": {
                    "type": "string",
                    "description": (
                        "Phone number or email to reach the customer."
                    ),
                    "minLength": 1,
                },
                "topic": {"type": "string", "minLength": 1},
                "notes": {"type": "string"},
            },
            "required": ["callback_at", "contact"],
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
        return ("pro", "enterprise")

    @property
    def execution_mode(self) -> str:
        return "in_process"

    async def execute(
        self,
        input: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        # Interim body — NO enqueue. The outbound-callback queue
        # ships in the Arc 13 channel-adapter wave.
        return {
            "success": False,
            "output": (
                "schedule_callback is registered but the outbound "
                "callback queue has not yet shipped (owning arc: "
                "ARC13). No callback was scheduled."
            ),
            "not_yet_available": True,
            "owning_arc": "ARC13",
        }
