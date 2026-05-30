"""Arc 13 (a) — channel_routes table (inbound addressing → Instance).

Revision ID: arc13_a_channel_routes
Revises: arc12b_custom_roles_permission_model
Create Date: 2026-05-30

First migration of the Arc 13 chain. Creates ``channel_routes``: the
map from a store-and-forward inbound address (email address or SMS
E.164 number) to the (admin_id, luciel_instance_id) that owns it. The
widget needs no route (its embed key carries the binding); email + SMS
do, because the provider only gives us the destination.

Schema
------
* ``id``               INTEGER PK autoincrement.
* ``admin_id``         FK admins.id (RESTRICT) — strict-tenant, NOT NULL.
* ``luciel_instance_id`` FK instances.id (RESTRICT) — NOT NULL.
* ``channel``          'email' | 'sms'.
* ``route_value``      fully-qualified lowercase email / E.164 number.
* ``created_at``       tz-aware, server default now().
* ``revoked_at``       NULL = live; set = released (kept for audit).

Uniqueness (one address/number → exactly one live Instance)
-----------------------------------------------------------
A PARTIAL unique index over live rows (``revoked_at IS NULL``) on
(``channel``, ``route_value``). Partial so a released number/address
can be re-provisioned to a different instance without colliding with
its historical row.

RLS posture (matches knowledge_sources, Arc 9 C11 strict-tenant)
----------------------------------------------------------------
ENABLE + FORCE RLS, RESTRICTIVE FOR ALL policy keyed on
``app.admin_id`` (fail-closed on unset GUC), plus an explicit
RESTRICTIVE FOR INSERT ``_write`` policy mirroring the
knowledge_sources install. Instance scoping stays at the service
layer (Ownership Model C). Tables created after Arc 9 C10.b inherit
the ``luciel_app`` default-privilege grant automatically — no explicit
grant here.

Rollback
--------
``downgrade()`` drops the policies, disables RLS, then drops the table.
Fail-closed: dropping the table removes routing rows; no inbound turn
can resolve afterwards, which is the correct closed state.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "arc13_a_channel_routes"
down_revision = "arc12b_custom_roles_permission_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channel_routes",
        sa.Column(
            "id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "admin_id",
            sa.String(length=100),
            sa.ForeignKey("admins.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "luciel_instance_id",
            sa.Integer(),
            sa.ForeignKey("instances.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("channel", sa.String(length=50), nullable=False),
        sa.Column("route_value", sa.String(length=320), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    op.create_index(
        "ix_channel_routes_admin_id",
        "channel_routes",
        ["admin_id"],
    )
    op.create_index(
        "ix_channel_routes_luciel_instance_id",
        "channel_routes",
        ["luciel_instance_id"],
    )
    op.create_index(
        "ix_channel_routes_admin_instance",
        "channel_routes",
        ["admin_id", "luciel_instance_id"],
    )
    op.create_index(
        "ix_channel_routes_channel_value",
        "channel_routes",
        ["channel", "route_value"],
    )
    # Partial unique index over LIVE routes only: one (channel,
    # route_value) → exactly one live Instance. Released rows
    # (revoked_at IS NOT NULL) are excluded so a number can be
    # re-provisioned later without colliding with its history.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_channel_routes_live_value
        ON channel_routes (channel, route_value)
        WHERE revoked_at IS NULL;
        """
    )

    # RLS — strict-tenant shape (Arc 9 C11), identical to
    # knowledge_sources.
    op.execute("ALTER TABLE channel_routes ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE channel_routes FORCE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY channel_routes_admin_isolation
        ON channel_routes
        AS RESTRICTIVE
        FOR ALL
        TO PUBLIC
        USING (admin_id::text = current_setting('app.admin_id', true))
        WITH CHECK (admin_id::text = current_setting('app.admin_id', true));
        """
    )
    op.execute(
        """
        CREATE POLICY channel_routes_admin_isolation_write
        ON channel_routes
        AS RESTRICTIVE
        FOR INSERT
        TO PUBLIC
        WITH CHECK (admin_id::text = current_setting('app.admin_id', true));
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS channel_routes_admin_isolation_write "
        "ON channel_routes;"
    )
    op.execute(
        "DROP POLICY IF EXISTS channel_routes_admin_isolation "
        "ON channel_routes;"
    )
    op.execute("ALTER TABLE channel_routes NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE channel_routes DISABLE ROW LEVEL SECURITY;")

    op.execute("DROP INDEX IF EXISTS uq_channel_routes_live_value;")
    op.drop_index(
        "ix_channel_routes_channel_value", table_name="channel_routes"
    )
    op.drop_index(
        "ix_channel_routes_admin_instance", table_name="channel_routes"
    )
    op.drop_index(
        "ix_channel_routes_luciel_instance_id", table_name="channel_routes"
    )
    op.drop_index(
        "ix_channel_routes_admin_id", table_name="channel_routes"
    )
    op.drop_table("channel_routes")
