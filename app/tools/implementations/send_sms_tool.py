"""send_sms — v1 catalog tool (§3.3.2).

Sends an SMS via the configured channel adapter. Action-
classification tier: NOTIFY_AND_PROCEED — outbound SMS is external-
facing, expected within the customer pattern, and recoverable by a
clarifying follow-up.

Interim-body rule (00_MASTER §"interim-body rule")
==================================================
The Twilio SMS adapter ships in Arc 13. Until then this tool
declares its full §3.3.1 contract but ``execute()`` performs NO
actual send. ``requires_channels={"sms"}`` documents the structural
dependency.
"""

# TODO(ARC13): replace this interim body with the Twilio adapter
# call once Arc 13 ships the channel-adapter infrastructure.

from __future__ import annotations

from typing import Any

from app.policy.action_classification import ActionTier
from app.tools.base import LucielTool, ToolContext


class SendSmsTool(LucielTool):

    declared_tier = ActionTier.NOTIFY_AND_PROCEED

    requires_channels = frozenset({"sms"})

    @property
    def tool_id(self) -> str:
        return "send_sms"

    @property
    def display_name(self) -> str:
        return "Send SMS"

    @property
    def description(self) -> str:
        return (
            "Send a short SMS message to the customer or an internal "
            "recipient via the configured SMS channel."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "E.164 phone number.",
                    "minLength": 4,
                    "pattern": r"^\+?[0-9\- ]+$",
                },
                "body": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1600,
                },
            },
            "required": ["to", "body"],
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
        # Interim body — NO side effect. The Twilio SMS adapter
        # ships in Arc 13.
        return {
            "success": False,
            "output": (
                "send_sms is registered but the channel adapter has "
                "not yet shipped (owning arc: ARC13, Twilio). No SMS "
                "was sent."
            ),
            "not_yet_available": True,
            "owning_arc": "ARC13",
        }
