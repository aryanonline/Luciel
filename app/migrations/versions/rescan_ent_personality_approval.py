"""Rescan ENT — Enterprise personality second-admin approval (Vision §7).

Revision ID: rescan_ent_personality_approval
Revises: rescand_connections_schema
Create Date: 2026-06-04

What this migration adds
------------------------

Seven new columns on ``instances`` that stage a proposed personality
change until a SECOND admin approves it. On Enterprise the PUT no longer
mutates the live ``personality_*`` columns directly; it stages the
proposal here and flips ``personality_approval_state`` to
``pending_approval``. A different admin then approves (copy staged →
live, state back to ``live``) or rejects (discard staged, live
untouched). Free/Pro apply immediately and keep state ``live``.

Mirrors the custom-role approval columns
(``rescanb_custom_role_approval``) and the sibling-grant approval shape
(Architecture §3.3.4 / §3.7.3).

* ``personality_approval_state``        varchar(20) NOT NULL DEFAULT 'live'
                                        CHECK IN ('live','pending_approval').
                                        Existing rows land 'live' — fully
                                        backward-compatible.
* ``pending_personality_preset``        varchar(64) NULL — proposed preset.
* ``pending_personality_axes``          jsonb NULL — proposed custom axes.
* ``pending_business_context``          text NULL — proposed background.
* ``personality_submitted_by_user_id``  uuid NULL FK users.id — proposer.
* ``personality_submitted_at``          timestamptz NULL.
* ``personality_approved_by_user_id``   uuid NULL FK users.id — approver.
* ``personality_approved_at``           timestamptz NULL.

Down-revision
-------------
Chains from the current head ``rescand_connections_schema`` (the only
migration-bearing unit running now). Downgrade drops all eight columns;
the ``personality_approval_state`` DEFAULT was 'live' so untouched rows
survive a round-trip with their live config intact.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# -------------------------------------------------------------------
# Revision identifiers
# -------------------------------------------------------------------
revision = "rescan_ent_personality_approval"
down_revision = "rescand_connections_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. personality_approval_state — varchar NOT NULL DEFAULT 'live'.
    # ------------------------------------------------------------------
    op.add_column(
        "instances",
        sa.Column(
            "personality_approval_state",
            sa.String(20),
            nullable=False,
            server_default="live",
        ),
    )
    op.create_check_constraint(
        "ck_instances_personality_approval_state",
        "instances",
        "personality_approval_state IN ('live', 'pending_approval')",
    )

    # ------------------------------------------------------------------
    # 2. Proposed pillars (staged until approval). All NULL = no pending.
    # ------------------------------------------------------------------
    op.add_column(
        "instances",
        sa.Column("pending_personality_preset", sa.String(64), nullable=True),
    )
    op.add_column(
        "instances",
        sa.Column(
            "pending_personality_axes",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
        ),
    )
    op.add_column(
        "instances",
        sa.Column("pending_business_context", sa.Text(), nullable=True),
    )

    # ------------------------------------------------------------------
    # 3. Submitter (proposer) — uuid NULL FK users.id + timestamp.
    # ------------------------------------------------------------------
    op.add_column(
        "instances",
        sa.Column(
            "personality_submitted_by_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_instances_personality_submitted_by_user_id",
        "instances",
        "users",
        ["personality_submitted_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column(
        "instances",
        sa.Column(
            "personality_submitted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    # ------------------------------------------------------------------
    # 4. Approver — uuid NULL FK users.id + timestamp.
    # ------------------------------------------------------------------
    op.add_column(
        "instances",
        sa.Column(
            "personality_approved_by_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_instances_personality_approved_by_user_id",
        "instances",
        "users",
        ["personality_approved_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column(
        "instances",
        sa.Column(
            "personality_approved_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("instances", "personality_approved_at")
    op.drop_constraint(
        "fk_instances_personality_approved_by_user_id",
        "instances",
        type_="foreignkey",
    )
    op.drop_column("instances", "personality_approved_by_user_id")
    op.drop_column("instances", "personality_submitted_at")
    op.drop_constraint(
        "fk_instances_personality_submitted_by_user_id",
        "instances",
        type_="foreignkey",
    )
    op.drop_column("instances", "personality_submitted_by_user_id")
    op.drop_column("instances", "pending_business_context")
    op.drop_column("instances", "pending_personality_axes")
    op.drop_column("instances", "pending_personality_preset")
    op.drop_constraint(
        "ck_instances_personality_approval_state",
        "instances",
        type_="check",
    )
    op.drop_column("instances", "personality_approval_state")
