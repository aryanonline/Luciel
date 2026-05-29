"""lookup_property — v1 catalog tool (§3.3.2).

Read-only property/listing lookup. Action-classification tier:
ROUTINE — this is exactly the reading-shaped, low-blast-radius work
Recap §4 names as not consequential.

Interim-body rule (00_MASTER §"interim-body rule")
==================================================
The real implementation requires an admin-configured property source
(admin CSV upload, MLS connector, etc.). No such source exists in
the tree today. The full §3.3.1 contract is declared so the
registry, broker, schema validator, and authorisation gate can all
reason about this tool; ``execute()`` performs NO side effect and
returns a structured "not yet available" dict.

Arc anchor: ARC14. The admin-configured property source (admin CSV
upload / MLS connector) is the data plane the Arc-14 knowledge stack
owns; the WU3 interim-body harness pins ``owning_arc="ARC14"`` as the
single source of truth for the seam (see
``tests/tools/test_arc12_wu3_catalog.py::_INTERIM_TOOLS``).
"""

# TODO(ARC14): replace this interim body once the Arc-14 admin-configured
# property source (CSV upload / MLS connector) ships. The interim
# contract is enforced by tests/tools/test_arc12_wu3_catalog.py.

from __future__ import annotations

from typing import Any

from app.policy.action_classification import ActionTier
from app.tools.base import LucielTool, ToolContext


class LookupPropertyTool(LucielTool):

    declared_tier = ActionTier.ROUTINE

    @property
    def tool_id(self) -> str:
        return "lookup_property"

    @property
    def display_name(self) -> str:
        return "Look up property"

    @property
    def description(self) -> str:
        return (
            "Look up a property listing by id, address, or filter "
            "criteria from the configured property source."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "property_id": {"type": "string", "minLength": 1},
                "address": {"type": "string", "minLength": 1},
                "filters": {
                    "type": "object",
                    "additionalProperties": True,
                },
            },
            "additionalProperties": False,
        }

    @property
    def output_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "output": {"type": "string"},
                "results": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
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
        return "in_process"

    async def execute(
        self,
        input: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        # Interim body — NO side effect, NO real lookup. The
        # admin-configured property source ships in a later arc.
        return {
            "success": False,
            "output": (
                "lookup_property is registered but no admin-configured "
                "property source (CSV / MLS connector) exists yet; the "
                "owning data plane ships in Arc 14. No results were "
                "returned."
            ),
            "results": [],
            "not_yet_available": True,
            "owning_arc": "ARC14",
        }
