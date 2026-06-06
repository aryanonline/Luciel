"""Unit 13g — budget counter write-through: Postgres authoritative store + RLS.

Revision ID: unit13g_budget_counter_writethrough
Revises: unit13e_session_summaries
Create Date: 2026-06-06

Why this migration exists
-------------------------

The conversation budget counter (§3.4.1b) was Redis-ONLY: the live
per-period count lived in ``luciel:budget:count:{admin}:{instance}:
{period}`` with a per-session SETNX marker for fire-once idempotency.
The §4.5 founder ruling (line 1332/1360) makes Postgres the SOURCE OF
TRUTH and Redis a cache. This migration lands the authoritative store:

1. ``conversation_budget_counter`` — the per-period count, UNIQUE on
   ``(admin_id, instance_id, billing_period_start)``. One row per open
   billing period per instance, holding the authoritative conversation
   count. ``billing_period_start`` is the ISO date STRING the Redis key
   uses (e.g. ``'2026-06-01'``), stored as ``VARCHAR`` so the two stores
   key on byte-identical anchors — no timezone/cast skew between the hot
   cache and the durable counter.

2. ``conversation_counted_sessions`` — the per-session idempotency
   record, UNIQUE on ``(admin_id, session_id)``. This row is the SINGLE
   authority for "this session has been counted" across BOTH stores. The
   write path inserts it with ``ON CONFLICT DO NOTHING`` in the same
   transaction as the counter increment; a re-fire of the same session
   (REFLECT-loop iteration, or a Redis-outage retry after a Redis-path
   count) collides on this unique row and the increment is skipped. This
   is what guarantees exactly-once across the Redis path AND the Postgres
   path — the cardinal "no double-charge" invariant.

Why a counter row instead of reusing the overage ledger
--------------------------------------------------------

``conversation_overage_ledger`` (arc18) is the CYCLE-CLOSE snapshot — one
row written at ``invoice.paid`` after the period is reset. It is not a
live counter and cannot be incremented mid-period without losing the
close-time audit semantics. The write-through needs a MID-PERIOD
authoritative counter, which is what ``conversation_budget_counter`` is.

RLS posture (§3.7.2b)
---------------------

Both tables carry tenant data, so both get ENABLE + FORCE ROW LEVEL
SECURITY + a PERMISSIVE policy fencing on ``admin_id`` (USING + WITH
CHECK), exactly like ``conversation_overage_ledger`` /
``session_summaries`` / ``leads``. Fail-closed when ``app.admin_id`` is
unset: ``current_setting('app.admin_id', true)`` returns NULL, the
``admin_id = NULL`` comparison is NULL in three-valued logic, and RLS
treats NULL as deny. The Arc 9 C10.b default-privileges grant is
inherited (tables created after that migration get SELECT/INSERT/UPDATE/
DELETE for luciel_app automatically).

Rollback contract
-----------------

``alembic downgrade -1`` drops both RLS policies, the FORCE/ENABLE flags,
the indexes, and both tables. Data-lossy in the dropping direction (the
live counts are lost) but Redis still carries the hot counter, so the
gate keeps working through a downgrade; the counts repopulate as
sessions fire.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "unit13g_budget_counter_writethrough"
down_revision = "unit13e_session_summaries"
branch_labels = None
depends_on = None


_COUNTER = "conversation_budget_counter"
_COUNTED = "conversation_counted_sessions"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. conversation_budget_counter — the authoritative per-period count.
    # ------------------------------------------------------------------
    op.create_table(
        _COUNTER,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "admin_id",
            sa.String(100),
            sa.ForeignKey("admins.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
            comment="Wall-1 tenant boundary. RLS fences on this column.",
        ),
        sa.Column(
            "instance_id",
            sa.Integer(),
            nullable=False,
            comment=(
                "Per-instance scope. Integer (not FK) so the counter "
                "outlives an instance soft-delete, mirroring the ledger."
            ),
        ),
        sa.Column(
            "billing_period_start",
            sa.String(32),
            nullable=False,
            comment=(
                "ISO date string anchor (e.g. '2026-06-01'), byte-identical "
                "to the Redis key's period_start so the two stores agree."
            ),
        ),
        sa.Column(
            "conversation_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="Authoritative conversation count for the open period.",
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
        sa.UniqueConstraint(
            "admin_id",
            "instance_id",
            "billing_period_start",
            name="uq_budget_counter_period",
        ),
    )

    # ------------------------------------------------------------------
    # 2. conversation_counted_sessions — the per-session idempotency row.
    # ------------------------------------------------------------------
    op.create_table(
        _COUNTED,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "admin_id",
            sa.String(100),
            sa.ForeignKey("admins.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
            comment="Wall-1 tenant boundary. RLS fences on this column.",
        ),
        sa.Column(
            "instance_id",
            sa.Integer(),
            nullable=False,
            comment="Per-instance scope (Integer, not FK; outlives delete).",
        ),
        sa.Column(
            "billing_period_start",
            sa.String(32),
            nullable=False,
            comment="The period anchor this session was counted against.",
        ),
        sa.Column(
            "session_id",
            sa.String(128),
            nullable=False,
            comment=(
                "Conversation identifier. The UNIQUE(admin_id, session_id) "
                "row is the single authority for 'session counted' across "
                "BOTH Redis and Postgres — the exactly-once commit point."
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
        sa.UniqueConstraint(
            "admin_id",
            "session_id",
            name="uq_counted_sessions_admin_session",
        ),
    )

    # ------------------------------------------------------------------
    # 3. RLS — mirrors conversation_overage_ledger exactly. Postgres-only.
    # ------------------------------------------------------------------
    if _is_postgres():
        for table, policy in (
            (_COUNTER, "conversation_budget_counter_tenant_isolation"),
            (_COUNTED, "conversation_counted_sessions_tenant_isolation"),
        ):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
            op.execute(
                f"""
                CREATE POLICY {policy}
                ON {table}
                AS PERMISSIVE
                FOR ALL
                TO PUBLIC
                USING (admin_id = current_setting('app.admin_id', true))
                WITH CHECK (admin_id = current_setting('app.admin_id', true));
                """
            )


def downgrade() -> None:
    if _is_postgres():
        for table, policy in (
            (_COUNTED, "conversation_counted_sessions_tenant_isolation"),
            (_COUNTER, "conversation_budget_counter_tenant_isolation"),
        ):
            op.execute(f"DROP POLICY IF EXISTS {policy} ON {table};")
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

    op.drop_table(_COUNTED)
    op.drop_table(_COUNTER)
