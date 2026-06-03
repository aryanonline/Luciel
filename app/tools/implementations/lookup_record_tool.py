"""lookup_record — v1 catalog tool (§3.3.2).

Read-only record lookup. Action-classification tier: ROUTINE — this is
exactly the reading-shaped, low-blast-radius work Recap §4 names as not
consequential.

Domain-agnostic (Locked Decision #5)
====================================
The tool gates on the ``record_source`` connector category (an
admin-configured generic record provider — e.g. an uploaded CSV) and
carries NO vertical-specific wording. The configured ``record_source``
connection supplies the backing data; the input schema is generic
(``record_id`` / ``query`` / ``filters``).

Interim-body rule (00_MASTER §"interim-body rule")
==================================================
The real implementation requires an admin-configured record source. No
such source body exists in the tree today. The full §3.3.1 contract is
declared so the registry, broker, schema validator, and authorisation
gate can all reason about this tool; ``execute()`` performs NO side
effect and returns a structured "not yet available" dict.

Arc anchor: UNASSIGNED. Architecture §3.3.2 names the data source as
"MLS or admin-uploaded CSV" but does NOT assign an owning arc to that
record-source infrastructure. This is a documented gap flagged for
founder review in the Arc 12 closeout — it is NOT confidently an Arc 14
deliverable. The interim-body harness pins ``owning_arc="UNASSIGNED"``
as the seam marker (see
``tests/tools/test_arc12_wu3_catalog.py::_INTERIM_TOOLS``).
"""

# TODO(ARC-UNASSIGNED): replace this interim body once the founder
# assigns an owning arc for the admin-configured record source
# (CSV upload / external record connector). The interim contract is
# enforced by tests/tools/test_arc12_wu3_catalog.py.

from __future__ import annotations

from typing import Any

from app.policy.action_classification import ActionTier
from app.tools.base import LucielTool, ToolContext


class LookupRecordTool(LucielTool):

    declared_tier = ActionTier.ROUTINE

    # Arc 15 WU4/WU5 — connection-contract gate (§3.3.2). The
    # ``record_source`` connector (admin CSV upload) connects LIVE in
    # this slice, so a configured CSV source yields a ``connected`` row
    # and the WU5 gate admits dispatch.
    requires_connection = "record_source"

    @property
    def tool_id(self) -> str:
        return "lookup_record"

    @property
    def display_name(self) -> str:
        return "Look up record"

    @property
    def description(self) -> str:
        return (
            "Look up a record by id or filter criteria from the "
            "configured record source."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "record_id": {"type": "string", "minLength": 1},
                "query": {"type": "string", "minLength": 1},
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
        # admin-configured record source ships in a later arc.
        return {
            "success": False,
            "output": (
                "lookup_record is registered but no admin-configured "
                "record source (CSV / external connector) exists yet "
                "(owning arc unassigned in the canonical documents — "
                "founder review). No results were returned."
            ),
            "results": [],
            "not_yet_available": True,
            "owning_arc": "UNASSIGNED",
        }
