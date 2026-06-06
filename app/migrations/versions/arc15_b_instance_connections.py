"""Arc 15 B — instance_connections table + RLS (Arc 17 connection-contract slice).

Revision ID: arc15_b_instance_connections
Revises: arc15_a_instance_config_pillars
Create Date: 2026-06-02

Why this migration exists
-------------------------
Arc 15 WU4 lands the real ``instance_connections`` table per
Architecture §3.8.2 — the durable record of WHICH external systems an
instance is wired to (calendar, email sender, sms sender, crm, property
source, outbound webhook) and the live status of each wiring.

This is the storage half of the Arc 17 connection contract. The
dispatch gate that consults it (WU5 — a tool with ``requires_connection``
may only fire when a ``connected`` row exists) reads this table.

Schema (§3.8.2 exact shape)
---------------------------
* ``id``               — Integer PK (Arc 5 PK doctrine).
* ``admin_id``         — String(100) FK→admins, non-null, indexed.
                         Wall-1 tenant boundary; RLS fences on it.
* ``instance_id``      — Integer FK→instances, non-null, indexed.
                         Wall-3 instance boundary.
* ``connection_type``  — PG enum ``connection_type`` (6 values).
* ``provider``         — String(64) non-null. The concrete provider
                         (e.g. 'google_calendar', 'csv', 'twilio').
* ``non_secret_config``      — JSONB nullable. NON-SECRET config ONLY (e.g.
                         a CSV column map, a webhook URL). NEVER secrets:
                         API keys / OAuth tokens live behind
                         ``secret_ref`` in the secret store.
* ``secret_ref``   — String(255) nullable. Opaque pointer into the
                         secret store. NULL in this slice (no live
                         credential-bearing connectors land here).
* ``status``           — PG enum ``connection_status`` (4 values):
                         unconfigured | connected | error | expired.
* ``last_verified_at`` — timestamptz nullable. Last successful health
                         check / verification.
* ``created_at``       — timestamptz non-null.
* ``updated_at``       — timestamptz non-null.
* ``revoked_at``       — timestamptz nullable. Soft-delete (§5.5
                         Pattern E).

Partial unique constraint
-------------------------
``uq_instance_connections_active`` enforces at-most-one non-revoked row
per ``(admin_id, instance_id, connection_type, provider)``. Revoked
rows are excluded so disconnect + reconnect can coexist as separate
rows. Same shape as ``uq_instance_tool_authorizations_active``.

RLS posture (§3.7.5)
--------------------
Mirrors ``arc12_wu2_instance_tool_authorizations.py`` exactly:
1. ENABLE ROW LEVEL SECURITY.
2. FORCE ROW LEVEL SECURITY (seals the ownership escape).
3. PERMISSIVE policy on ``admin_id`` with strict USING + WITH CHECK.

When ``app.admin_id`` is unset, ``current_setting(..., true)`` returns
NULL; ``admin_id = NULL`` is NULL in three-valued logic; RLS treats
NULL as deny. Fail-closed by construction.

Grants
------
``ALTER DEFAULT PRIVILEGES`` from Arc 9 C10.b applies to tables created
after it; no explicit grant is issued here.

Rollback contract
-----------------
``downgrade`` drops the table (+ its two enum types). Data-safe: a
connection table only widens dispatch surface — dropping it narrows it.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM, JSONB


revision = "arc15_b_instance_connections"
down_revision = "arc15_a_instance_config_pillars"
branch_labels = None
depends_on = None


_TABLE = "instance_connections"

_CONN_TYPE_ENUM = "connection_type"
_CONN_TYPE_VALUES = (
    "calendar",
    "email_sender",
    "sms_sender",
    "crm",
    "property_source",
    "outbound_webhook",
)

_CONN_STATUS_ENUM = "connection_status"
_CONN_STATUS_VALUES = (
    "unconfigured",
    "connected",
    "error",
    "expired",
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Create the two PG enum types.
    # ------------------------------------------------------------------
    conn_type_enum = ENUM(
        *_CONN_TYPE_VALUES, name=_CONN_TYPE_ENUM, create_type=False
    )
    conn_status_enum = ENUM(
        *_CONN_STATUS_VALUES, name=_CONN_STATUS_ENUM, create_type=False
    )
    conn_type_enum.create(op.get_bind(), checkfirst=True)
    conn_status_enum.create(op.get_bind(), checkfirst=True)

    # ------------------------------------------------------------------
    # 2. Create the table.
    # ------------------------------------------------------------------
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "admin_id",
            sa.String(100),
            sa.ForeignKey("admins.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
            comment=(
                "Wall-1 tenant boundary. The owning Admin; RLS fences "
                "on this column."
            ),
        ),
        sa.Column(
            "instance_id",
            sa.Integer(),
            sa.ForeignKey("instances.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
            comment="Wall-3 instance boundary. The Instance wired to the system.",
        ),
        sa.Column(
            "connection_type",
            conn_type_enum,
            nullable=False,
            comment=(
                "§3.8.2 connector category: calendar | email_sender | "
                "sms_sender | crm | property_source | outbound_webhook."
            ),
        ),
        sa.Column(
            "provider",
            sa.String(64),
            nullable=False,
            comment="Concrete provider, e.g. 'csv', 'google_calendar', 'twilio'.",
        ),
        sa.Column(
            "non_secret_config",
            JSONB(),
            nullable=True,
            comment=(
                "NON-SECRET config ONLY (CSV column map, webhook URL). "
                "NEVER secrets — credentials live behind secret_ref."
            ),
        ),
        sa.Column(
            "secret_ref",
            sa.String(255),
            nullable=True,
            comment=(
                "Opaque pointer into the secret store. NULL in this "
                "slice (no live credential-bearing connectors land here)."
            ),
        ),
        sa.Column(
            "status",
            conn_status_enum,
            nullable=False,
            server_default="unconfigured",
            comment=(
                "unconfigured | connected | error | expired. A tool with "
                "requires_connection may only fire when status='connected'."
            ),
        ),
        sa.Column(
            "last_verified_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Last successful health-check / verification timestamp.",
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
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Soft-revoke timestamp (§5.5 Pattern E). NULL = live; "
                "non-NULL = disconnected."
            ),
        ),
    )

    # Partial unique index — at-most-one live row per
    # (admin_id, instance_id, connection_type, provider).
    op.create_index(
        "uq_instance_connections_active",
        _TABLE,
        ["admin_id", "instance_id", "connection_type", "provider"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    # Composite covering index for the gate's hot-path lookup.
    op.create_index(
        "ix_instance_connections_lookup",
        _TABLE,
        ["admin_id", "instance_id", "connection_type"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    # ------------------------------------------------------------------
    # 3. RLS posture — mirrors arc12_wu2_instance_tool_authorizations.py.
    # ------------------------------------------------------------------
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY instance_connections_tenant_isolation
        ON {_TABLE}
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (admin_id = current_setting('app.admin_id', true))
        WITH CHECK (admin_id = current_setting('app.admin_id', true));
        """
    )


def downgrade() -> None:
    op.execute(
        f"DROP POLICY IF EXISTS instance_connections_tenant_isolation "
        f"ON {_TABLE};"
    )
    op.execute(f"ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY;")

    op.drop_index("ix_instance_connections_lookup", table_name=_TABLE)
    op.drop_index("uq_instance_connections_active", table_name=_TABLE)
    op.drop_table(_TABLE)

    ENUM(name=_CONN_STATUS_ENUM).drop(op.get_bind(), checkfirst=True)
    ENUM(name=_CONN_TYPE_ENUM).drop(op.get_bind(), checkfirst=True)
