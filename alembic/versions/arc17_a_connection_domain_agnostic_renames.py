"""Arc 17 A — domain-agnostic connection renames.

Revision ID: arc17_a_connection_domain_agnostic_renames
Revises: arc15_c_drop_system_prompt_additions
Create Date: 2026-06-03

Why this migration exists
-------------------------
Two founder-directed renames against the shipped ``instance_connections``
table (Arc 15 WU4 / the Arc 17 connection slice). The shipped
``arc15_b_instance_connections`` migration stays INTACT — this is an
amendment on top, never an in-place edit of a shipped migration.

1. Locked Decision #5 (domain-agnostic naming): the ``connection_type``
   PG enum drifted to the real-estate-specific value ``property_source``.
   Postgres 10+ supports ``ALTER TYPE ... RENAME VALUE``, which preserves
   every existing row (the enum's on-disk oid is stable; only the label
   text changes), so this is data-safe and reversible.

2. Arc 17 brief wording: the column ``last_verified_at`` is renamed to
   ``last_health_check_at`` to match the health-check vocabulary the
   refresh endpoint + token-refresh worker use.

Rollback contract
-----------------
``downgrade`` renames both back. ``ALTER TYPE RENAME VALUE`` and
``ALTER TABLE RENAME COLUMN`` are both metadata-only and fully
reversible; no data is rewritten in either direction.
"""
from __future__ import annotations

from alembic import op


revision = "arc17_a_connection_domain_agnostic_renames"
down_revision = "arc15_c_drop_system_prompt_additions"
branch_labels = None
depends_on = None


_TABLE = "instance_connections"
_ENUM = "connection_type"


def upgrade() -> None:
    # 1. Rename the enum VALUE in place (Postgres 10+). Metadata-only;
    #    every existing row's stored label flips atomically.
    op.execute(
        f"ALTER TYPE {_ENUM} RENAME VALUE 'property_source' TO 'record_source';"
    )

    # 2. Rename the health-check timestamp column.
    op.alter_column(
        _TABLE,
        "last_verified_at",
        new_column_name="last_health_check_at",
    )


def downgrade() -> None:
    op.alter_column(
        _TABLE,
        "last_health_check_at",
        new_column_name="last_verified_at",
    )
    op.execute(
        f"ALTER TYPE {_ENUM} RENAME VALUE 'record_source' TO 'property_source';"
    )
