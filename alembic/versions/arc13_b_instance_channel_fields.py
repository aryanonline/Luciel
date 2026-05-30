"""Arc 13 (b) — per-instance channel-enablement fields on instances.

Revision ID: arc13_b_instance_channel_fields
Revises: arc13_a_channel_routes
Create Date: 2026-05-30

Second migration of the Arc 13 chain. Adds the per-instance channel
state the runtime reads to answer "which channels are structurally
enabled on this Instance?" — the function ``_instance_channels_enabled``
in app/api/v1/admin_tools.py (an Arc 13 stub) now reads these columns.

Columns added to ``instances``
------------------------------
* ``enabled_channels``       TEXT[] — the set of channel ids enabled on
                             this Instance. Server default ``{widget}``
                             so every existing + new Instance has the
                             widget on by default (widget is always
                             available per the entitlement matrix; email
                             / sms are added here when provisioned).
* ``sms_provisioned_number`` VARCHAR(32) NULL — the E.164 number
                             provisioned to this Instance for SMS. NULL
                             until SMS is enabled + a number is bound.
                             The corresponding channel_routes row
                             (channel='sms') is the routing record; this
                             column is the instance-side reference.
* ``sms_number_mode``        VARCHAR(16) NULL — 'dedicated' | 'shared'.
                             NULL until SMS is enabled. Pro gets a
                             dedicated number per Instance; Enterprise
                             gets dedicated + (deferred) brokerage
                             routing. See app/policy/entitlements.py
                             dedicated-number helper.

RLS
---
``instances`` already has RLS ENABLED + FORCED with both a RESTRICTIVE
and a PERMISSIVE tenant policy (Arc 9 C3.5d / C10.a). Column additions
inherit the table policy automatically — no new policy needed, and we
deliberately do NOT touch RLS here so the existing fences stay intact.

Backfill
--------
``enabled_channels`` server default ``{widget}`` backfills every
existing row to the widget-only set on the ADD COLUMN, matching the
entitlement floor (every tier has the widget). No data migration step
needed; NULL is impossible because the column is NOT NULL with a
server default.

Rollback
--------
``downgrade()`` drops the three columns. Reversible and lossless for
the channel subsystem (routing rows live in channel_routes, dropped by
arc13_a's downgrade if the whole chain is rolled back).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "arc13_b_instance_channel_fields"
down_revision = "arc13_a_channel_routes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "instances",
        sa.Column(
            "enabled_channels",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("ARRAY['widget']::text[]"),
        ),
    )
    op.add_column(
        "instances",
        sa.Column(
            "sms_provisioned_number",
            sa.String(length=32),
            nullable=True,
        ),
    )
    op.add_column(
        "instances",
        sa.Column(
            "sms_number_mode",
            sa.String(length=16),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("instances", "sms_number_mode")
    op.drop_column("instances", "sms_provisioned_number")
    op.drop_column("instances", "enabled_channels")
