"""push_to_crm — v1 catalog tool (§3.3.2).

Pushes a lead / interaction record to an external CRM. Action-
classification tier: NOTIFY_AND_PROCEED — a CRM row is external,
visible to the customer's sales team, but reversible (the row can
be edited or deleted in the CRM).

Interim-body rule (00_MASTER §"interim-body rule")
==================================================
Per 01_WORKUNITS.md WU3, push_to_crm runs via the BYO / webhook-
outbound path. That subprocess sandbox is the WU6 deliverable in
this same arc. Until WU6 lands, push_to_crm declares its full
§3.3.1 contract but ``execute()`` performs NO side effect — it
returns a structured "not yet available" dict.

Arc anchor (partial): Architecture §3.3.2 names three CRM paths —
"HubSpot, Salesforce, custom webhook." The custom-webhook path can
ride Arc 12 WU6's BYO outbound mechanism (anchor: ARC12_WU6). The
native HubSpot/Salesforce connector paths have NO owning arc assigned
in the canonical documents — that is a document gap flagged for
founder review in the Arc 12 closeout. Do not treat native-CRM
support as scheduled until the founder assigns an arc.
"""

# TODO(ARC12_WU6): replace this interim body with a dispatch through
# the BYO webhook outbound path once WU6 ships the subprocess
# sandbox.

from __future__ import annotations

from typing import Any

from app.policy.action_classification import ActionTier
from app.tools.base import LucielTool, ToolContext


class PushToCrmTool(LucielTool):

    declared_tier = ActionTier.NOTIFY_AND_PROCEED

    @property
    def tool_id(self) -> str:
        return "push_to_crm"

    @property
    def display_name(self) -> str:
        return "Push to CRM"

    @property
    def description(self) -> str:
        return (
            "Push a lead or interaction record to the configured "
            "external CRM."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "record_type": {
                    "type": "string",
                    "enum": ["lead", "contact", "interaction", "note"],
                },
                "payload": {
                    "type": "object",
                    "additionalProperties": True,
                },
            },
            "required": ["record_type", "payload"],
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
        # Interim body — NO outbound call. The BYO webhook subprocess
        # sandbox (the underlying outbound transport) ships in WU6.
        return {
            "success": False,
            "output": (
                "push_to_crm is registered but the BYO / webhook-"
                "outbound path it dispatches through has not yet "
                "shipped (owning arc: ARC12_WU6). No CRM record was "
                "created."
            ),
            "not_yet_available": True,
            "owning_arc": "ARC12_WU6",
        }
