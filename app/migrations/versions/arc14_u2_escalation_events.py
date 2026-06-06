"""Arc 14 U2 — escalation_events table + RLS.

Revision ID: arc14_u2_escalation_events
Revises: arc13_b_instance_channel_fields
Create Date: 2026-06-01

Why this migration exists
-------------------------

Arc 14 U2 builds the §3.4.5 Escalation Judgment Module. Every
escalation decision the module makes must leave a durable forensic
record: which of the four fixed signals fired, the signal's confidence,
a model-reasoning excerpt, the raw inputs the judge evaluated, and the
(admin_id, instance_id, session_id) scope at a timestamp. The
``escalation_events`` table is that record.

The four signals + thresholds are doctrinal (NOT admin-configurable):
  * Gate 1 INTAKE — explicit human request; strong negative sentiment.
  * Gate 2 OUTCOME — cannot confidently answer; high-value lead.
This table records which fixed signal fired, never a per-tenant rule.

Schema decisions
----------------
* ``admin_id`` is ``String(100)`` matching ``admins.id`` and the Wall-1
  column convention everywhere else.
* ``luciel_instance_id`` is ``Integer`` FK ``instances.id`` ON DELETE
  SET NULL — the forensic record survives instance deletion (matches
  the ``traces`` FK posture for the same scope column).
* ``session_id`` is ``String(100)`` non-null — every escalation belongs
  to a conversation turn.
* ``signal`` / ``gate`` are ``String`` columns with CHECK constraints
  pinning them to the doctrinal vocabularies (mirrors the Arc 12 WU4
  ``approval_state`` CHECK convention — strings + advisory tuple, no PG
  ENUM, renders cleanly on both Postgres and SQLite).
* ``signal_confidence`` is a nullable float (the firing signal's
  normalised confidence / score).
* ``reasoning_excerpt`` is ``Text`` (a short model-reasoning excerpt).
* ``signal_inputs`` is ``JSONB`` (the raw inputs the judge evaluated).

Indexes
-------
* ``ix_escalation_events_admin_id`` — implicit from the index=True on
  the admin_id column.
* ``ix_escalation_events_tenant_time`` on ``(admin_id, created_at)`` —
  the "show me every escalation for tenant X in time range Y" dashboard
  query (mirrors ``ix_admin_audit_logs_tenant_time``).
* ``ix_escalation_events_session`` on ``session_id`` — "every
  escalation for this conversation".
* ``ix_escalation_events_signal`` — implicit from index=True on signal.

RLS posture (§3.7.5)
--------------------
Mirrors the Arc 12 WU4 ``sibling_call_grants`` policy exactly:

1. ``ENABLE ROW LEVEL SECURITY`` — turn RLS on.
2. ``FORCE ROW LEVEL SECURITY`` — apply RLS to the table owner too
   (Arc 9 C10.a doctrine; ``luciel_app`` is NOBYPASSRLS but FORCE seals
   the ownership escape).
3. PERMISSIVE policy ``escalation_events_tenant_isolation`` on the wall
   column ``admin_id``. USING + WITH CHECK both strict so reads and
   writes are equally fenced. When ``app.admin_id`` is unset,
   ``current_setting(..., true)`` returns NULL; ``admin_id = NULL`` is
   NULL in three-valued logic; RLS treats NULL as "deny." Fail-closed
   by construction.

Grants
------
``ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT,
UPDATE, DELETE ON TABLES TO luciel_app`` was installed at Arc 9 C10.b.
Tables created after that migration inherit the grant; no explicit
grant is issued here.

Rollback contract
-----------------
``alembic downgrade -1`` drops the table, indexes, and RLS policy.
Data-safe: dropping a tenant-scoped forensic table widens nothing.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision = "arc14_u2_escalation_events"
down_revision = "arc13_b_instance_channel_fields"
branch_labels = None
depends_on = None


_TABLE = "escalation_events"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Create the table.
    # ------------------------------------------------------------------
    op.create_table(
        _TABLE,
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
            "luciel_instance_id",
            sa.Integer(),
            sa.ForeignKey("instances.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
            comment=(
                "Instance the escalation happened under. SET NULL on "
                "delete — the forensic record survives instance removal."
            ),
        ),
        sa.Column(
            "session_id",
            sa.String(100),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "user_id",
            sa.String(100),
            nullable=True,
        ),
        sa.Column(
            "signal",
            sa.String(40),
            nullable=False,
            index=True,
            comment=(
                "Which of the four fixed §3.4.5 signals fired. NOT "
                "admin-configurable; pinned by the CHECK constraint."
            ),
        ),
        sa.Column(
            "gate",
            sa.String(16),
            nullable=False,
            comment="'intake' (pre-PLAN) or 'outcome' (post-REFLECT).",
        ),
        sa.Column(
            "signal_confidence",
            sa.Float(),
            nullable=True,
            comment=(
                "The firing signal's normalised confidence / score."
            ),
        ),
        sa.Column(
            "reasoning_excerpt",
            sa.Text(),
            nullable=True,
            comment="Short model-reasoning / decision excerpt.",
        ),
        sa.Column(
            "signal_inputs",
            JSONB(),
            nullable=True,
            comment=(
                "Raw inputs the judge evaluated (message, classifier "
                "outputs, loop confidence, grounding, etc.)."
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
        sa.CheckConstraint(
            "signal IN ("
            "'explicit_human_request', 'strong_negative_sentiment', "
            "'cannot_confidently_answer', 'high_value_lead')",
            name="ck_escalation_events_signal",
        ),
        sa.CheckConstraint(
            "gate IN ('intake', 'outcome')",
            name="ck_escalation_events_gate",
        ),
    )

    # "Show me every escalation for tenant X in time range Y."
    op.create_index(
        "ix_escalation_events_tenant_time",
        _TABLE,
        ["admin_id", "created_at"],
    )
    # "Every escalation for this conversation."
    op.create_index(
        "ix_escalation_events_session",
        _TABLE,
        ["session_id"],
    )

    # ------------------------------------------------------------------
    # 2. RLS posture — mirrors arc12_wu4_sibling_call_grants exactly.
    # ------------------------------------------------------------------
    op.execute(
        f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY;"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        f"""
        CREATE POLICY escalation_events_tenant_isolation
        ON {_TABLE}
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (admin_id = current_setting('app.admin_id', true))
        WITH CHECK (admin_id = current_setting('app.admin_id', true));
        """
    )


def downgrade() -> None:
    # Reverse order:

    # 2. RLS teardown.
    op.execute(
        f"DROP POLICY IF EXISTS "
        f"escalation_events_tenant_isolation "
        f"ON {_TABLE};"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY;"
    )

    # 1. Drop indexes + table.
    op.drop_index(
        "ix_escalation_events_session", table_name=_TABLE
    )
    op.drop_index(
        "ix_escalation_events_tenant_time", table_name=_TABLE
    )
    op.drop_table(_TABLE)
