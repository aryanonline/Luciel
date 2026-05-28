"""bring_your_own_webhook — v1 catalog tool (§3.3.2, §3.3.5).

Dispatches a customer-configured outbound webhook with the full
security envelope from §3.3.5 (Decisions #5 / #6): subprocess
isolation, 30s hard timeout, input/output JSON-Schema validation,
restricted egress allowlist, transport-error retry with exponential
backoff, per-endpoint circuit breaker, audit row per invocation.

Action-classification tier: APPROVAL_REQUIRED — a BYO webhook fires
admin-defined code against an admin-defined endpoint with effects
the platform cannot reason about. The senior-advisor default Recap
§4 names for unknown-blast-radius work is approval-required.

``execution_mode`` is ``"subprocess"`` (Decision #5) — the only
catalog tool that runs outside the worker process.

Interim-body rule (00_MASTER §"interim-body rule")
==================================================
The subprocess sandbox + retry + circuit-breaker + egress allowlist
+ audit row is the Arc 12 WU6 deliverable. Until WU6 lands, this
tool declares its full §3.3.1 contract (so the registry, broker,
schema validator, and authorisation gate can all reason about it)
and ``execute()`` performs NO outbound call — it returns a
structured "not yet available" dict.

Note on classification: the broker enforces APPROVAL_REQUIRED
BEFORE calling ``execute``, so even with this interim body the
correct gate fires on dispatch. A test asserting "interim body
returns the not-yet-available dict" must bypass the classifier
(see tests/tools/test_arc12_wu3_catalog.py).
"""

# TODO(ARC12_WU6): replace this interim body with the real
# subprocess-sandboxed webhook dispatch path.

from __future__ import annotations

from typing import Any

from app.policy.action_classification import ActionTier
from app.tools.base import LucielTool, ToolContext


class BringYourOwnWebhookTool(LucielTool):

    declared_tier = ActionTier.APPROVAL_REQUIRED

    @property
    def tool_id(self) -> str:
        return "bring_your_own_webhook"

    @property
    def display_name(self) -> str:
        return "Bring-your-own webhook"

    @property
    def description(self) -> str:
        return (
            "Dispatch a customer-configured outbound webhook in a "
            "sandboxed subprocess with input/output schema validation, "
            "egress allowlist, retry policy, and circuit breaker."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "endpoint_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Identifier of the admin-registered BYO "
                        "endpoint (URL + schemas + allowlist + "
                        "circuit-breaker state live there)."
                    ),
                },
                "payload": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": (
                        "Payload validated against the endpoint's "
                        "admin-registered input schema BEFORE "
                        "subprocess dispatch."
                    ),
                },
            },
            "required": ["endpoint_id", "payload"],
            "additionalProperties": False,
        }

    @property
    def output_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "output": {"type": "string"},
                "response": {
                    "type": "object",
                    "additionalProperties": True,
                },
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
        # Decision #5: BYO webhooks run in a subprocess so a hung or
        # crashing webhook cannot take the worker down with it.
        return "subprocess"

    async def execute(
        self,
        input: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        # Interim body — NO outbound call, NO subprocess spawn. The
        # subprocess sandbox + retry + circuit-breaker + audit row
        # ship in Arc 12 WU6.
        return {
            "success": False,
            "output": (
                "bring_your_own_webhook is registered but the "
                "subprocess sandbox has not yet shipped (owning arc: "
                "ARC12_WU6). No webhook was dispatched."
            ),
            "not_yet_available": True,
            "owning_arc": "ARC12_WU6",
        }
