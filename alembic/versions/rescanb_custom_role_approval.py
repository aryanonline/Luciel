"""Rescan Tier-B — custom-role second-admin approval workflow (Architecture §3.7.3).

Revision ID: rescanb_custom_role_approval
Revises: arc18_conversation_budget_metering
Create Date: 2026-06-06

What this migration adds
------------------------

Four new columns on ``custom_roles``:

* ``approval_state``        varchar(20) NOT NULL DEFAULT 'live'
                            CHECK IN ('live','pending_approval','revoked').
                            Existing rows land 'live' — fully backward-
                            compatible.

* ``approved_by_user_id``   uuid NULL FK users.id — who approved the role.

* ``approved_at``           timestamptz NULL — when it was approved.

* ``pending_change_json``   jsonb NULL — staged (but not yet applied)
                            permission/scope change waiting for a second
                            admin_owner to approve.

Architecture §3.7.3 rule
-------------------------
Any custom role that includes ``can_configure_connections`` OR
``can_view_billing`` must be approved by a SECOND admin_owner before
granting any permissions at runtime. A role in ``pending_approval`` state
is excluded from effective-permission resolution (grants ZERO permissions).

Down-revision
-------------
Drops the four columns. The ``approval_state`` DEFAULT was 'live', so
existing rows that were never touched by the approval workflow survive
a round-trip with their state intact.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# -------------------------------------------------------------------
# Revision identifiers
# -------------------------------------------------------------------
revision = "rescanb_custom_role_approval"
down_revision = "arc18_conversation_budget_metering"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. approval_state — varchar NOT NULL DEFAULT 'live', CHECK constraint.
    # ------------------------------------------------------------------
    op.add_column(
        "custom_roles",
        sa.Column(
            "approval_state",
            sa.String(20),
            nullable=False,
            server_default="live",
        ),
    )
    op.create_check_constraint(
        "ck_custom_roles_approval_state",
        "custom_roles",
        "approval_state IN ('live', 'pending_approval', 'revoked')",
    )

    # ------------------------------------------------------------------
    # 2. approved_by_user_id — uuid NULL FK users.id
    # ------------------------------------------------------------------
    op.add_column(
        "custom_roles",
        sa.Column(
            "approved_by_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_custom_roles_approved_by_user_id",
        "custom_roles",
        "users",
        ["approved_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # ------------------------------------------------------------------
    # 3. approved_at — timestamptz NULL
    # ------------------------------------------------------------------
    op.add_column(
        "custom_roles",
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    # ------------------------------------------------------------------
    # 4. pending_change_json — jsonb NULL
    # ------------------------------------------------------------------
    op.add_column(
        "custom_roles",
        sa.Column(
            "pending_change_json",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("custom_roles", "pending_change_json")
    op.drop_column("custom_roles", "approved_at")
    op.drop_constraint(
        "fk_custom_roles_approved_by_user_id", "custom_roles", type_="foreignkey"
    )
    op.drop_column("custom_roles", "approved_by_user_id")
    op.drop_constraint(
        "ck_custom_roles_approval_state", "custom_roles", type_="check"
    )
    op.drop_column("custom_roles", "approval_state")
