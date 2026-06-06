"""Unit 13e — session_summaries table + RLS (§3.4.10 cross-session memory).

Revision ID: unit13e_session_summaries
Revises: unit13e_session_resolved_lead_id
Create Date: 2026-06-06

Why this migration exists
-------------------------

§3.4.10 specifies a PERSISTED session summary as the cross-session memory
store, with its own retention clock (90 days Free / 1 year Pro). Before
Unit 13e, summaries were folded into the ``leads.summary`` column only —
there was no dedicated store keyed on the participant (resolved_lead_id)
with its own retention TTL. This migration creates that store.

It is written by the finalization pipeline at session end (the §3.4.7
summarization moment) and is the source the cross-session retriever reads
as the summary leg (the message-history reader stays as the raw-history
leg).

Schema decisions (mirror arc14_u4_leads conventions)
-----------------------------------------------------
* ``admin_id`` ``String(100)`` FK ``admins.id`` ON DELETE RESTRICT — the
  Wall-1 column convention; RLS fences on it.
* ``luciel_instance_id`` ``Integer`` FK ``instances.id`` ON DELETE SET
  NULL — the summary survives instance deletion (matches leads).
* ``resolved_lead_id`` ``String(100)`` nullable — the §3.4.8 session-key
  participant. NULL = anonymous (still persists, but not a cross-session
  recall anchor since a NULL never matches another NULL, §3.4.9).
* ``session_id`` ``String(100)`` non-null — every summary belongs to a
  session.
* ``summary`` ``Text`` non-null — the §3.4.7 structured summary text.

Indexes
-------
* ``ix_session_summaries_admin_id`` — implicit from index=True on admin_id.
* ``ix_session_summaries_luciel_instance_id`` — implicit from index=True.
* ``ix_session_summaries_resolved_lead_id`` — the cross-session recall
  anchor lookup.
* ``ix_session_summaries_session_id`` — implicit from index=True.
* ``ix_session_summaries_tenant_time`` on ``(admin_id, created_at)`` — the
  retention-sweep "summaries older than TTL for tenant X" query.

RLS posture (§3.7.5)
--------------------
Mirrors arc14_u4_leads exactly: ENABLE + FORCE ROW LEVEL SECURITY +
PERMISSIVE policy on ``admin_id``. Fail-closed when ``app.admin_id`` is
unset (NULL comparison denies).

Rollback contract
-----------------
``alembic downgrade -1`` drops the table, indexes, and RLS policy.
Data-safe: dropping a tenant-scoped table widens nothing.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "unit13e_session_summaries"
down_revision = "unit13e_session_resolved_lead_id"
branch_labels = None
depends_on = None


_TABLE = "session_summaries"


def upgrade() -> None:
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
            comment="Wall-1 tenant boundary. RLS fences on this column.",
        ),
        sa.Column(
            "luciel_instance_id",
            sa.Integer(),
            sa.ForeignKey("instances.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
            comment=(
                "Instance the summary was captured under. SET NULL on "
                "delete — the summary survives instance removal."
            ),
        ),
        sa.Column(
            "resolved_lead_id",
            sa.String(100),
            nullable=True,
            index=True,
            comment=(
                "§3.4.8 session-key participant. NULL = anonymous (never "
                "a cross-session recall anchor, §3.4.9)."
            ),
        ),
        sa.Column(
            "session_id",
            sa.String(100),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "summary",
            sa.Text(),
            nullable=False,
            comment="§3.4.7 structured conversation summary text.",
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
    )

    # "Summaries older than TTL for tenant X" — the retention-sweep query.
    op.create_index(
        "ix_session_summaries_tenant_time",
        _TABLE,
        ["admin_id", "created_at"],
    )

    # ------------------------------------------------------------------
    # RLS posture — mirrors arc14_u4_leads exactly.
    # ------------------------------------------------------------------
    op.execute(
        f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY;"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        f"""
        CREATE POLICY session_summaries_tenant_isolation
        ON {_TABLE}
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (admin_id = current_setting('app.admin_id', true))
        WITH CHECK (admin_id = current_setting('app.admin_id', true));
        """
    )


def downgrade() -> None:
    op.execute(
        f"DROP POLICY IF EXISTS session_summaries_tenant_isolation "
        f"ON {_TABLE};"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY;"
    )

    op.drop_index("ix_session_summaries_tenant_time", table_name=_TABLE)
    op.drop_table(_TABLE)
