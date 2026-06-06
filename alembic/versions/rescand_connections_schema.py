"""RESCAN TIER-B(conn): connections schema completion — enum extension, new
columns, constraint fix (Architecture §3.8.2, §3.8.4, §3.6.7).

Revision ID: rescand_connections_schema
Revises: rescand_lifecycle_states
Create Date: 2026-06-11

What this migration does
------------------------

1. **Extend connection_status enum** — adds ``revoked`` and ``dormant``.

   Architecture §3.8.4 specifies six connection states:
     unconfigured / connected / error / expired / revoked / dormant

   The existing enum (created by arc15_b_instance_connections) only
   carries the first four.  This revision adds the two missing values.

   * ``revoked`` — explicit revoke on deactivation/lifecycle cascade.
     The broker only dispatches to ``connected`` rows; revoked rows are
     retained-but-not-dispatched.  NOTE: the table already carries a
     ``revoked_at`` timestamp column (soft-delete §5.5 Pattern E).
     BOTH representations are now in sync: ``revoked_at`` IS NOT NULL ⟹
     ``status = 'revoked'``.  See DUAL REVOKED REPRESENTATION NOTE below.
   * ``dormant`` — Pro → Free downgrade preserves connections per §3.6.7
     ("retain secrets, do not purge; restore on re-upgrade").  Dormant
     rows are retained with secret_ref intact; the broker skips them
     the same way it skips revoked rows.

   PG ENUM ADD VALUE — transaction isolation note
   -----------------------------------------------
   ``ALTER TYPE ... ADD VALUE`` cannot run in the same transaction as
   DDL that references the new value on some PG versions.  Pattern copied
   from rescand_lifecycle_states: commit the current (empty) transaction,
   set autocommit=True via the raw DBAPI cursor, execute the ADD VALUE
   statements, then restore autocommit=False so the remaining DDL runs in
   a fresh implicit transaction as normal.

   DUAL REVOKED REPRESENTATION NOTE
   ----------------------------------
   The ``instance_connections`` table carries *two* revoke signals:
     * ``revoked_at`` TIMESTAMPTZ — soft-delete timestamp written by the
       lifecycle-cascade and the connection-revoke path (§5.5 Pattern E).
       Non-NULL means "revoked"; NULL means "live".
     * ``status`` column — now extended to carry ``'revoked'`` as a
       first-class value alongside ``'unconfigured'``, ``'connected'``,
       ``'error'``, ``'expired'``, ``'dormant'``.

   Both signals MUST agree: when revoked_at is set, status is also set to
   ``'revoked'``.  The partial unique index and the broker gate already
   use ``revoked_at IS NULL`` as the liveness predicate; the status column
   is the human-readable / API-surfaced representation used by the
   dashboard and the CJ §7 status chip.

   This dual representation is intentional and is documented here.  The
   Architecture doc (§3.8.2) should be amended to note both columns.

2. **Add status_detail column** (§3.8.2) — ``text NULL``.

   Human-readable detail message for the current status, e.g.:
     "OAuth refresh failed — reconnect in dashboard"
     "Connection dormant; re-upgrade to restore"
   Written by the health-check worker on the ``expired`` path (CJ §7
   "Reconnect needed" chip reads this) and by the dormant path on
   downgrade.  NULL for connected/unconfigured rows.

3. **Add created_by_user_id column** (§3.8.2) — ``uuid NULL FK users.id``.

   The team member who configured the connection.  Nullable for
   back-compat (existing rows have no value; new rows populated at
   configure time).

4. **Fix unique constraint** — change from 4-tuple to 3-tuple.

   The existing ``uq_instance_connections_active`` partial unique index
   covers ``(admin_id, instance_id, connection_type, provider)`` WHERE
   ``revoked_at IS NULL``.  This allows two active connections of the
   same *type* (e.g. two calendar connections — one Google, one generic)
   which contradicts §3.8.2's "single-active-per-type" invariant.

   This migration:
     a. Drops the 4-tuple index.
     b. Creates a new 3-tuple partial unique index:
        ``(admin_id, instance_id, connection_type)``
        WHERE ``revoked_at IS NULL AND status NOT IN ('revoked', 'dormant')``.

   The predicate now excludes both revoked_at IS NOT NULL rows AND
   dormant rows (a dormant connection held from a previous Pro
   subscription should not block a new configuration of the same type
   after re-upgrade). This matches §3.8.2 "single-active-per-type"
   without constraining archived/historical rows.

   FINDING: The live constraint IS the 4-tuple (see arc15_b_instance_connections);
   this migration changes it to the correct 3-tuple as specified.

5. **Update the lookup covering index** — extend its predicate to also
   exclude dormant rows for the broker's hot-path query (broker only
   dispatches to ``connected``; the index predicate was previously only
   ``revoked_at IS NULL``).

Downgrade
---------
* Enum value removal: ENUM values ``revoked`` and ``dormant`` are LEFT IN
  PLACE on downgrade.  PostgreSQL does not support ALTER TYPE … DROP VALUE.
  This is the standard Alembic/PostgreSQL posture; the values are harmless
  if no rows reference them.
* Columns ``status_detail`` and ``created_by_user_id`` are dropped on
  downgrade (nullable adds are safe to reverse).
* Unique constraint: restoring the 4-tuple index on downgrade.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "rescand_connections_schema"
down_revision = "rescand_lifecycle_states"
branch_labels = None
depends_on = None

_TABLE = "instance_connections"
_CONN_STATUS_ENUM = "connection_status"

# New enum values to add.
_NEW_STATUS_VALUES = ("revoked", "dormant")


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Extend the connection_status PG ENUM with 'revoked' and 'dormant'.
    #
    #    ISOLATION: ALTER TYPE ... ADD VALUE cannot run inside a txn that
    #    has already begun DDL.  Copy the pattern from
    #    rescand_lifecycle_states: commit the current (empty) Alembic txn,
    #    run ADD VALUE in autocommit mode, then restore autocommit so
    #    subsequent DDL runs in a fresh implicit transaction.
    # ------------------------------------------------------------------
    conn = op.get_bind()
    dbapi_conn = conn.connection.dbapi_connection
    dbapi_conn.commit()  # return to IDLE state
    try:
        dbapi_conn.autocommit = True
        cur = dbapi_conn.cursor()
        for value in _NEW_STATUS_VALUES:
            cur.execute(
                f"ALTER TYPE {_CONN_STATUS_ENUM} "
                f"ADD VALUE IF NOT EXISTS '{value}'"
            )
        cur.close()
    finally:
        dbapi_conn.autocommit = False  # restore default

    # ------------------------------------------------------------------
    # 2. Add status_detail column — text NULL.
    # ------------------------------------------------------------------
    op.add_column(
        _TABLE,
        sa.Column(
            "status_detail",
            sa.Text(),
            nullable=True,
            comment=(
                "Human-readable detail for the current status. "
                "Written by the health-check worker on expired path "
                "(CJ §7 Reconnect chip) and by the dormant path on "
                "downgrade. NULL for connected/unconfigured rows."
            ),
        ),
    )

    # ------------------------------------------------------------------
    # 3. Add created_by_user_id column — uuid NULL FK users.id.
    # ------------------------------------------------------------------
    op.add_column(
        _TABLE,
        sa.Column(
            "created_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            comment=(
                "The team member (User) who configured this connection. "
                "NULL for connections created before this column was added "
                "or created by system processes."
            ),
        ),
    )

    # ------------------------------------------------------------------
    # 4. Fix unique constraint: drop 4-tuple, create 3-tuple.
    #
    #    Old: (admin_id, instance_id, connection_type, provider)
    #         WHERE revoked_at IS NULL
    #    New: (admin_id, instance_id, connection_type)
    #         WHERE revoked_at IS NULL AND status NOT IN ('revoked', 'dormant')
    #
    #    The predicate excludes dormant rows so a re-upgrade can replace
    #    a dormant connection with a fresh active one of the same type.
    # ------------------------------------------------------------------
    op.drop_index("uq_instance_connections_active", table_name=_TABLE)
    op.create_index(
        "uq_instance_connections_active",
        _TABLE,
        ["admin_id", "instance_id", "connection_type"],
        unique=True,
        postgresql_where=sa.text(
            "revoked_at IS NULL AND status NOT IN ('revoked', 'dormant')"
        ),
    )

    # ------------------------------------------------------------------
    # 5. Update the lookup covering index to also exclude dormant rows
    #    (broker only dispatches to connected; dormant rows are skipped).
    # ------------------------------------------------------------------
    op.drop_index("ix_instance_connections_lookup", table_name=_TABLE)
    op.create_index(
        "ix_instance_connections_lookup",
        _TABLE,
        ["admin_id", "instance_id", "connection_type"],
        postgresql_where=sa.text(
            "revoked_at IS NULL AND status NOT IN ('revoked', 'dormant')"
        ),
    )


def downgrade() -> None:
    # ------------------------------------------------------------------
    # NOTE ON ENUM DOWNGRADE: The new ENUM values ('revoked', 'dormant')
    # are intentionally LEFT IN PLACE.  PostgreSQL does not support
    # ALTER TYPE ... DROP VALUE.  The values are harmless if no rows
    # reference them.  A DBA can remove them by rebuilding the type:
    #   1. Verify no rows reference 'revoked' or 'dormant'.
    #   2. UPDATE instance_connections SET status='unconfigured'
    #      WHERE status IN ('revoked','dormant');
    #   3. DROP TYPE connection_status CASCADE.
    #   4. Recreate with original 4 values; re-ALTER the column.
    # This is out of scope for an automated Alembic downgrade.
    # ------------------------------------------------------------------

    # Restore the 4-tuple unique index (pre-TIER-B shape).
    op.drop_index("uq_instance_connections_active", table_name=_TABLE)
    op.create_index(
        "uq_instance_connections_active",
        _TABLE,
        ["admin_id", "instance_id", "connection_type", "provider"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    # Restore the lookup covering index (pre-TIER-B shape).
    op.drop_index("ix_instance_connections_lookup", table_name=_TABLE)
    op.create_index(
        "ix_instance_connections_lookup",
        _TABLE,
        ["admin_id", "instance_id", "connection_type"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    # Drop the new columns (backward-compatible adds; safe to reverse).
    op.drop_column(_TABLE, "created_by_user_id")
    op.drop_column(_TABLE, "status_detail")
