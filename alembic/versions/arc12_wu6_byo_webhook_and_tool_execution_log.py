"""Arc 12 WU6 — byo_webhook_endpoints + tool_execution_log + RLS.

Revision ID: arc12_wu6_byo_webhook_and_tool_execution_log
Revises: arc12_wu4_sibling_call_grants
Create Date: 2026-05-29

Why this migration exists
-------------------------

Arc 12 WU6 ships the real ``bring_your_own_webhook`` body inside a
subprocess sandbox with the full §3.3.5 security envelope. Two new
tables back the work:

1. ``byo_webhook_endpoints`` — per-instance admin-registered BYO
   webhook configuration. One row per registered endpoint carries
   the URL, the input + output JSON schemas the sandbox validates
   against, and the egress allowlist (the resolved request host
   MUST match the allowlist or the subprocess refuses the call).
   Per ``§3.3.5`` / Decision #6, the full envelope is enforced at
   tool-config time and at dispatch — the URL alone is not enough.

2. ``tool_execution_log`` — general-purpose audit row per tool
   invocation. WU6 writes one row per BYO invocation carrying
   ``execution_mode``, input hash, output hash, latency,
   error class, and the circuit-breaker state at dispatch. The
   table is intentionally NOT BYO-specific: ``tool_id`` is a free
   string so WU5 (the sibling-runtime audit) can reuse the same
   table for ``call_sibling_luciel`` dispatches. WU5/WU6 share
   the structure; the only WU6-specific columns are the optional
   ``circuit_breaker_state`` and the ``error_class`` taxonomy
   (transport / timeout / schema_input / schema_output / circuit_open
   / egress_denied / other). Retention is governed by
   Decision #10 (tool-execution audit retention = same as
   conversation per tier); applying retention is out of scope for
   WU6 — the table just needs to exist with timestamps that the
   retention worker can read.

Schema decisions — ``byo_webhook_endpoints``
--------------------------------------------

* ``admin_id`` ``String(100)`` matching the Wall-1 convention.
* ``instance_id`` ``Integer`` matching ``instances.id`` per Arc 5.
* ``endpoint_url`` ``String(2048)`` — fits a fully qualified URL with
  a long path/query; matches the upper bound the existing webhook
  outbound paths use.
* ``input_schema`` / ``output_schema`` ``JSONB`` — admin-registered
  JSON Schemas. The WU1 minimal validator (``app/tools/schema.py``)
  runs both at dispatch.
* ``allowed_domains`` ``ARRAY(String)`` — explicit list of FQDNs the
  subprocess is allowed to reach. The sandbox resolves the
  ``endpoint_url`` host at dispatch and refuses if it is not in the
  list (case-insensitive, exact match — wildcards are deliberately
  out of scope until the documents schedule them).
* ``revoked_at`` ``DateTime`` nullable — §5.5 Pattern E soft-delete.
* Partial unique index on ``(admin_id, instance_id, endpoint_url)``
  over non-revoked rows so an admin can revoke + re-register the
  same URL without a constraint violation.

Schema decisions — ``tool_execution_log``
-----------------------------------------

* ``admin_id`` / ``instance_id`` for Wall-1 / Wall-3 scoping +
  RLS.
* ``tool_id`` ``String(64)`` matching the registry key.
* ``execution_mode`` ``String(20)`` — ``in_process`` or
  ``subprocess``.
* ``input_hash`` / ``output_hash`` ``String(64)`` — hex SHA-256 of
  the canonicalised input/output payloads. Hashes (not payloads)
  so the audit row is PII-free; the conversation log still carries
  the prompt/response history for retention-bounded inspection.
* ``latency_ms`` ``Integer`` — wall-clock duration of the dispatch
  including subprocess spawn (BYO) or in-process call (other).
* ``error_class`` ``String(40)`` — short taxonomy code; NULL on
  success. WU6 emits: ``transport`` (connection / TLS),
  ``timeout`` (subprocess killed at 30s), ``schema_input`` /
  ``schema_output`` (validation failure), ``circuit_open``
  (refused at dispatch by the breaker), ``egress_denied``
  (allowlist refused the resolved host), ``http_error`` (non-2xx
  response), ``other`` (catch-all). WU5 emits its own codes; the
  column is a free string.
* ``circuit_breaker_state`` ``String(20)`` nullable — one of
  ``closed`` | ``half_open`` | ``open`` at the moment the dispatch
  was attempted; NULL for tools without a breaker.
* ``error_message`` ``String(500)`` nullable — short human-readable
  description; truncated by the recorder. Optional.
* ``created_at`` ``DateTime(timezone=True)`` indexed for retention
  scans.

RLS posture (§3.7.5)
--------------------

Mirrors ``arc12_wu2_instance_tool_authorizations.py`` exactly on
both tables: ENABLE + FORCE + PERMISSIVE policy keyed on
``admin_id = current_setting('app.admin_id', true)`` for USING +
WITH CHECK. Fail-closed by construction when the GUC is unset.

Grants inherit from the Arc 9 C10.b ``ALTER DEFAULT PRIVILEGES``
sweep — no explicit GRANT issued here.

Rollback contract
-----------------

``alembic downgrade -1`` drops both tables, their indexes, and
RLS policies. Data-safe: dropping default-deny tables widens
visibility — never narrows it. BYO endpoints stop dispatching
because the registry row is gone; the WU6 tool body returns a
"not configured" failure rather than an outbound call.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB


revision = "arc12_wu6_byo_webhook_and_tool_execution_log"
down_revision = "arc12_wu4_sibling_call_grants"
branch_labels = None
depends_on = None


_BYO_TABLE = "byo_webhook_endpoints"
_LOG_TABLE = "tool_execution_log"


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    is_sqlite = _is_sqlite()

    # ------------------------------------------------------------------
    # byo_webhook_endpoints
    # ------------------------------------------------------------------
    op.create_table(
        _BYO_TABLE,
        sa.Column(
            "id", sa.Integer(), primary_key=True, autoincrement=True
        ),
        sa.Column(
            "admin_id",
            sa.String(100),
            sa.ForeignKey("admins.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
            comment=(
                "Wall-1 tenant boundary. RLS fences on this column."
            ),
        ),
        sa.Column(
            "instance_id",
            sa.Integer(),
            sa.ForeignKey("instances.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
            comment="Wall-3 instance boundary.",
        ),
        sa.Column(
            "endpoint_url",
            sa.String(2048),
            nullable=False,
            comment=(
                "Outbound URL the subprocess sandbox will POST to. "
                "The resolved host MUST appear in allowed_domains or "
                "the egress-allowlist check refuses the call."
            ),
        ),
        sa.Column(
            "input_schema",
            JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            comment=(
                "JSON Schema validated against the inbound payload "
                "BEFORE subprocess dispatch (§3.3.5)."
            ),
        ),
        sa.Column(
            "output_schema",
            JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            comment=(
                "JSON Schema validated against the webhook response "
                "AFTER subprocess returns. Malformed output ⇒ "
                "tool failure, NO retry (schema_output is terminal)."
            ),
        ),
        sa.Column(
            "allowed_domains",
            (
                ARRAY(sa.String(255))
                if not is_sqlite
                else sa.JSON()
            ),
            nullable=False,
            comment=(
                "Egress allowlist — exact-match FQDN list the "
                "subprocess is permitted to reach. Case-insensitive."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            server_onupdate=sa.func.now(),
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "§5.5 Pattern E soft-delete. NULL = live; non-NULL = "
                "revoked."
            ),
        ),
    )

    # Partial unique index — one live registration per
    # (admin, instance, endpoint_url). Postgres-only; SQLite tests
    # don't exercise the unique constraint.
    if not is_sqlite:
        op.create_index(
            "uq_byo_webhook_endpoints_active",
            _BYO_TABLE,
            ["admin_id", "instance_id", "endpoint_url"],
            unique=True,
            postgresql_where=sa.text("revoked_at IS NULL"),
        )
        op.create_index(
            "ix_byo_webhook_endpoints_lookup",
            _BYO_TABLE,
            ["admin_id", "instance_id"],
            postgresql_where=sa.text("revoked_at IS NULL"),
        )

    # ------------------------------------------------------------------
    # tool_execution_log
    # ------------------------------------------------------------------
    op.create_table(
        _LOG_TABLE,
        sa.Column(
            "id", sa.Integer(), primary_key=True, autoincrement=True
        ),
        sa.Column(
            "admin_id",
            sa.String(100),
            sa.ForeignKey("admins.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "instance_id",
            sa.Integer(),
            sa.ForeignKey("instances.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "tool_id",
            sa.String(64),
            nullable=False,
            comment=(
                "§3.3.1 tool_id. General-purpose: WU5 sibling runtime "
                "writes 'call_sibling_luciel' rows here too."
            ),
        ),
        sa.Column(
            "execution_mode",
            sa.String(20),
            nullable=False,
            comment="'in_process' | 'subprocess'.",
        ),
        sa.Column(
            "input_hash",
            sa.String(64),
            nullable=False,
            comment=(
                "Hex SHA-256 of canonicalised input payload. Hashes "
                "(not payloads) keep this audit row PII-free."
            ),
        ),
        sa.Column(
            "output_hash",
            sa.String(64),
            nullable=True,
            comment=(
                "Hex SHA-256 of canonicalised output payload; NULL "
                "when the dispatch produced no output (e.g. timeout)."
            ),
        ),
        sa.Column(
            "latency_ms",
            sa.Integer(),
            nullable=False,
            comment="Wall-clock duration of dispatch.",
        ),
        sa.Column(
            "error_class",
            sa.String(40),
            nullable=True,
            comment=(
                "Short taxonomy code on failure; NULL on success. "
                "WU6 emits: transport | timeout | schema_input | "
                "schema_output | circuit_open | egress_denied | "
                "http_error | other."
            ),
        ),
        sa.Column(
            "circuit_breaker_state",
            sa.String(20),
            nullable=True,
            comment=(
                "'closed' | 'half_open' | 'open' at dispatch attempt; "
                "NULL for tools without a breaker."
            ),
        ),
        sa.Column(
            "error_message",
            sa.String(500),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )

    # ------------------------------------------------------------------
    # RLS posture — both tables, mirror arc12_wu2.
    # ------------------------------------------------------------------
    if not is_sqlite:
        for tbl, policy_name in (
            (_BYO_TABLE, "byo_webhook_endpoints_tenant_isolation"),
            (_LOG_TABLE, "tool_execution_log_tenant_isolation"),
        ):
            op.execute(
                f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY;"
            )
            op.execute(
                f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY;"
            )
            op.execute(
                f"""
                CREATE POLICY {policy_name}
                ON {tbl}
                AS PERMISSIVE
                FOR ALL
                TO PUBLIC
                USING (admin_id = current_setting('app.admin_id', true))
                WITH CHECK (admin_id = current_setting('app.admin_id', true));
                """
            )


def downgrade() -> None:
    is_sqlite = _is_sqlite()

    if not is_sqlite:
        for tbl, policy_name in (
            (_LOG_TABLE, "tool_execution_log_tenant_isolation"),
            (_BYO_TABLE, "byo_webhook_endpoints_tenant_isolation"),
        ):
            op.execute(
                f"DROP POLICY IF EXISTS {policy_name} ON {tbl};"
            )
            op.execute(
                f"ALTER TABLE {tbl} NO FORCE ROW LEVEL SECURITY;"
            )
            op.execute(
                f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY;"
            )

    op.drop_table(_LOG_TABLE)

    if not is_sqlite:
        op.drop_index(
            "ix_byo_webhook_endpoints_lookup", table_name=_BYO_TABLE
        )
        op.drop_index(
            "uq_byo_webhook_endpoints_active", table_name=_BYO_TABLE
        )
    op.drop_table(_BYO_TABLE)
