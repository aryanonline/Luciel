"""send_email — v1 catalog tool (§3.3.2).

Sends an email via the configured channel adapter. Action-
classification tier: NOTIFY_AND_PROCEED — an outbound email is
external-facing but reversible (within reason — sending one to the
wrong recipient is recoverable by clarification) and is expected
within the customer-facing pattern.

Interim-body rule (00_MASTER §"interim-body rule")
==================================================
The SES email adapter ships in Arc 13. Until then this tool declares
its full §3.3.1 contract (so the registry, broker, schema validator,
and authorisation gate can all reason about it) but ``execute()``
performs NO actual send. ``requires_channels={"email"}`` documents
the structural dependency.
"""

# TODO(ARC13): replace this interim body with the SES adapter call
# once Arc 13 ships the channel-adapter infrastructure.

from __future__ import annotations

from typing import Any

from app.policy.action_classification import ActionTier
from app.tools.base import LucielTool, ToolContext


class SendEmailTool(LucielTool):

    declared_tier = ActionTier.NOTIFY_AND_PROCEED

    requires_channels = frozenset({"email"})

    @property
    def tool_id(self) -> str:
        return "send_email"

    @property
    def display_name(self) -> str:
        return "Send email"

    @property
    def description(self) -> str:
        return (
            "Send an email to the customer or an internal recipient "
            "via the configured email channel."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient email address.",
                    "minLength": 3,
                },
                "subject": {"type": "string", "minLength": 1},
                "body": {"type": "string", "minLength": 1},
            },
            "required": ["to", "subject", "body"],
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
        # Interim body — NO side effect. The SES email adapter
        # ships in Arc 13.
        return {
            "success": False,
            "output": (
                "send_email is registered but the channel adapter has "
                "not yet shipped (owning arc: ARC13, SES). No email "
                "was sent."
            ),
            "not_yet_available": True,
            "owning_arc": "ARC13",
        }
