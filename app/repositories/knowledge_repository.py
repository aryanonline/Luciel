"""
Knowledge repository — post-Cleanup-B.

Scope-aware CRUD over the ``knowledge_chunks`` table. After
Cleanup B's legacy-column drops, the chunk table has a single
source binding: an INTEGER FK column named ``source_id``
referencing ``knowledge_sources.id`` (NOT NULL). The pre-Cleanup
String ``source_id`` column and the orthogonal free-text
``source`` column are both gone.

What that means for callers:

  * ``add_chunks(... source_id: int)`` — the FK is mandatory.
    Pre-Cleanup-A's optional stringy ``source_id`` + optional
    ``source_fk`` pair collapse to a single required INTEGER FK.
  * The chunk-side grouping methods that used to key on the
    String ``source_id`` (``get_active_source``,
    ``list_sources_for_instance``, ``supersede_source``,
    ``latest_version_for_source``) are removed — their only
    callers were the legacy admin routes that Cleanup B also
    deleted.
  * ``search_similar`` now INNER JOINs ``knowledge_sources`` (the
    FK is NOT NULL, so the LEFT-join fallback for legacy chunks
    is no longer needed). Filters on
    ``ingestion_status = 'ready'`` and the source-side lifecycle
    flags (Architecture v1 §3.2 retrieval flow step 1).
  * ``soft_delete_chunks_for_source_id`` (renamed from
    ``_for_source_fk``) is the cascade helper Step 7's
    ``DELETE /sources/{id}`` route calls.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeChunk

logger = logging.getLogger(__name__)


class KnowledgeRepository:
    """CRUD over knowledge_chunks, scoped by LucielInstance + the
    new INTEGER source_id FK to ``knowledge_sources``."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ==============================================================
    # CREATE
    # ==============================================================

    def add_chunks(
        self,
        *,
        chunks: Sequence[str],
        embeddings: Sequence[Sequence[float]],
        admin_id: str | None,
        luciel_instance_id: int,
        knowledge_type: str,
        title: str | None,
        source_id: int,
        source_version: int,
        source_filename: str | None,
        source_type: str | None,
        ingested_by: str | None,
        created_by: str | None,
        autocommit: bool = True,
    ) -> list[KnowledgeChunk]:
        """Bulk-insert chunks with matching embeddings.

        ``chunks`` and ``embeddings`` must be the same length. Each
        chunk becomes one ``knowledge_chunks`` row. ``source_id``
        is the INTEGER FK to ``knowledge_sources.id`` (mandatory).
        """
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"chunks/embeddings length mismatch: "
                f"{len(chunks)} vs {len(embeddings)}"
            )
        if not chunks:
            return []

        rows: list[KnowledgeChunk] = []
        for text, emb in zip(chunks, embeddings):
            row = KnowledgeChunk(
                admin_id=admin_id,
                luciel_instance_id=luciel_instance_id,
                content=text,
                title=title,
                knowledge_type=knowledge_type,
                source_id=source_id,
                source_version=source_version,
                source_filename=source_filename,
                source_type=source_type,
                ingested_by=ingested_by,
                created_by=created_by,
            )
            # `embedding` is pgvector; bind via raw attr set.
            row.embedding = emb  # type: ignore[attr-defined]
            self.db.add(row)
            rows.append(row)

        if autocommit:
            self.db.commit()
            for row in rows:
                self.db.refresh(row)
        else:
            self.db.flush()
        return rows

    # ==============================================================
    # READ — single chunk
    # ==============================================================

    def get_chunk(self, chunk_id: int) -> KnowledgeChunk | None:
        """Fetch a single chunk by primary key. No scope filter —
        authorization happens at the route layer before this is
        called."""
        return self.db.get(KnowledgeChunk, chunk_id)

    # ==============================================================
    # READ — scope-wise (chunks for retriever's union inheritance)
    # ==============================================================

    def list_active_chunks_for_scope(
        self,
        *,
        admin_id: str | None,
        luciel_instance_id: int | None,
        knowledge_type: str | None = None,
        limit: int = 1000,
    ) -> list[KnowledgeChunk]:
        """Upward-inheritance read used by the retriever.

        Returns active chunks visible to this scope:
            - chunks bound to this luciel_instance_id  (instance-private)
            - chunks at this admin_id with luciel_instance_id IS NULL
              (tenant-shared)
            - chunks with both (admin_id, luciel_instance_id) NULL
              (global)

        Arc 12 EX3: the legacy ``domain_id`` leg is gone — v2 scopes
        knowledge by ``(admin_id, luciel_instance_id)`` only.
        """
        instance_clause = (
            KnowledgeChunk.luciel_instance_id == luciel_instance_id
            if luciel_instance_id is not None
            else None
        )
        tenant_clause = and_(
            KnowledgeChunk.luciel_instance_id.is_(None),
            KnowledgeChunk.admin_id == admin_id,
        ) if admin_id is not None else None
        global_clause = and_(
            KnowledgeChunk.luciel_instance_id.is_(None),
            KnowledgeChunk.admin_id.is_(None),
        )

        union_parts = [
            c for c in (instance_clause, tenant_clause, global_clause)
            if c is not None
        ]
        if not union_parts:
            return []

        stmt = (
            select(KnowledgeChunk)
            .where(
                or_(*union_parts),
                KnowledgeChunk.superseded_at.is_(None),
            )
            .order_by(KnowledgeChunk.id.asc())
            .limit(limit)
        )
        if knowledge_type is not None:
            stmt = stmt.where(KnowledgeChunk.knowledge_type == knowledge_type)
        return list(self.db.execute(stmt).scalars().all())

    # ==============================================================
    # DELETE — soft-delete cascade
    # ==============================================================

    def soft_delete_chunks_for_source_id(
        self,
        *,
        source_id: int,
        admin_id: str,
        autocommit: bool = False,
    ) -> int:
        """Stamp ``soft_deleted_at`` on every active chunk whose
        ``source_id`` matches. Mirrors the application-side cascade
        from ``KnowledgeSourceRepository.soft_delete``. The API
        handler for ``DELETE /sources/{id}`` calls both so the
        source row and its chunks transition together.

        Returns the number of rows newly stamped (already-soft-
        deleted rows are not double-counted; idempotent).
        """
        now = datetime.now(tz=timezone.utc)
        stmt = select(KnowledgeChunk).where(
            KnowledgeChunk.source_id == source_id,
            KnowledgeChunk.admin_id == admin_id,
            KnowledgeChunk.soft_deleted_at.is_(None),
        )
        rows = list(self.db.execute(stmt).scalars().all())
        for row in rows:
            row.soft_deleted_at = now
        if autocommit:
            self.db.commit()
        else:
            self.db.flush()
        return len(rows)

    # ==============================================================
    # READ — vector similarity (for retriever, chat path)
    # ==============================================================

    def search_similar(
        self,
        *,
        query_embedding: Sequence[float],
        admin_id: str | None,
        luciel_instance_id: int | None = None,
        knowledge_type: str | None = None,
        limit: int = 5,
    ) -> list[dict]:
        """Vector similarity search with upward inheritance.

        Visibility (union of):
          - luciel_instance_id == given  (instance-private)
          - luciel_instance_id IS NULL AND admin_id match (tenant-shared)
          - admin_id IS NULL  (global)

        Arc 12 EX3: the legacy ``domain_id`` leg is gone — v2 scopes
        knowledge by ``(admin_id, luciel_instance_id)`` only.

        Active-only (``superseded_at IS NULL``). Excludes lifecycle
        flagged rows: ``soft_deleted_at IS NULL`` and
        ``pending_downgrade_archived_at IS NULL``.

        Post-Cleanup-B: chunks now always have a non-NULL
        ``source_id`` FK pointing at a ``knowledge_sources`` row.
        The join is INNER (was LEFT OUTER pre-Cleanup-B to handle
        legacy chunks with no FK). Source-side lifecycle gates
        still apply: only chunks whose parent source is
        ``ingestion_status='ready'`` AND not soft-deleted AND not
        downgrade-archived are returned.

        Post-Cleanup-C: the legacy ``agent_id`` fan-out is removed
        (column dropped). The pre-Step-24.5 read-compat branch had
        zero production rows.

        Orders by cosine distance ascending (<=>).

        Returns a list of dicts with the per-chunk fields the
        retriever needs (id, content, title, knowledge_type, scope
        triple, distance, source_id, source_record_id,
        source_record_status). ``source_record_id`` and
        ``source_id`` collapse to the same value post-Cleanup-B;
        both are exposed so the retriever's RetrievedChunk
        construction can stay shape-compatible.
        """
        clauses: list = []
        if luciel_instance_id is not None:
            clauses.append(
                KnowledgeChunk.luciel_instance_id == luciel_instance_id
            )
        if admin_id is not None:
            clauses.append(
                and_(
                    KnowledgeChunk.luciel_instance_id.is_(None),
                    KnowledgeChunk.admin_id == admin_id,
                )
            )
        clauses.append(
            and_(
                KnowledgeChunk.luciel_instance_id.is_(None),
                KnowledgeChunk.admin_id.is_(None),
            )
        )

        from sqlalchemy import literal_column
        from sqlalchemy.orm import aliased

        from app.models.knowledge_source import KnowledgeSource

        emb_literal = "[" + ",".join(f"{float(x):.7f}" for x in query_embedding) + "]"
        distance_expr = literal_column(f"embedding <=> '{emb_literal}'::vector")

        ks = aliased(KnowledgeSource)

        stmt = (
            select(
                KnowledgeChunk.id,
                KnowledgeChunk.content,
                KnowledgeChunk.title,
                KnowledgeChunk.knowledge_type,
                KnowledgeChunk.luciel_instance_id,
                KnowledgeChunk.admin_id,
                KnowledgeChunk.source_id,
                ks.id.label("source_record_id"),
                ks.ingestion_status.label("source_record_status"),
                distance_expr.label("distance"),
            )
            # INNER JOIN — source_id is NOT NULL post-Cleanup-B.
            .join(ks, KnowledgeChunk.source_id == ks.id)
            .where(
                or_(*clauses),
                KnowledgeChunk.superseded_at.is_(None),
                KnowledgeChunk.soft_deleted_at.is_(None),
                KnowledgeChunk.pending_downgrade_archived_at.is_(None),
                # Source-side lifecycle gates.
                ks.ingestion_status == "ready",
                ks.soft_deleted_at.is_(None),
                ks.pending_downgrade_archived_at.is_(None),
            )
            .order_by(distance_expr.asc())
            .limit(limit)
        )
        if knowledge_type is not None:
            stmt = stmt.where(KnowledgeChunk.knowledge_type == knowledge_type)

        rows = self.db.execute(stmt).all()
        return [
            {
                "id": r.id,
                "content": r.content,
                "title": r.title,
                "knowledge_type": r.knowledge_type,
                "luciel_instance_id": r.luciel_instance_id,
                "admin_id": r.admin_id,
                "distance": float(r.distance) if r.distance is not None else None,
                "source_id": r.source_id,
                "source_record_id": r.source_record_id,
                "source_record_status": r.source_record_status,
            }
            for r in rows
        ]
