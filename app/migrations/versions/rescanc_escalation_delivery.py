"""Rescan Tier-C — escalation delivery layer (Architecture §3.5).

Revision ID: rescanc_escalation_delivery
Revises: rescanb_custom_role_approval
Create Date: 2026-06-07

What this migration adds
------------------------

Three new columns on ``escalation_events``:

* ``delivery_status``   varchar(16) NOT NULL DEFAULT 'pending'
                        CHECK IN ('pending','delivered','acked','failed').
                        Tracks the lifecycle of the notification delivery
                        for exactly-once idempotency. Default 'pending' so
                        existing rows are backward-compatible.

* ``attempts``          integer NOT NULL DEFAULT 0
                        Number of send attempts made across all channels.
                        Incremented by the delivery service on each retry.

* ``last_attempt_at``   timestamptz NULL
                        Timestamp of the most recent send attempt.
                        NULL for rows written before this migration.

One new unique index on ``escalation_events``:

* ``uq_escalation_events_idempotency``
  UNIQUE over (session_id, signal, gate) WHERE delivery_status != 'pending'.
  This is a PARTIAL unique index: allows multiple 'pending' rows for
  the same (session, signal, gate) — e.g. replay during a crash window —
  but prevents two 'delivered' rows for the same logical event (exactly-once
  delivery guarantee).

  NOTE: The spec asks for UNIQUE over (session_id, signal, gate) over
  non-revoked rows. Because the model has no revoked state on the event
  itself (only on delivery_status), we implement this as a partial unique
  index over rows where delivery_status IN ('delivered', 'acked').

Down-revision
-------------
Drops the three columns and the partial unique index.
Existing rows that had non-'pending' delivery_status lose that metadata
but otherwise survive the round-trip.

RLS
---
No new RLS policy needed — the escalation_events table already has
PERMISSIVE RLS fenced on admin_id (established by the arc14_u2 migration).
The new columns are scoped under the same row, no cross-tenant access
is introduced.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "rescanc_escalation_delivery"
down_revision = "rescanb_custom_role_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add delivery_status column.
    op.add_column(
        "escalation_events",
        sa.Column(
            "delivery_status",
            sa.String(16),
            nullable=False,
            server_default="pending",
            comment=(
                "Rescan Tier-C §3.5 — delivery lifecycle: "
                "pending / delivered / acked / failed."
            ),
        ),
    )
    op.create_check_constraint(
        "ck_escalation_events_delivery_status",
        "escalation_events",
        "delivery_status IN ('pending', 'delivered', 'acked', 'failed')",
    )

    # 2. Add attempts column.
    op.add_column(
        "escalation_events",
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Rescan Tier-C §3.5 — cumulative send attempts across channels.",
        ),
    )

    # 3. Add last_attempt_at column.
    op.add_column(
        "escalation_events",
        sa.Column(
            "last_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Rescan Tier-C §3.5 — timestamp of the last send attempt.",
        ),
    )

    # 4. Partial unique index for idempotency: at most one
    #    delivered/acked row per (session_id, signal, gate).
    op.create_index(
        "uq_escalation_events_idempotency",
        "escalation_events",
        ["session_id", "signal", "gate"],
        unique=True,
        postgresql_where=sa.text(
            "delivery_status IN ('delivered', 'acked')"
        ),
    )


def downgrade() -> None:
    # Drop in reverse order.
    op.drop_index(
        "uq_escalation_events_idempotency",
        table_name="escalation_events",
    )
    op.drop_constraint(
        "ck_escalation_events_delivery_status",
        "escalation_events",
        type_="check",
    )
    op.drop_column("escalation_events", "last_attempt_at")
    op.drop_column("escalation_events", "attempts")
    op.drop_column("escalation_events", "delivery_status")
