"""call_sibling_luciel — v1 catalog tool (§3.3.2, §3.3.4).

Invokes a sibling Luciel instance with a delegated subtask. Action-
classification tier: NOTIFY_AND_PROCEED — a sibling call is itself
not a customer-facing side effect (the sibling's own tool calls run
through its own broker + tier gate), but it is a meaningful
cross-instance dispatch the runtime should surface.

WU5 wiring
==========
The five-check dispatch path (cycle detection, per-inbound fan-out
budget, master switch on both endpoints, live grant lookup, derived
context + sibling-access audit row) is implemented in
``app.tools.sibling_dispatch``. ``execute()`` is a thin adapter:
unpack the input, hand off to ``dispatch_sibling_call``, return the
result dict. The single Arc 14 seam is the structured
"authorized-and-dispatched" payload returned on the happy path —
when Arc 14's agentic loop lands, the seam swaps the structured
payload for the orchestrator round-trip response without touching
the guardrails.

``execution_mode`` is ``"in_process"`` per WU3 — the sibling call
runs inside the worker's event loop, not in a subprocess.
``requires_channels`` is empty — sibling composition is not a
channel adapter dependency.
"""

from __future__ import annotations

from typing import Any

from app.policy.action_classification import ActionTier
from app.tools.base import LucielTool, ToolContext
from app.tools.sibling_dispatch import dispatch_sibling_call


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
                "callee_instance_id": {"type": ["integer", "null"]},
                "caller_instance_id": {"type": ["integer", "null"]},
                "grant_id": {"type": ["integer", "null"]},
                "depth": {"type": ["integer", "null"]},
                "fan_out_count": {"type": ["integer", "null"]},
                "derived_context": {
                    "type": ["object", "null"],
                    "additionalProperties": True,
                },
                "error_reason": {"type": ["string", "null"]},
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
        # worker's event loop, not in a subprocess.
        return "in_process"

    async def execute(
        self,
        input: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        # The §3.3.1 input_schema validation already ran in the
        # broker before we got here — we can trust the shape.
        return dispatch_sibling_call(
            callee_instance_id=int(input["target_instance_id"]),
            task=str(input["task"]),
            payload=input.get("payload"),
            context=context,
        )
