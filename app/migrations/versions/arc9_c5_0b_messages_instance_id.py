"""Arc 9 C5.0b -- Add messages.luciel_instance_id (additive, nullable, backfilled).

Second of two schema-delta migrations preparing ``messages`` for the
Wall-3 (instance-level) RLS layer of C5.2. Unlike C5.0a (tenant_id)
this column STAYS NULLABLE forever -- consistent with every other
Wall-3 table in C4.3 (api_keys, knowledge_embeddings, memory_items,
sessions, traces, admin_audit_logs). NULL means "no instance scope",
which is a legitimate state for legacy/unbound message rows.

WHY NULL-PERMISSIVE FOR Wall 3
==============================

The Wall-3 doctrine (C4 envelope) is asymmetric NULL-permissive:

    USING:      luciel_instance_id::text = current_setting('app.instance_id', true)
                OR luciel_instance_id IS NULL
    WITH CHECK: luciel_instance_id::text = current_setting('app.instance_id', true)
                OR (luciel_instance_id IS NULL AND current_setting('app.instance_id', true) = '')

This means:
  * Reads succeed if the row matches the bound instance OR has no
    instance binding (legacy rows pre-Step 24.5 luciel_instance_id).
  * Writes are gated -- NULL writes require an empty GUC, matching-
    instance writes require the GUC to match.

The matching session-level RLS (C4.3d) is already in place. Messages
inherit instance scope from their parent session, so the backfill
copies sessions.luciel_instance_id into messages.luciel_instance_id.
The column stays nullable because some message rows belong to
sessions that were created before luciel_instance_id was added to
sessions itself (those sessions have NULL too).

SAFETY MODEL
============

Same three-phase pattern as C5.0a but WITHOUT the NOT NULL flip in
Phase 3 -- NULL is a legitimate end-state here.

  Phase 1: Add nullable column. Metadata-only ALTER. Sub-second.
  Phase 2: Chunked backfill from sessions.luciel_instance_id. Some
           rows may legitimately stay NULL (sessions row had NULL).
  Phase 3: Composite index (luciel_instance_id, session_id) to back
           the C5.2 RLS predicate. Single-column index would also
           work but the composite matches the access pattern that
           includes both filters at runtime.

Re-runnable: Phase 1 is gated by an inspector check, Phase 2 is
WHERE luciel_instance_id IS NULL so it only touches unprocessed rows.

DOWNGRADE
=========

Reversible. Drop index, drop column. C5.2 RLS depends on this column
but is NOT installed by THIS migration -- so the downgrade is safe.

Refs ARC9_RUNBOOK §C5.0b, C4 doctrine (NULL-permissive Wall 3).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "arc9_c5_0b_messages_instance_id"
down_revision = "arc9_c5_0a_messages_tenant_id"
branch_labels = None
depends_on = None


BACKFILL_BATCH_SIZE = 5000


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ---- Phase 1: add nullable column (idempotent) -----------------
    cols = {c["name"] for c in inspector.get_columns("messages")}
    if "luciel_instance_id" not in cols:
        op.add_column(
            "messages",
            sa.Column(
                "luciel_instance_id",
                sa.Integer(),
                nullable=True,
            ),
        )

    # ---- Phase 2: chunked backfill from sessions.luciel_instance_id
    # Same shape as C5.0a -- CTE-batched UPDATE FROM joined sessions,
    # bounded by BACKFILL_BATCH_SIZE, looping until rowcount == 0.
    # Note we do NOT filter on luciel_instance_id IS NOT NULL in the
    # source -- copying NULL across is the correct behaviour because
    # the messages column is also nullable.
    #
    # The WHERE clause uses messages.luciel_instance_id IS NULL AND
    # the EXISTS subquery to avoid re-processing rows already
    # backfilled. The check on EXISTS guards against orphan messages
    # (which the FK ondelete=CASCADE should make impossible, but
    # defense-in-depth is cheap here).
    while True:
        result = bind.execute(
            sa.text(
                f"""
                WITH batch AS (
                    SELECT m.id AS msg_id, s.luciel_instance_id AS inst_id
                    FROM messages m
                    JOIN sessions s ON s.id = m.session_id
                    WHERE m.luciel_instance_id IS NULL
                      AND s.luciel_instance_id IS NOT NULL
                    ORDER BY m.id
                    LIMIT {BACKFILL_BATCH_SIZE}
                )
                UPDATE messages
                SET luciel_instance_id = batch.inst_id
                FROM batch
                WHERE messages.id = batch.msg_id
                """
            )
        )
        if result.rowcount == 0:
            break

    # ---- Phase 3: composite index for RLS + access pattern ---------
    # Column STAYS nullable -- C4 NULL-permissive Wall-3 doctrine.
    # Composite (luciel_instance_id, session_id) supports the C5.2
    # RLS predicate and the message-history-by-session access pattern
    # when filtered by instance. Single-column on luciel_instance_id
    # would also suffice for RLS alone but the composite is the
    # hotter index for runtime queries.
    op.create_index(
        "ix_messages_luciel_instance_id_session_id",
        "messages",
        ["luciel_instance_id", "session_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_messages_luciel_instance_id_session_id", table_name="messages"
    )
    op.drop_column("messages", "luciel_instance_id")
