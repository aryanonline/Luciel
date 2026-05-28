"""call_sibling_luciel — v1 catalog tool (§3.3.2, §3.3.4).

Invokes a sibling Luciel instance with a delegated subtask. Action-
classification tier: NOTIFY_AND_PROCEED — a sibling call is itself
not a customer-facing side effect (the sibling's own tool calls run
through its own broker + tier gate), but it is a meaningful
cross-instance dispatch the runtime should surface.

Interim-body rule (00_MASTER §"interim-body rule")
==================================================
The sibling composition runtime — cycle detection, per-inbound
fan-out budget, grant lookup, derived context, sibling-access audit
row — is the Arc 12 WU5 deliverable. Until WU5 lands, this tool
declares its full §3.3.1 contract (so the registry, broker, schema
validator, and authorisation gate can all reason about it) and
``execute()`` returns a structured "not yet available" dict.

``execution_mode`` is ``"in_process"`` per WU3 — the sibling call
runs inside the worker's event loop, not in a subprocess.
``requires_channels`` is empty — sibling composition is not a
channel adapter dependency.
"""

# TODO(ARC12_WU5): replace this interim body with the real sibling
# composition runtime (cycle detection + per-inbound fan-out budget
# + grant lookup + sibling-access audit row).

from __future__ import annotations

from typing import Any

from app.policy.action_classification import ActionTier
from app.tools.base import LucielTool, ToolContext


class CallSiblingLucielTool(LucielTool):

    declared_tier = ActionTier.NOTIFY_AND_PROCEED

    @property
    def tool_id(self) -> str:
        return "call_sibling_luciel"

    @property
    def display_name(self) -> str:
        return "Call sibling Luciel"

    @property
    def description(self) -> str:
        return (
            "Delegate a subtask to a sibling Luciel instance authorised "
            "via a live sibling_call_grants row."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target_instance_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Integer PK of the callee instance. Must "
                        "have a live grant authorising the caller "
                        "instance to call it."
                    ),
                },
                "task": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Natural-language description of the "
                        "subtask the sibling should handle."
                    ),
                },
                "payload": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": (
                        "Optional structured payload passed alongside "
                        "the task description."
                    ),
                },
            },
            "required": ["target_instance_id", "task"],
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
        # Sibling composition is unavailable on free per Decision #19
        # / §3.3.4 master switch (composition_enabled = pro/enterprise
        # only).
        return ("pro", "enterprise")

    @property
    def execution_mode(self) -> str:
        # In-process per WU3: sibling dispatch runs inside the
        # worker's event loop, not in a subprocess. WU5 wires the
        # real dispatch path.
        return "in_process"

    async def execute(
        self,
        input: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        # Interim body — NO dispatch. The sibling composition
        # runtime ships in Arc 12 WU5.
        return {
            "success": False,
            "output": (
                "call_sibling_luciel is registered but the sibling "
                "composition runtime has not yet shipped (owning "
                "arc: ARC12_WU5). No sibling was invoked."
            ),
            "not_yet_available": True,
            "owning_arc": "ARC12_WU5",
        }
