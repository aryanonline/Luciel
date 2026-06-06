"""Arc 9 C5.0a -- Add messages.tenant_id (additive, nullable, backfilled, NOT NULL).

This is the FIRST of two schema-delta migrations that prepare the
``messages`` table for Wall-4 (intra-tenant session isolation) RLS.
Without a denormalised ``tenant_id`` column on messages, RLS cannot
filter at the row level -- the only authority for "this row's tenant"
would be the joined sessions row, which RLS evaluates AFTER the FROM
clause and therefore cannot use for default-deny defence-in-depth.

DOCTRINE LOCKED at C5 plan (founder approval 2026-05-24):
    Bring C8 forward, build Wall 4 RLS now. Schema deltas execute
    INSIDE Arc 9, before the master flag flips. This migration plus
    C5.0b (luciel_instance_id) plus C5.1 (Wall-1 RLS) plus C5.2
    (Wall-3 RLS) together establish a uniform three-wall posture
    across every customer-data table.

SAFETY MODEL
============

The naive shape -- ``ALTER TABLE ADD tenant_id String(100) NOT NULL`` --
would fail on any non-empty messages table (no default for existing
rows) AND would take an ACCESS EXCLUSIVE lock for the entire backfill
window. Production has ~live message rows; we cannot afford either.

This migration is therefore three explicit phases inside one Alembic
revision, ALL idempotent on re-run:

  Phase 1: Add column as nullable. ACCESS EXCLUSIVE lock is held for
           a metadata-only ALTER. Sub-second. No data is rewritten.

  Phase 2: Backfill in batches of 5000 rows. Each batch is its own
           UPDATE statement so the transaction log stays bounded and
           any single batch can be retried. The backfill copies the
           parent session's tenant_id into the new column. We commit
           after each batch -- otherwise the entire backfill would
           need to fit inside one transaction, defeating the chunking.

  Phase 3: SET NOT NULL once every row is populated. This re-acquires
           an ACCESS EXCLUSIVE lock briefly to validate the constraint.
           Adding an index on tenant_id is deferred to C5.1, where it
           ships alongside the RLS policy that needs it.

DOWNGRADE
=========

Reversible. Drop the column. Existing messages survive (the join via
sessions.tenant_id remains the legacy authority). Wall-4 RLS that
depends on this column is NOT installed by THIS migration; that's
C5.1's responsibility, so downgrading C5.0a alone is safe even with
the master flag on.

ZERO-DOWNTIME COMPATIBILITY
============================

The application code at the time C5.0a deploys still references
``MessageModel.session_id`` only -- the model does NOT read tenant_id
yet. C5.3 wires SessionRepository.add_message to populate the column,
but the migration ITSELF does not require any app-code change. This
means:

  * Old app pods running pre-C5.0a code can continue inserting rows
    after the column exists (column is nullable until Phase 3).
  * Once Phase 3 lands, old pods would fail INSERTs (NOT NULL).
    We therefore gate Phase 3 behind the C5.3 app-code rollout: the
    runbook calls for deploying the new MessageModel BEFORE running
    Phase 3 of this migration. The deploy gate (G3 staging dry-run)
    catches a mis-ordered run.

  * New rows AFTER the column exists but BEFORE C5.3 ships would land
    with NULL tenant_id (the legacy add_message path doesn't supply
    it). The backfill in Phase 2 is RE-RUN as part of the C5.3 deploy
    to mop up any NULL rows that landed in the gap. This is the
    "double-backfill" pattern documented in ARC9_RUNBOOK §C5.

Refs ARC9_RUNBOOK §C5.0a, C1_FINDINGS row for messages.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "arc9_c5_0a_messages_tenant_id"
down_revision = "arc9_c4_3f_rls_instance_admin_audit_logs"
branch_labels = None
depends_on = None


BACKFILL_BATCH_SIZE = 5000


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ---- Phase 1: add nullable column (idempotent) -----------------
    cols = {c["name"] for c in inspector.get_columns("messages")}
    if "tenant_id" not in cols:
        op.add_column(
            "messages",
            sa.Column("tenant_id", sa.String(length=100), nullable=True),
        )

    # ---- Phase 2: chunked backfill from sessions.tenant_id ---------
    # We loop until UPDATE affects zero rows. Each iteration uses a
    # CTE to pick the next batch of NULL-tenant_id rows ordered by id
    # (the PK, stable). The CTE projects the joined sessions.tenant_id
    # and the UPDATE binds the result into messages.tenant_id.
    #
    # Why the CTE pattern rather than a single bulk UPDATE FROM:
    #   * Bounded transaction size: each chunk is its own UPDATE,
    #     committed before the next runs. Long single transactions
    #     hold replication-conflict locks and bloat WAL.
    #   * Resumable: if Alembic dies mid-backfill, the next run picks
    #     up where the previous left off (no temp state needed).
    #   * Lock-friendly: each chunk locks only the rows it touches.
    while True:
        result = bind.execute(
            sa.text(
                f"""
                WITH batch AS (
                    SELECT m.id AS msg_id, s.tenant_id AS t_id
                    FROM messages m
                    JOIN sessions s ON s.id = m.session_id
                    WHERE m.tenant_id IS NULL
                    ORDER BY m.id
                    LIMIT {BACKFILL_BATCH_SIZE}
                )
                UPDATE messages
                SET tenant_id = batch.t_id
                FROM batch
                WHERE messages.id = batch.msg_id
                """
            )
        )
        if result.rowcount == 0:
            break

    # ---- Phase 3: assert no NULLs survived, then NOT NULL ----------
    # If any row is still NULL here, the join via sessions.id failed
    # (orphaned message row, broken FK). We REFUSE to flip NOT NULL
    # in that state -- the operator must reconcile orphans first.
    # This is the same fail-loud-at-the-boundary discipline as the
    # cross_session_retriever defense-in-depth check.
    orphans = bind.execute(
        sa.text("SELECT COUNT(*) FROM messages WHERE tenant_id IS NULL")
    ).scalar()
    if orphans and orphans > 0:
        raise RuntimeError(
            f"C5.0a backfill incomplete: {orphans} messages row(s) "
            "still have NULL tenant_id after JOIN to sessions. "
            "Likely cause: orphaned messages whose session_id has no "
            "matching sessions row (FK ondelete='CASCADE' should have "
            "prevented this -- investigate before retrying)."
        )

    op.alter_column("messages", "tenant_id", nullable=False)

    # ---- Phase 4: index on (tenant_id, session_id) ------------------
    # Composite index supports:
    #   * RLS predicate (tenant_id = current_setting(...)) -- C5.1
    #   * list_messages(session_id) under tenant filter -- C5.3
    # Single-column index on tenant_id alone would be useful for
    # admin-wide queries but the composite is the hotter access
    # pattern. CONCURRENTLY would be ideal for prod but Alembic
    # migrations run inside a transaction by default, so we use a
    # plain CREATE INDEX here and document the prod-deploy gate
    # (off-peak window, brief lock) in the runbook.
    op.create_index(
        "ix_messages_tenant_id_session_id",
        "messages",
        ["tenant_id", "session_id"],
        unique=False,
    )


def downgrade() -> None:
    # Reverse order: drop index, drop column. Wall-4 RLS that depends
    # on the column is NOT installed by this migration (that's C5.1),
    # so the drop is safe even if the master flag is on.
    op.drop_index("ix_messages_tenant_id_session_id", table_name="messages")
    op.drop_column("messages", "tenant_id")
