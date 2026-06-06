"""Unit 13d — add outcome to leads (§3.9 conversion metric prerequisite).

Revision ID: unit13d_lead_outcome
Revises: unit13c_connection_auth_class
Create Date: 2026-06-06

The §3.9 Analytics conversion-rate metric needs to know each lead's sales
outcome. Lead capture (§3.4.4) writes the lead row but never knows the
outcome — that is downstream business data the admin records once they
work the lead. This migration adds the column the admin's
``PATCH /api/v1/admin/leads/{id}/outcome`` endpoint writes and the
AnalyticsService reads.

What this migration does
------------------------
1. Add ``outcome`` (varchar(20), NULLABLE). NULL is the capture default
   (not yet worked); no backfill — every existing lead is correctly NULL.
2. Add a CHECK constraint pinning ``outcome IS NULL OR outcome IN
   ('converted','lost','in_progress')`` — the honesty backstop, the live
   DB rejects an out-of-vocabulary outcome (mirrors the
   ck_escalation_events_signal / ck_admins_tier posture).

No new table, no RLS change — ``leads`` already carries the
``leads_tenant_isolation`` PERMISSIVE policy on ``admin_id`` (arc14_u4),
which fences this new column too.

Downgrade
---------
Drops the CHECK constraint and the column. Data-safe: ``outcome`` is
admin-recorded business data, but dropping it widens no tenant boundary
and the column is reconstructable by re-working the leads.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "unit13d_lead_outcome"
down_revision = "unit13c_connection_auth_class"
branch_labels = None
depends_on = None

_TABLE = "leads"
_COLUMN = "outcome"
_CHECK_NAME = "ck_leads_outcome_valid"

_ALLOWED = ("converted", "lost", "in_progress")


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.String(length=20),
            nullable=True,
            comment=(
                "§3.9 sales outcome recorded by the admin (NULL = not yet "
                "worked): converted / lost / in_progress. Feeds the "
                "conversion-rate metric; written only by PATCH .../outcome."
            ),
        ),
    )

    allowed = ", ".join(f"'{v}'" for v in _ALLOWED)
    op.create_check_constraint(
        _CHECK_NAME,
        _TABLE,
        f"{_COLUMN} IS NULL OR {_COLUMN} IN ({allowed})",
    )


def downgrade() -> None:
    op.drop_constraint(_CHECK_NAME, _TABLE, type_="check")
    op.drop_column(_TABLE, _COLUMN)
