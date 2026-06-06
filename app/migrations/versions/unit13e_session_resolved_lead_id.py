"""Unit 13e — add resolved_lead_id to sessions (§3.4.8 session key).

Revision ID: unit13e_session_resolved_lead_id
Revises: unit13d_lead_outcome
Create Date: 2026-06-06

§3.4.8 defines the session key as (instance_id, participant_id, channel),
where participant_id = the resolved lead identity for lead-facing channels
(set when the identity resolver binds a session to a lead/User) or the
internal Slack workspace user id for the internal channel (§3.4.9
exception). The sessions table had admin_id, luciel_instance_id, user_id,
and channel but NO participant column.

What this migration does
------------------------
1. Add ``resolved_lead_id`` (varchar(100), NULLABLE). ADDITIVE — user_id
   is kept verbatim for back-compat and the anonymous-widget path. NULL is
   the anonymous default; per the §3.4.9 HARD RULE a NULL never matches
   another NULL as "same participant" (SQL = NULL is never TRUE, and the
   lookup helper refuses NULL keys). No backfill — existing sessions were
   created before the column existed and are correctly NULL.
2. Add the §3.4.8 session-key index ``ix_sessions_key`` on
   ``(luciel_instance_id, resolved_lead_id, channel)`` so the session-key
   lookup is index-backed.

No RLS change — ``sessions`` already carries its tenant-isolation policy
(Wall-1 admin_id + Wall-3 luciel_instance_id); the new column is fenced by
the existing policy. The budget meter is NOT touched (it keys on
session_id + (admin_id, instance_id, period_start), §3.4.1b) — this column
is additive to budget counting.

Downgrade
---------
Drops the index and column. Data-safe: dropping a participant column
widens no tenant boundary and the value is reconstructable from the
identity resolver.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "unit13e_session_resolved_lead_id"
down_revision = "unit13d_lead_outcome"
branch_labels = None
depends_on = None

_TABLE = "sessions"
_COLUMN = "resolved_lead_id"
_INDEX = "ix_sessions_key"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.String(length=100),
            nullable=True,
            comment=(
                "§3.4.8 session-key participant id: resolved lead identity "
                "(str of resolved User.id) for lead-facing channels, or the "
                "internal workspace user id (§3.4.9). NULL = anonymous "
                "(never matches another NULL as same participant)."
            ),
        ),
    )
    op.create_index(
        _INDEX,
        _TABLE,
        ["luciel_instance_id", _COLUMN, "channel"],
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, _COLUMN)
