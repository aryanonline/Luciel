"""lookup_record — v1 catalog tool (§3.3.2). Arc 17 live implementation.

Read-only record lookup. Action-classification tier: ROUTINE — this is
exactly the reading-shaped, low-blast-radius work Recap §4 names as not
consequential.

Correctness boundary (§3.2, load-bearing)
=========================================
``lookup_record`` returns LIVE, EXACT records from its configured
``record_source`` connection. It reads the backing source on EVERY call
and returns the matched rows as live records. It is NOT the knowledge
store: it is never blended with the vector / graph retrieval path. A
result is always framed as coming from the admin's own record source.

Domain-agnostic (Locked Decision #5)
====================================
The tool gates on the ``record_source`` connector category (an
admin-configured generic record provider — e.g. an uploaded CSV) and
carries NO vertical-specific wording. The input schema is generic
(``record_id`` / ``query`` / ``filters``); the query semantics reason
only about structural ``id`` / ``record_id`` identity columns, never
vertical columns.

Arc anchor: ARC17 (record-source data infrastructure). The Architecture
§3.2 named the data source as "an admin-uploaded CSV or a live data
connector" but did not assign an owning arc; the founder assigned it to
Arc 17 and this body is the live implementation. See
``ARC17_LOOKUP_RECORD_AMENDMENT.md`` at the repo root.

Where the source lives, and the s3 deploy gate
==============================================
The connection's NON-SECRET ``config_json.store_ref`` names the storage
location (a local path / ``file://`` URI, or an ``s3://`` URI). The
resolver dispatches by scheme:
  * local / file:// → ``LocalFileRecordSource`` (CSV via csv.DictReader),
  * s3://           → ``S3RecordSource`` (real boto3), DEPLOY-GATED.
With ``record_source_live_enabled`` False (the boot-safe default) an
s3:// store_ref returns an HONEST deploy-gated failure — never a fake
success and never a crash; no boto3 client is constructed.
"""
from __future__ import annotations

import logging
from typing import Any

from app.policy.action_classification import ActionTier
from app.tools.base import LucielTool, ToolContext

logger = logging.getLogger(__name__)


class LookupRecordTool(LucielTool):

    declared_tier = ActionTier.ROUTINE

    # Arc 15 WU4/WU5 — connection-contract gate (§3.3.2). The
    # ``record_source`` connector connects LIVE (Arc 17), so a configured
    # source yields a ``connected`` row and the gate admits dispatch. The
    # gate already guarantees a live ``connected`` row before execute()
    # runs, but execute() still defends against a missing row / store_ref.
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
                "truncated": {"type": "boolean"},
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
        record_id = input.get("record_id")
        query = input.get("query")
        filters = input.get("filters")

        # Thread the DB session from ToolContext (same pattern the
        # DefaultDenyToolAuthorizer uses). No session → honest failure,
        # never a crash.
        session = context.session
        if session is None:
            return self._fail(
                "Record lookup could not access a database session, so "
                "the configured record source could not be resolved."
            )

        # Resolve the live record_source connection. Gate-3 guarantees a
        # live ``connected`` row exists, but defend against its absence.
        from app.repositories.instance_connection_repository import (
            InstanceConnectionRepository,
        )

        repo = InstanceConnectionRepository(session)
        row = repo.get_live_by_type(
            admin_id=context.admin_id,
            instance_id=context.instance_id,
            connection_type="record_source",
        )
        if row is None:
            return self._fail(
                "No live record source is configured for this instance, "
                "so there is nothing to look up against."
            )

        config = row.config_json or {}
        store_ref = config.get("store_ref")
        if not store_ref or not str(store_ref).strip():
            return self._fail(
                "The configured record source has no store_ref (storage "
                "location), so its records cannot be read."
            )

        from app.core.config import settings
        from app.integrations.record_source.base import RecordSourceError
        from app.integrations.record_source.resolver import (
            RecordSourceUnavailableError,
            resolve_record_source,
        )

        try:
            source = resolve_record_source(store_ref, settings)
            rows, truncated = source.query(
                record_id=record_id,
                query=query,
                filters=filters,
            )
        except RecordSourceUnavailableError as exc:
            # Honest deploy-gated / unreachable-source failure.
            return self._fail(str(exc))
        except RecordSourceError as exc:
            logger.warning(
                "lookup_record: record source unreadable admin=%s "
                "instance=%s: %s",
                context.admin_id, context.instance_id, exc,
            )
            return self._fail(
                f"The configured record source could not be read: {exc}"
            )

        if not rows:
            return {
                "success": True,
                "output": "No matching records in your record source.",
                "results": [],
            }

        count = len(rows)
        suffix = " (results truncated)" if truncated else ""
        return {
            "success": True,
            "output": (
                f"{count} record(s) found in your record source{suffix}."
            ),
            "results": rows,
            "truncated": truncated,
        }

    @staticmethod
    def _fail(reason: str) -> dict[str, Any]:
        """Honest non-side-effecting failure (the lookup did not run)."""
        return {
            "success": False,
            "output": reason,
            "results": [],
        }
