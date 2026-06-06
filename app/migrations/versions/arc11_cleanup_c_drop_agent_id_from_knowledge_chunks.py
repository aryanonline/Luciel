"""Arc 11 Cleanup C — drop legacy ``agent_id`` column from
``knowledge_chunks``.

Revision ID: arc11_cleanup_c_drop_agent_id_from_knowledge_chunks
Revises: arc11_cleanup_b_drop_legacy_source_columns
Create Date: 2026-05-28

Why this migration exists
-------------------------

``knowledge_chunks.agent_id`` is the last remnant of the pre-Step-24.5
agent layer. It is a ``String(100)`` column wired through the
retriever, repository, and ingestion modules as a read-side compat
branch for legacy rows. Production has 0 such rows
(ARC11_PLAN.md §11 Q3 LOCKED). Cleanup C drops the column entirely
alongside the application-side read-compat fan-out.

Steps
-----

1. Drop the single-column index ``ix_knowledge_chunks_agent_id`` that
   the original additive migration created (later renamed alongside
   the table in ``arc11_b_rename_embeddings_to_chunks``).
2. Drop the column.

There is no composite index that includes ``agent_id`` on
``knowledge_chunks`` post-Cleanup-B (the only composite,
``ix_knowledge_chunks_scope_source``, lives on
``(admin_id, domain_id, luciel_instance_id, source_id)``). So nothing
else needs touching.

Production safety
-----------------

``knowledge_chunks`` has 0 rows in production (ARC11_PLAN.md §12), and
0 rows have ``agent_id`` set even across dev/test environments. The
column drop is an instant DDL.

Rollback
--------

``downgrade()`` re-adds the column as nullable + recreates the index
under the post-Step-2 name. Data is not reconstructable (it was
already empty when the column was dropped).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# ---------------------------------------------------------------------
# Alembic identifiers.
# ---------------------------------------------------------------------
revision = "arc11_cleanup_c_drop_agent_id_from_knowledge_chunks"
down_revision = "arc11_cleanup_b_drop_legacy_source_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop the single-column index. Use IF EXISTS — the original
    #    index name across the table-rename history is
    #    ``ix_knowledge_chunks_agent_id``, but a defensive IF EXISTS
    #    lets the migration succeed on environments where the
    #    pre-rename index name (``ix_knowledge_embeddings_agent_id``)
    #    survived a partial table-rename.
    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_agent_id")
    op.execute("DROP INDEX IF EXISTS ix_knowledge_embeddings_agent_id")

    # 2. Drop the column.
    op.drop_column("knowledge_chunks", "agent_id")


def downgrade() -> None:
    # Re-add as nullable. Data is NOT reconstructable.
    op.add_column(
        "knowledge_chunks",
        sa.Column("agent_id", sa.String(length=100), nullable=True),
    )
    op.create_index(
        "ix_knowledge_chunks_agent_id",
        "knowledge_chunks",
        ["agent_id"],
    )
