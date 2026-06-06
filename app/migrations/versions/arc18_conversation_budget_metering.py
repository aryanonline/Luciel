"""Arc 18 — conversation_overage_ledger table + RLS; widen escalation CHECK.

Revision ID: arc18_conversation_budget_metering
Revises: arc17_b_secret_cleanup_outbox
Create Date: 2026-06-03

Why this migration exists
-------------------------

Arc 18 (§3.4.1b) adds per-instance conversation-budget metering with
Stripe metered overage billing at cycle close. Two schema changes:

1. NEW ``conversation_overage_ledger`` — the durable billing audit trail.
   Redis holds the live, ephemeral per-instance counter; at cycle close
   (the ``invoice.paid`` webhook) the handler snapshots each instance's
   closed period into one row here (conversations used, cap, raw overage,
   rounded units reported, tier/cadence at close, the Stripe usage-record
   id) THEN resets the counter. The unique
   ``(admin_id, instance_id, billing_period_start)`` makes a redelivered
   ``invoice.paid`` idempotent at the row level.

2. WIDEN ``ck_escalation_events_signal`` to admit ``'budget_exhausted'``.
   The budget gate fires a ``budget_exhausted`` escalation at GATE_INTAKE
   when a Free instance is at/over cap (graceful handoff, no LLM call) —
   it reuses the escalation machinery, so the CHECK must allow the new
   signal value. Postgres CHECK constraints are immutable in place, so we
   DROP + recreate. (SQLite test DBs build schema from ``Base.metadata``,
   which already carries the widened CHECK in the model — this op is the
   Postgres-side reconciliation.)

RLS posture (§3.7.5)
--------------------
``conversation_overage_ledger`` mirrors the Arc 14 U2 ``escalation_events``
policy exactly: ENABLE + FORCE RLS + a PERMISSIVE policy fencing on
``admin_id`` (USING + WITH CHECK), fail-closed when ``app.admin_id`` is
unset. The default-privileges grant installed at Arc 9 C10.b is inherited.

Rollback contract
-----------------
``alembic downgrade -1`` drops the ledger table + RLS policy and restores
the narrower escalation CHECK. Data-safe in the dropping direction;
re-narrowing the escalation CHECK would FAIL if any ``budget_exhausted``
rows already exist — that is intentional (a destructive downgrade must
not silently violate the surviving constraint).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "arc18_conversation_budget_metering"
down_revision = "arc17_b_secret_cleanup_outbox"
branch_labels = None
depends_on = None


_LEDGER = "conversation_overage_ledger"
_ESC = "escalation_events"
_ESC_CHECK = "ck_escalation_events_signal"

_SIGNALS_WITH_BUDGET = (
    "'explicit_human_request', 'strong_negative_sentiment', "
    "'cannot_confidently_answer', 'high_value_lead', 'budget_exhausted'"
)
_SIGNALS_WITHOUT_BUDGET = (
    "'explicit_human_request', 'strong_negative_sentiment', "
    "'cannot_confidently_answer', 'high_value_lead'"
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. conversation_overage_ledger.
    # ------------------------------------------------------------------
    op.create_table(
        _LEDGER,
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
            index=True,
            comment=(
                "Per-instance scope. Integer (not FK) so the billing "
                "record outlives an instance soft-delete."
            ),
        ),
        sa.Column(
            "billing_period_start",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="The period anchor reset at this close (Redis key period_start).",
        ),
        sa.Column("conversations_used", sa.Integer(), nullable=False),
        sa.Column("budget_cap", sa.Integer(), nullable=False),
        sa.Column("overage_count", sa.Integer(), nullable=False),
        sa.Column("overage_units_reported", sa.Integer(), nullable=False),
        sa.Column("tier_at_close", sa.String(16), nullable=False),
        sa.Column("cadence_at_close", sa.String(16), nullable=False),
        sa.Column(
            "stripe_usage_record_id",
            sa.String(64),
            nullable=True,
            comment="Stripe usage record id; NULL when no overage / unconfigured.",
        ),
        sa.Column(
            "reported_at",
            sa.DateTime(timezone=True),
            nullable=True,
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
            name="uq_overage_ledger_period",
        ),
    )
    op.create_index(
        "ix_overage_ledger_tenant_time",
        _LEDGER,
        ["admin_id", "billing_period_start"],
    )

    # RLS — mirrors escalation_events / sibling_call_grants. Postgres-only.
    if _is_postgres():
        op.execute(f"ALTER TABLE {_LEDGER} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {_LEDGER} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"""
            CREATE POLICY conversation_overage_ledger_tenant_isolation
            ON {_LEDGER}
            AS PERMISSIVE
            FOR ALL
            TO PUBLIC
            USING (admin_id = current_setting('app.admin_id', true))
            WITH CHECK (admin_id = current_setting('app.admin_id', true));
            """
        )

    # ------------------------------------------------------------------
    # 2. Widen the escalation signal CHECK (Postgres-only; SQLite builds
    #    the widened CHECK straight from the model metadata).
    # ------------------------------------------------------------------
    if _is_postgres():
        op.execute(f"ALTER TABLE {_ESC} DROP CONSTRAINT IF EXISTS {_ESC_CHECK};")
        op.execute(
            f"ALTER TABLE {_ESC} ADD CONSTRAINT {_ESC_CHECK} "
            f"CHECK (signal IN ({_SIGNALS_WITH_BUDGET}));"
        )


def downgrade() -> None:
    # 2. Restore the narrower escalation CHECK. Will FAIL if any
    #    budget_exhausted rows exist — intentional (no silent violation).
    if _is_postgres():
        op.execute(f"ALTER TABLE {_ESC} DROP CONSTRAINT IF EXISTS {_ESC_CHECK};")
        op.execute(
            f"ALTER TABLE {_ESC} ADD CONSTRAINT {_ESC_CHECK} "
            f"CHECK (signal IN ({_SIGNALS_WITHOUT_BUDGET}));"
        )

    # 1. Drop the ledger (RLS policy + index + table).
    if _is_postgres():
        op.execute(
            f"DROP POLICY IF EXISTS "
            f"conversation_overage_ledger_tenant_isolation ON {_LEDGER};"
        )
        op.execute(f"ALTER TABLE {_LEDGER} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {_LEDGER} DISABLE ROW LEVEL SECURITY;")

    op.drop_index("ix_overage_ledger_tenant_time", table_name=_LEDGER)
    op.drop_table(_LEDGER)
