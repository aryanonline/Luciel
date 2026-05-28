"""Arc 11 Cleanup B — drop legacy source columns, rename source_fk
to source_id, set NOT NULL.

Revision ID: arc11_cleanup_b_drop_legacy_source_columns
Revises: arc11_cleanup_a_data_category_rename
Create Date: 2026-05-28

Why this migration exists
-------------------------

Arc 11 Step 1 introduced ``knowledge_sources`` + an additive
``source_fk BIGINT`` column on ``knowledge_chunks``. The original
``source_id String(100)`` column and the orthogonal free-text
``source String(500)`` column have lived alongside ``source_fk``
throughout the Arc-11 cutover.

Cleanup A (the preceding migration) stopped writing the legacy
``source_id`` String column on new chunk rows. Cleanup B finishes
the job:

  1. Set ``source_fk`` NOT NULL. Production has 0 chunk rows
     (ARC11_PLAN.md §12); for dev/test data, a guarded
     ``UPDATE ... WHERE source_fk IS NULL`` runs before the NOT
     NULL flip, picking the first ``knowledge_sources`` row as a
     fallback. If no source rows exist, the UPDATE is a no-op and
     the NOT NULL flip succeeds because the chunk table is empty.
  2. Drop the legacy stringy ``source_id`` String column.
  3. Drop the orthogonal free-text ``source`` String column.
  4. Drop the legacy composite index that referenced the old
     ``source_id`` (String) column, and recreate it on the new
     INTEGER FK column.
  5. Rename ``source_fk`` → ``source_id``. The legacy column is
     gone by step 2, so the name is free.
  6. Drop the FK constraint ``fk_knowledge_chunks_source_fk`` and
     recreate it as ``fk_knowledge_chunks_source_id`` pointing at
     the renamed column. (``ALTER TABLE ... RENAME COLUMN`` does
     NOT rename owned constraints in Postgres.)
  7. Drop the ``ix_knowledge_chunks_source_fk`` index and recreate
     as ``ix_knowledge_chunks_source_id``.

Production safety
-----------------

Production has 0 chunk rows + 0 source rows (ARC11_PLAN.md §12), so
every operation in ``upgrade()`` runs against an empty table and
the migration completes in milliseconds. The guarded UPDATE is
correctness insurance for any dev/test environment that loaded
data between plan-writing and the close.

Rollback
--------

``downgrade()`` symmetrically reverses every step: re-add the
legacy columns as nullable, restore the FK constraint name, rename
``source_id`` → ``source_fk``, restore the legacy String composite
index. The legacy data is NOT reconstructable — but the columns
go back to NULL, which matches the pre-Cleanup-A state for any
rows that were created post-Cleanup-A.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# ---------------------------------------------------------------------
# Alembic identifiers.
# ---------------------------------------------------------------------
revision = "arc11_cleanup_b_drop_legacy_source_columns"
down_revision = "arc11_cleanup_a_data_category_rename"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Backfill source_fk for any chunks that have NULL FKs (dev/
    #    test data only — prod has 0 rows). Use a single fallback
    #    source row if any exist; if none, the UPDATE matches 0
    #    rows and the next NOT NULL flip succeeds against an empty
    #    table. The guarded DO block prevents the migration from
    #    failing on environments with no knowledge_sources at all.
    op.execute(
        """
        DO $$
        DECLARE fallback_fk BIGINT;
        BEGIN
            SELECT id INTO fallback_fk FROM knowledge_sources ORDER BY id ASC LIMIT 1;
            IF fallback_fk IS NOT NULL THEN
                UPDATE knowledge_chunks
                   SET source_fk = fallback_fk
                 WHERE source_fk IS NULL;
            END IF;
        EXCEPTION WHEN OTHERS THEN
            -- Defensive: tolerate a missing knowledge_sources table
            -- so a partial downgrade-followed-by-reupgrade can still
            -- proceed.
            NULL;
        END
        $$;
        """
    )

    # 2. Flip source_fk to NOT NULL.
    op.alter_column(
        "knowledge_chunks",
        "source_fk",
        existing_type=sa.BigInteger(),
        nullable=False,
    )

    # 3. Drop the legacy composite index referencing the old String
    #    source_id column. We'll recreate the equivalent on the new
    #    INTEGER FK after the rename.
    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_scope_source")

    # 4. Drop the legacy String source_id column + the orthogonal
    #    free-text source column.
    op.drop_column("knowledge_chunks", "source_id")
    op.drop_column("knowledge_chunks", "source")

    # 5. Drop the FK constraint that names the column being renamed —
    #    Postgres won't rename a constraint along with the column.
    op.drop_constraint(
        "fk_knowledge_chunks_source_fk",
        "knowledge_chunks",
        type_="foreignkey",
    )

    # 6. Drop the index on source_fk (will be recreated under the new
    #    column name after the rename).
    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_source_fk")

    # 7. Rename source_fk → source_id. The legacy column is gone, so
    #    the name is free.
    op.alter_column(
        "knowledge_chunks",
        "source_fk",
        new_column_name="source_id",
        existing_type=sa.BigInteger(),
        existing_nullable=False,
    )

    # 8. Recreate the FK constraint under its new name pointing at
    #    the renamed column.
    op.create_foreign_key(
        "fk_knowledge_chunks_source_id",
        "knowledge_chunks",
        "knowledge_sources",
        ["source_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 9. Recreate the single-column index on the new column name.
    op.create_index(
        "ix_knowledge_chunks_source_id",
        "knowledge_chunks",
        ["source_id"],
    )

    # 10. Recreate the composite index that the model declares. The
    #     new shape includes the renamed source_id (BIGINT FK).
    op.create_index(
        "ix_knowledge_chunks_scope_source",
        "knowledge_chunks",
        ["admin_id", "domain_id", "luciel_instance_id", "source_id"],
    )


def downgrade() -> None:
    # Symmetric reversal. Re-introduce both legacy columns as
    # nullable, restore the old FK constraint name + indexes.

    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_scope_source")
    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_source_id")

    op.drop_constraint(
        "fk_knowledge_chunks_source_id",
        "knowledge_chunks",
        type_="foreignkey",
    )

    # Rename source_id → source_fk.
    op.alter_column(
        "knowledge_chunks",
        "source_id",
        new_column_name="source_fk",
        existing_type=sa.BigInteger(),
        existing_nullable=False,
    )

    # Re-create the legacy FK constraint name on the renamed column.
    op.create_foreign_key(
        "fk_knowledge_chunks_source_fk",
        "knowledge_chunks",
        "knowledge_sources",
        ["source_fk"],
        ["id"],
        ondelete="CASCADE",
    )

    # Recreate the index that lived on source_fk pre-Cleanup-B.
    op.create_index(
        "ix_knowledge_chunks_source_fk",
        "knowledge_chunks",
        ["source_fk"],
    )

    # Drop NOT NULL — the column was nullable pre-Cleanup-B.
    op.alter_column(
        "knowledge_chunks",
        "source_fk",
        existing_type=sa.BigInteger(),
        nullable=True,
    )

    # Re-add the legacy String columns as nullable. Data is NOT
    # reconstructable — they come back NULL on every row.
    op.add_column(
        "knowledge_chunks",
        sa.Column("source", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "knowledge_chunks",
        sa.Column("source_id", sa.String(length=100), nullable=True),
    )
    op.create_index(
        "ix_knowledge_chunks_source_id_legacy",
        "knowledge_chunks",
        ["source_id"],
    )

    # Recreate the pre-Cleanup-B composite index (it referenced the
    # legacy String source_id; matching the model state at that
    # revision).
    op.create_index(
        "ix_knowledge_chunks_scope_source",
        "knowledge_chunks",
        ["admin_id", "domain_id", "luciel_instance_id", "source_id"],
    )
