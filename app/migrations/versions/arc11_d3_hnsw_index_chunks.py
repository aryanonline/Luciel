"""Arc 11 Step 4 (d3) — HNSW vector index on knowledge_chunks.embedding.

Revision ID: arc11_d3_hnsw_index_chunks
Revises: arc11_d2_rls_chunks_postrename_verify
Create Date: 2026-05-28

Why this migration exists
-------------------------

The retriever (``KnowledgeRepository.search_similar``) orders by
``embedding <=> :query_vec`` — pgvector's cosine-distance operator.
Without a vector index, this is a sequential scan of every chunk
for every query; at the scale Architecture v1 §3.2 forecasts (~10M
chunks per large enterprise admin), that is unworkable.

This migration installs the HNSW index Architecture v1 §3.2
specifies:

    CREATE INDEX ix_knowledge_chunks_embedding_hnsw
        ON knowledge_chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64);

Why HNSW (not IVFFlat)
----------------------

The very first knowledge embeddings migration (b0e003ffa07f) shipped
an IVFFlat index named ``ix_knowledge_embedding_vector``. IVFFlat is
the older pgvector index type — fast to build, but with a known
recall/latency trade-off that gets worse as the dataset shifts away
from the centroid distribution it was built against. HNSW is the
modern choice (pgvector ≥ 0.5.0; prod is on 0.8.1 per
ARC11_PLAN.md §12). It builds slower but holds recall + latency
better as the index grows and updates land.

Both indexes can coexist on the same column. The Arc 10 IVFFlat
index is **NOT** dropped here — the planner will pick whichever
gives a better plan for a given query. Step 11 cleanup decides
whether to retire the legacy IVFFlat index after a soak period
with HNSW in place.

Parameters
----------

  * m = 16 — pgvector default. Controls graph connectivity. Higher
    m means more memory + slower build but better recall.
  * ef_construction = 64 — pgvector default. Construction-time
    candidate list size. Higher is more accurate but slower to
    build. Per the plan §2.3 these are the defaults that perform
    well up to ~1M vectors, which is well beyond the v1 ceiling.

CONCURRENTLY
------------

We deliberately do NOT use ``CREATE INDEX CONCURRENTLY``. Alembic
runs DDL inside a transaction by default; ``CONCURRENTLY`` errors
out inside a transaction. The same trade-off applies as in
``d8e2c4b1a0f3_step29y_cluster4_worker_rejection_idempotency.py``:
production is empty (ARC11_PLAN.md §12), so the brief table lock
during index build is negligible. The operator runbook for the
first prod deploy after this migration lands should note that
re-applying it against a populated table later (re-creation,
disaster recovery) requires a manual concurrent build out of band.

Idempotency
-----------

``CREATE INDEX IF NOT EXISTS`` so re-running the migration on a DB
that already has the index is a no-op. The downgrade uses
``DROP INDEX IF EXISTS`` symmetrically.

Rollback
--------

Drops the index. The IVFFlat index on the embedding column
(installed by b0e003ffa07f) remains, so queries still have *an*
index plan available. Worst-case post-downgrade is a partial
recall regression on freshly-inserted chunks; not a correctness
issue.
"""
from __future__ import annotations

from alembic import op


revision = "arc11_d3_hnsw_index_chunks"
down_revision = "arc11_d2_rls_chunks_postrename_verify"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_embedding_hnsw
            ON knowledge_chunks
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS ix_knowledge_chunks_embedding_hnsw;"
    )
