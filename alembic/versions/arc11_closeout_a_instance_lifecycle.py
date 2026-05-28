"""Arc 11 Closeout PR-A — instance lifecycle status enum + soft-delete sweep index.

Revision ID: arc11_closeout_a_instance_lifecycle
Revises: arc11_cleanup_c_user_invites_role_enum
Create Date: 2026-05-28

Doctrine anchors
----------------

* Customer Journey §4.5 Phase 8 — three distinct lifecycle affordances
  on the "Manage account" surface: Pause (operational), Delete (30-day
  grace), Close account (Arc 10).

* Architecture §3.6.1 — soft-delete window measured from
  ``soft_deleted_at`` (locked). The retention worker reads this column
  to find instances 30 days past deactivation and hard-delete their
  knowledge embeddings + conversations.

* Vision §6.4 Reactivation — Admin clicks Restore within 30 days →
  knowledge restored, embed keys re-minted (new keys, old keys stay
  revoked), capacity slot consumed again.

What this migration adds
------------------------

1. A PostgreSQL ENUM type ``instance_status`` with members
   ``active``, ``paused``, ``deleted``. Mirrors the Python enum in
   ``app/models/instance_status.py``. Same shape as Arc 11 Cleanup C's
   ``scope_role`` enum (four-member locked role enum) — that pattern is
   the doctrinal precedent.

2. ``instances.instance_status`` column, NOT NULL, default ``active``,
   so existing rows pick up the new column without backfill churn.

3. Backfill: any row with the legacy ``active = FALSE`` flag is
   mapped to ``paused``. The conservative read of pre-Arc-11 state:
   those rows were deactivated via the old ``DELETE /instances/{pk}``
   route which only flipped ``active = FALSE`` and never stamped
   ``soft_deleted_at``. Treating them as ``paused`` preserves the
   "data retained, reactivatable" semantics; treating them as
   ``deleted`` would falsely start a 30-day grace clock that was
   never authorized by the user.

4. Partial index ``ix_instances_soft_deleted_sweep`` over
   ``soft_deleted_at`` filtered to ``instance_status = 'deleted'`` and
   ``soft_deleted_at IS NOT NULL``. Backs the retention worker's
   nightly scan predicate (``app/worker/tasks/instance_retention.py``)
   — same partial-index shape as the
   ``ix_admins_closure_clock_eligible`` index Arc 10 added for the
   tenant-level retention worker.

Production safety
-----------------

* The column add is non-blocking: NOT NULL with a constant default,
  which PostgreSQL 11+ handles as a catalog-only change (no full table
  rewrite). The ``instances`` table is small (one row per Admin per
  product), so even on older Postgres this would be sub-second.

* The backfill is a single UPDATE keyed on the existing
  ``ix_instances_active`` index. Production currently has zero rows
  with ``active = FALSE`` (verified via the audit log: the old DELETE
  route hadn't been exercised in prod), so this UPDATE is effectively
  a no-op there but is correct for any dev/staging row that has been
  deactivated.

* The partial index is created with the standard ``CREATE INDEX``
  form (not ``CONCURRENTLY``) because the table is small and is not
  under heavy write load. If a future revision needs to retro-fit this
  on a larger table, switch to ``CONCURRENTLY`` and the corresponding
  Alembic incantation.

Downgrade
---------

Symmetric: drop the partial index, drop the column, drop the enum
type. The legacy ``active`` column survives unchanged — Arc 12 will
drop it in a separate, clearly bounded revision.
"""
from __future__ import annotations

from alembic import op


# ---------------------------------------------------------------------
# Alembic identifiers.
# ---------------------------------------------------------------------
revision = "arc11_closeout_a_instance_lifecycle"
down_revision = "arc11_cleanup_c_user_invites_role_enum"
branch_labels = None
depends_on = None


_STATUS_VALUES = ("active", "paused", "deleted")


def upgrade() -> None:
    # 1. CREATE TYPE instance_status AS ENUM ('active','paused','deleted')
    op.execute(
        "CREATE TYPE instance_status AS ENUM ("
        + ", ".join(f"'{v}'" for v in _STATUS_VALUES)
        + ")"
    )

    # 2. Add the column NOT NULL with default 'active'. PostgreSQL 11+
    #    treats a constant-default add as a catalog-only change, so
    #    this is safe on any non-trivial table size.
    op.execute(
        "ALTER TABLE instances "
        "ADD COLUMN instance_status instance_status "
        "NOT NULL DEFAULT 'active'"
    )

    # 3. Backfill existing legacy-deactivated rows to 'paused'. Anything
    #    that already had soft_deleted_at stamped (currently zero such
    #    rows in prod) would also have active=False and is therefore
    #    captured here as 'paused' — the conservative read; the route
    #    layer will re-stamp to 'deleted' the next time those rows are
    #    explicitly deleted via the new DELETE route.
    op.execute(
        "UPDATE instances "
        "SET instance_status = 'paused' "
        "WHERE active = FALSE"
    )

    # 4. Partial index for the retention worker's nightly scan.
    op.execute(
        "CREATE INDEX ix_instances_soft_deleted_sweep "
        "ON instances (soft_deleted_at) "
        "WHERE instance_status = 'deleted' "
        "AND soft_deleted_at IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_instances_soft_deleted_sweep")
    op.execute("ALTER TABLE instances DROP COLUMN IF EXISTS instance_status")
    op.execute("DROP TYPE IF EXISTS instance_status")
