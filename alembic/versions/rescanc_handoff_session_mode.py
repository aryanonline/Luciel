"""Rescan Tier-C — human-controlled session mode (Architecture §3.4.12).

Revision ID: rescanc_handoff_session_mode
Revises: rescanc_escalation_delivery
Create Date: 2026-06-08

What this migration adds
------------------------

Four new columns on ``sessions``:

* ``control_mode``           varchar(20) NOT NULL DEFAULT 'luciel'
                             CHECK IN ('luciel','human_controlled').
                             Determines whether the orchestrator runs
                             the agentic loop ('luciel') or gates it
                             entirely ('human_controlled') for inbound
                             messages on this session.

* ``taken_over_by_user_id``  uuid NULL
                             The admin User who initiated an
                             admin-initiated takeover. NULL for
                             Luciel-initiated takeovers (trigger=
                             'luciel_escalated').

* ``taken_over_at``          timestamptz NULL
                             UTC timestamp when control_mode was set
                             to 'human_controlled'. NULL for sessions
                             that have never been taken over.

* ``handed_back_at``         timestamptz NULL
                             UTC timestamp when the admin called
                             /handback and control_mode reverted to
                             'luciel'. NULL while still
                             human_controlled or if the session ended
                             via inactivity timeout before handback.

Expand-contract design
----------------------
All four columns are additive (existing rows keep the NOT NULL default
'luciel' / three NULL timestamps). No data migration needed. RLS is
unaffected — the sessions table already scopes reads to the
authenticated admin_id (established by arc9_c3_rls_sessions).

Down-revision
-------------
Drops the four columns and the check constraint. Existing rows that
had control_mode='luciel' lose only the metadata columns (no data loss
for the normal path).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "rescanc_handoff_session_mode"
down_revision = "rescanc_escalation_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add control_mode column (NOT NULL, default 'luciel').
    op.add_column(
        "sessions",
        sa.Column(
            "control_mode",
            sa.String(20),
            nullable=False,
            server_default="luciel",
            comment=(
                "Rescan Tier-C §3.4.12 — session control mode: "
                "'luciel' (agentic loop runs) or "
                "'human_controlled' (orchestrator gated, zero LLM calls)."
            ),
        ),
    )
    op.create_check_constraint(
        "ck_sessions_control_mode",
        "sessions",
        "control_mode IN ('luciel', 'human_controlled')",
    )

    # 2. Add taken_over_by_user_id column (UUID, nullable).
    op.add_column(
        "sessions",
        sa.Column(
            "taken_over_by_user_id",
            PG_UUID(as_uuid=True),
            nullable=True,
            comment=(
                "Rescan Tier-C §3.4.12 — admin User who initiated the "
                "takeover (NULL for Luciel-initiated trigger='luciel_escalated')."
            ),
        ),
    )

    # 3. Add taken_over_at column (timestamptz, nullable).
    op.add_column(
        "sessions",
        sa.Column(
            "taken_over_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Rescan Tier-C §3.4.12 — UTC timestamp when session "
                "became human_controlled."
            ),
        ),
    )

    # 4. Add handed_back_at column (timestamptz, nullable).
    op.add_column(
        "sessions",
        sa.Column(
            "handed_back_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Rescan Tier-C §3.4.12 — UTC timestamp when admin called "
                "/handback and control reverted to 'luciel'."
            ),
        ),
    )


def downgrade() -> None:
    # Drop in reverse order.
    op.drop_column("sessions", "handed_back_at")
    op.drop_column("sessions", "taken_over_at")
    op.drop_column("sessions", "taken_over_by_user_id")
    op.drop_constraint(
        "ck_sessions_control_mode",
        "sessions",
        type_="check",
    )
    op.drop_column("sessions", "control_mode")
