"""RESCAN TIER-DE — extend instance_status enum to 5 states (Architecture §3.6.1).

Revision ID: rescand_lifecycle_states
Revises: rescanc_graph_kb
Create Date: 2026-06-11

What this migration does
------------------------

Architecture §3.6.1 specifies five discrete lifecycle states for an
Instance:

  active / paused / deactivating / grace_window / hard_deleted

The existing 3-state enum (active / paused / deleted) was created by
``arc11_closeout_a_instance_lifecycle``. This revision extends it to
the full 5-state set while retaining ``deleted`` as a deprecated alias
for ``grace_window`` (documented below).

Changes
-------

1. **Add three new ENUM values** to the PostgreSQL ``instance_status``
   type: ``deactivating``, ``grace_window``, ``hard_deleted``.

   ``deleted`` is kept as-is — it is a documented alias for the
   ``grace_window`` state. Existing rows with
   ``instance_status = 'deleted'`` continue to be valid and are
   treated by the retention worker as equivalent to ``grace_window``
   (both states appear in the worker's scan predicate via the
   ``INSTANCE_GRACE_STATES`` frozenset in
   ``app/models/instance_status.py``).

   Mapping:
     deleted  →  grace_window (alias; same semantics — 30-day clock
                               running from soft_deleted_at)

2. **Update the partial index** ``ix_instances_soft_deleted_sweep``
   to cover both ``'deleted'`` and ``'grace_window'`` states so the
   nightly retention worker picks up rows regardless of which
   vocabulary was used.

PG ENUM ADD VALUE — transaction isolation
-----------------------------------------

On PostgreSQL, ``ALTER TYPE ... ADD VALUE`` cannot be used in the
SAME transaction as DDL that references the new value (the index
CREATE uses ``grace_window`` in its WHERE predicate). PostgreSQL
raises ``UnsafeNewEnumValueUsage`` if you try.

The fix: run the ADD VALUE statements via a raw DBAPI cursor with
``autocommit=True``, committing each new enum label before the
surrounding Alembic transaction resumes. This is the standard pattern
for Alembic ENUM extension when the new values must be referenced in
the same migration file.

This is different from ``arc6_c_pending_downgrade_columns`` which
used a plain ``op.execute()`` — that worked because the new value was
never used in a WHERE clause or CHECK constraint within the same
migration. Here we need the value in the index predicate, so
autocommit is required.

Downgrade — ENUM value removal note
-------------------------------------

PostgreSQL does NOT support removing an ENUM value (``ALTER TYPE ...
DROP VALUE`` is not a valid command as of PG 16). The three new values
(``deactivating``, ``grace_window``, ``hard_deleted``) are therefore
LEFT IN PLACE on downgrade. This is the documented expand-contract
reality for PG enums and is explicitly acceptable per the task spec.

The column and state-logic changes (the partial index update) DO
downgrade. The ENUM type itself retains the new values on downgrade.

To hard-remove the new values, a DBA would need to:
  1. Verify no rows reference the new values.
  2. DROP TYPE instance_status CASCADE.
  3. CREATE TYPE instance_status AS ENUM ('active', 'paused', 'deleted').
  4. ALTER TABLE instances ALTER COLUMN instance_status TYPE instance_status
     USING instance_status::text::instance_status.
This is out of scope for an automated Alembic downgrade.
"""
from __future__ import annotations

from alembic import op

revision = "rescand_lifecycle_states"
down_revision = "rescanc_graph_kb"
branch_labels = None
depends_on = None


# New values to add to the existing instance_status PG ENUM.
_NEW_VALUES = ("deactivating", "grace_window", "hard_deleted")


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Extend the instance_status PG ENUM with three new values.
    #
    #    ISOLATION: ALTER TYPE ... ADD VALUE cannot be used in the same
    #    transaction as DDL that references the new value. Since the
    #    partial index in step 2 uses 'grace_window' in its predicate,
    #    we must commit the ADD VALUE before the CREATE INDEX. We do
    #    this by running the ADD VALUE statements via a raw DBAPI cursor
    #    in AUTOCOMMIT mode, so each new value is committed immediately
    #    and visible to the subsequent index creation.
    # ------------------------------------------------------------------
    conn = op.get_bind()
    # conn.connection is the SQLAlchemy pool _ConnectionFairy;
    # .dbapi_connection is the actual psycopg.Connection object.
    dbapi_conn = conn.connection.dbapi_connection
    # Alembic's begin_transaction() wraps the migration in a txn, so by
    # the time upgrade() runs the connection is in INTRANS state. We must
    # commit the current (empty) transaction to return to IDLE before we
    # can set autocommit=True. The commit is safe here: Alembic's own
    # transaction tracking will start a fresh txn for subsequent DDL.
    dbapi_conn.commit()  # return to IDLE state
    try:
        dbapi_conn.autocommit = True
        cur = dbapi_conn.cursor()
        for value in _NEW_VALUES:
            cur.execute(
                f"ALTER TYPE instance_status "
                f"ADD VALUE IF NOT EXISTS '{value}'"
            )
        cur.close()
    finally:
        dbapi_conn.autocommit = False  # restore default (begin new txn)

    # ------------------------------------------------------------------
    # 2. Update the partial index ix_instances_soft_deleted_sweep to
    #    cover both 'deleted' (legacy alias) and 'grace_window' (new
    #    canonical state for the grace period).
    #
    #    Original predicate:
    #      WHERE instance_status = 'deleted' AND soft_deleted_at IS NOT NULL
    #
    #    New predicate:
    #      WHERE instance_status IN ('deleted', 'grace_window')
    #        AND soft_deleted_at IS NOT NULL
    #
    #    Now that ADD VALUE has been committed (step 1 used autocommit),
    #    'grace_window' can safely be referenced in DDL here.
    # ------------------------------------------------------------------
    op.execute("DROP INDEX IF EXISTS ix_instances_soft_deleted_sweep")
    op.execute(
        "CREATE INDEX ix_instances_soft_deleted_sweep "
        "ON instances (soft_deleted_at) "
        "WHERE instance_status IN ('deleted', 'grace_window') "
        "AND soft_deleted_at IS NOT NULL"
    )


def downgrade() -> None:
    # ------------------------------------------------------------------
    # NOTE ON ENUM DOWNGRADE: The three new ENUM values
    # ('deactivating', 'grace_window', 'hard_deleted') are intentionally
    # LEFT IN PLACE. PostgreSQL does not support ALTER TYPE ... DROP VALUE.
    # This is the standard Alembic/PostgreSQL posture for ENUM extensions;
    # the values are harmless if no rows reference them. Full removal
    # requires a DBA to rebuild the type (see module docstring).
    # ------------------------------------------------------------------

    # Restore the original partial index predicate (pre-TIER-DE shape).
    op.execute("DROP INDEX IF EXISTS ix_instances_soft_deleted_sweep")
    op.execute(
        "CREATE INDEX ix_instances_soft_deleted_sweep "
        "ON instances (soft_deleted_at) "
        "WHERE instance_status = 'deleted' "
        "AND soft_deleted_at IS NOT NULL"
    )
