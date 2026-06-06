"""Arc 14 U4 — leads table + RLS (§3.4.4 lead capture, §3.4.7 summary).

Revision ID: arc14_u4_leads
Revises: arc14_u2_escalation_events
Create Date: 2026-06-01

Why this migration exists
-------------------------

Arc 14 U4 builds the §3.4.4 Lead Capture + §3.4.7 Summarization
cognition. When a conversation crosses the lead threshold (contact info
given, specific-listing intent, budget mentioned, or otherwise
sales-qualified) the orchestrator's COGNITION FINALIZATION step writes
one structured lead row to the dashboard lead view. Lead capture is
always-on cognition (§3.4) — NOT a tool, NOT admin-configurable, NOT
tier-gated — so the table it writes to is part of the runtime schema,
not the configurable tool surface.

This is VantageMind's OWN lead record. It is deliberately decoupled from
the ``push_to_crm`` tool: push_to_crm extends a captured lead OUTWARD to
an external CRM; this table is the internal row push_to_crm would read.

Schema decisions (mirror escalation_events / traces conventions)
----------------------------------------------------------------
* ``admin_id`` ``String(100)`` FK ``admins.id`` ON DELETE RESTRICT — the
  Wall-1 column convention; RLS fences on it.
* ``luciel_instance_id`` ``Integer`` FK ``instances.id`` ON DELETE SET
  NULL — the lead survives instance deletion (matches escalation_events).
* ``session_id`` ``String(100)`` non-null — every lead belongs to a
  conversation.
* §3.4.4 structured fields: ``name``, ``contact_channel``,
  ``contact_identifier``, ``intent`` (Text), ``key_facts`` (JSONB list),
  ``next_step`` (Text). All nullable — a lead crosses the threshold on
  ANY one qualifying signal, so not every field is always present.
* §3.4.7 ``summary`` (Text) persisted alongside the lead row.

Indexes
-------
* ``ix_leads_admin_id`` — implicit from index=True on admin_id.
* ``ix_leads_tenant_time`` on ``(admin_id, created_at)`` — the dashboard
  "leads for tenant X in time range Y" query.
* ``ix_leads_session`` on ``session_id`` — "every lead for this
  conversation".

RLS posture (§3.7.5)
--------------------
Mirrors the Arc 14 U2 ``escalation_events`` policy exactly:

1. ``ENABLE ROW LEVEL SECURITY``.
2. ``FORCE ROW LEVEL SECURITY`` — apply RLS to the table owner too.
3. PERMISSIVE policy ``leads_tenant_isolation`` on ``admin_id``. USING +
   WITH CHECK both strict so reads and writes are equally fenced. When
   ``app.admin_id`` is unset, ``current_setting(..., true)`` returns
   NULL; ``admin_id = NULL`` is NULL in three-valued logic; RLS treats
   NULL as "deny." Fail-closed by construction.

Grants
------
``ALTER DEFAULT PRIVILEGES ... GRANT SELECT, INSERT, UPDATE, DELETE ON
TABLES TO luciel_app`` was installed at Arc 9 C10.b. Tables created after
that migration inherit the grant; no explicit grant is issued here.

Rollback contract
-----------------
``alembic downgrade -1`` drops the table, indexes, and RLS policy.
Data-safe: dropping a tenant-scoped table widens nothing.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision = "arc14_u4_leads"
down_revision = "arc14_u2_escalation_events"
branch_labels = None
depends_on = None


_TABLE = "leads"


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
            comment="Wall-1 tenant boundary. RLS fences on this column.",
        ),
        sa.Column(
            "luciel_instance_id",
            sa.Integer(),
            sa.ForeignKey("instances.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
            comment=(
                "Instance the lead was captured under. SET NULL on "
                "delete — the lead survives instance removal."
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
        # --- §3.4.4 structured lead fields ---
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column(
            "contact_channel",
            sa.String(50),
            nullable=True,
            comment="Channel the customer is reachable on.",
        ),
        sa.Column(
            "contact_identifier",
            sa.String(320),
            nullable=True,
            comment="Address on the contact channel (email/phone/visitor id).",
        ),
        sa.Column(
            "intent",
            sa.Text(),
            nullable=True,
            comment="The sales intent in plain language.",
        ),
        sa.Column(
            "key_facts",
            JSONB(),
            nullable=True,
            comment="Salient facts mentioned (listing id, budget, timeline).",
        ),
        sa.Column(
            "next_step",
            sa.Text(),
            nullable=True,
            comment="The recommended next action.",
        ),
        # --- §3.4.7 structured summary persisted alongside the lead ---
        sa.Column(
            "summary",
            sa.Text(),
            nullable=True,
            comment="Structured conversation summary persisted with the lead.",
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

    # "Leads for tenant X in time range Y."
    op.create_index(
        "ix_leads_tenant_time",
        _TABLE,
        ["admin_id", "created_at"],
    )
    # "Every lead for this conversation."
    op.create_index(
        "ix_leads_session",
        _TABLE,
        ["session_id"],
    )

    # ------------------------------------------------------------------
    # 2. RLS posture — mirrors arc14_u2_escalation_events exactly.
    # ------------------------------------------------------------------
    op.execute(
        f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY;"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        f"""
        CREATE POLICY leads_tenant_isolation
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
        f"DROP POLICY IF EXISTS leads_tenant_isolation ON {_TABLE};"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY;"
    )

    # 1. Drop indexes + table.
    op.drop_index("ix_leads_session", table_name=_TABLE)
    op.drop_index("ix_leads_tenant_time", table_name=_TABLE)
    op.drop_table(_TABLE)
