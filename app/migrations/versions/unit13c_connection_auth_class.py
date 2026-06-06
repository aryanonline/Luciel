"""Unit 13c — add auth_class to instance_connections (§3.8.5).

Revision ID: unit13c_connection_auth_class
Revises: unit9_escalation_signal_llm_unavailable
Create Date: 2026-06-06

§3.8.5's core abstraction is the connection's ``auth_class`` — the
credential SHAPE that drives the health/refresh worker's cadence +
action, decoupled from the (vertical-leaning) ``connection_type``. The
four classes are:

  * ``oauth_token``          — refreshable OAuth (calendar / crm).
  * ``long_lived_token``     — long-lived bearer.
  * ``api_key``              — static key (record_source / outbound_webhook).
  * ``provisioned_resource`` — platform-owned transport (email_sender / sms_sender).

What this migration does
------------------------
1. Add ``auth_class`` (varchar(32), NOT NULL). A temporary server_default
   (``'api_key'``) lets the NOT NULL add succeed against existing rows;
   the backfill below corrects each row by connection_type, then the
   default is dropped so a new INSERT MUST supply the value (the ORM
   model derives it from ``auth_class_for`` at create time).
2. Backfill existing rows by ``connection_type`` (the same partition as
   ``app.connections.instance_connection.AUTH_CLASS_BY_TYPE``):
     calendar, crm                 → oauth_token
     email_sender, sms_sender      → provisioned_resource
     record_source, outbound_webhook → api_key
3. Add a CHECK constraint pinning the four-value vocabulary at the DB
   layer (the honesty backstop — the live DB rejects an out-of-vocabulary
   class).

Downgrade
---------
Drops the CHECK constraint and the column (a NOT NULL add with a backfill
is safe to reverse — no data is lost that wasn't derivable from
connection_type).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "unit13c_connection_auth_class"
down_revision = "unit9_escalation_signal_llm_unavailable"
branch_labels = None
depends_on = None

_TABLE = "instance_connections"
_COLUMN = "auth_class"
_CHECK_NAME = "ck_instance_connections_auth_class"

# (auth_class, [connection_types]) — mirrors AUTH_CLASS_BY_TYPE.
_BACKFILL = (
    ("oauth_token", ("calendar", "crm")),
    ("provisioned_resource", ("email_sender", "sms_sender")),
    ("api_key", ("record_source", "outbound_webhook")),
)

_ALLOWED = (
    "oauth_token",
    "long_lived_token",
    "api_key",
    "provisioned_resource",
)


def upgrade() -> None:
    # 1. Add the column NOT NULL with a temporary default so the add
    #    succeeds against existing rows.
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.String(length=32),
            nullable=False,
            server_default="api_key",
            comment=(
                "§3.8.5 credential-shape class driving the health/refresh "
                "worker cadence + action: oauth_token / long_lived_token / "
                "api_key / provisioned_resource. Derived from connection_type "
                "via auth_class_for at create time."
            ),
        ),
    )

    # 2. Backfill existing rows by connection_type.
    conn = op.get_bind()
    for auth_class, conn_types in _BACKFILL:
        conn.execute(
            sa.text(
                f"UPDATE {_TABLE} SET {_COLUMN} = :ac "
                f"WHERE connection_type::text IN :types"
            ).bindparams(
                sa.bindparam("ac", value=auth_class),
                sa.bindparam("types", value=tuple(conn_types), expanding=True),
            )
        )

    # 3. Drop the temporary default — new inserts must supply the value.
    op.alter_column(_TABLE, _COLUMN, server_default=None)

    # 4. Pin the four-value vocabulary at the DB layer (honesty backstop).
    allowed = ", ".join(f"'{v}'" for v in _ALLOWED)
    op.create_check_constraint(
        _CHECK_NAME,
        _TABLE,
        f"{_COLUMN} IN ({allowed})",
    )


def downgrade() -> None:
    op.drop_constraint(_CHECK_NAME, _TABLE, type_="check")
    op.drop_column(_TABLE, _COLUMN)
