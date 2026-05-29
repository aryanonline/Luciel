"""Arc 12 EX3 — drop knowledge_chunks.domain_id + dependent indexes.

Revision ID: arc12_ex3_drop_knowledge_domain
Revises: arc12_ex3_drop_conversation_domain
Create Date: 2026-05-29

Single-table cleanup: removes the legacy ``domain_id`` String(100) column
from ``knowledge_chunks``. The column was the last legacy scoping half
on the chunks table; v2 scopes knowledge by ``(admin_id,
luciel_instance_id)`` (Wall 3 / Architecture §3.7.2) and the
``knowledge_chunks`` RLS policy already keys on that pair exclusively
(EX2). No live RLS predicate, audit-chain entry, or service path reads
``KnowledgeChunk.domain_id`` post-EX1b — the retriever passes
``domain_id=None`` to the repository, the ingestion path's
``_resolve_chunking_config`` kwarg was a documented no-op, and the
repository's add_chunks call site in ingestion.py persisted ``None``.

Indexes affected
----------------

Two composite indexes on ``knowledge_chunks`` reference ``domain_id``:

  1. ``ix_knowledge_scope`` — ``(admin_id, domain_id, knowledge_type)``.
     Backed the legacy "list chunks for this admin+domain by type"
     lookup. With ``domain_id`` removed, the remaining
     ``(admin_id, knowledge_type)`` pair is not on a hot path — list
     reads in the repository already filter by ``luciel_instance_id``
     first, and the existing single-column ``ix_knowledge_chunks_admin_id``
     btree (from the column-level ``index=True``) covers the admin-scope
     case. Drop without re-creating.
  2. ``ix_knowledge_chunks_scope_source`` — ``(admin_id, domain_id,
     luciel_instance_id, source_id)``. Backed soft-delete cascade /
     source-grouped lookups. The remaining triple
     ``(admin_id, luciel_instance_id, source_id)`` is exactly the
     filter shape used by ``soft_delete_chunks_for_source_id`` and
     the v2 union-inheritance scope reads, so RE-CREATE it without
     ``domain_id``.

``IF EXISTS`` is used defensively — prior arcs (e.g. EX2 RLS work)
may have already cleaned up some index residue.

Downgrade
---------

Re-adds ``domain_id`` as NULLABLE (matches the pre-drop nullability —
the column was always nullable in the ORM). Drops the v2 narrow
composite and recreates the original two indexes.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "arc12_ex3_drop_knowledge_domain"
down_revision = "arc12_ex3_drop_conversation_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop dependent indexes FIRST so the column drop has no
    #    DependentObjectsStillExist hazard. IF EXISTS keeps the
    #    migration idempotent across environments where a prior arc
    #    already cleaned them up.
    op.execute("DROP INDEX IF EXISTS public.ix_knowledge_scope")
    op.execute(
        "DROP INDEX IF EXISTS public.ix_knowledge_chunks_scope_source"
    )

    # 2. Re-create the source-grouped composite without ``domain_id``.
    #    Filter shape used by soft_delete_chunks_for_source_id and the
    #    v2 union-inheritance scope reads.
    op.create_index(
        "ix_knowledge_chunks_scope_source",
        "knowledge_chunks",
        ["admin_id", "luciel_instance_id", "source_id"],
        unique=False,
    )

    # 3. Drop the column. No FK targets it (it was a stringy
    #    composite-natural-key half, never had its own FK).
    op.drop_column("knowledge_chunks", "domain_id")


def downgrade() -> None:
    # Re-add NULLABLE (matches pre-drop nullability).
    op.add_column(
        "knowledge_chunks",
        sa.Column(
            "domain_id",
            sa.String(length=100),
            nullable=True,
        ),
    )

    # Drop the v2 narrow composite so the wide form can be recreated.
    op.execute(
        "DROP INDEX IF EXISTS public.ix_knowledge_chunks_scope_source"
    )

    # Recreate the original wide composite.
    op.create_index(
        "ix_knowledge_chunks_scope_source",
        "knowledge_chunks",
        ["admin_id", "domain_id", "luciel_instance_id", "source_id"],
        unique=False,
    )

    # Recreate the type-scoped composite.
    op.create_index(
        "ix_knowledge_scope",
        "knowledge_chunks",
        ["admin_id", "domain_id", "knowledge_type"],
        unique=False,
    )
