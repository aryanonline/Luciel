"""
Knowledge repository (Step 25b, File 8).

Scope-aware CRUD over knowledge_embeddings.

Write discipline:
    - Writes always carry the full scope (tenant_id, domain_id,
      luciel_instance_id) so retrieval inheritance works.
    - source_id + source_version are atomic units of replace.
    - Soft supersede: never UPDATE-in-place, never DELETE rows. Set
      superseded_at, insert new version. Preserves audit trail.

Read discipline:
    - list_for_luciel_instance reads instance-scoped chunks only.
    - list_for_scope reads with upward inheritance
      (instance -> domain -> tenant -> global) — what File 11's
      retriever will use for retrieval-time union.
    - Active-only filter (superseded_at IS NULL) on every read that
      serves chat or admin-list; admins can opt into ?include_superseded=true
      later if we need audit surface.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable, Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeEmbedding

logger = logging.getLogger(__name__)


class KnowledgeRepository:
    """CRUD over knowledge_embeddings, scoped by LucielInstance + legacy triple."""

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
        tenant_id: str | None,
        domain_id: str | None,
        agent_id: str | None,
        luciel_instance_id: int | None,
        knowledge_type: str,
        title: str | None,
        source: str | None,
        source_id: str | None,
        source_version: int,
        source_filename: str | None,
        source_type: str | None,
        ingested_by: str | None,
        created_by: str | None,
        autocommit: bool = True,
    ) -> list[KnowledgeEmbedding]:
        """Bulk-insert chunks with matching embeddings.

        `chunks` and `embeddings` must be the same length. Each chunk
        becomes one knowledge_embeddings row. Returns the persisted
        model instances (refreshed with DB-assigned id + timestamps).
        """
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"chunks/embeddings length mismatch: "
                f"{len(chunks)} vs {len(embeddings)}"
            )
        if not chunks:
            return []

        rows: list[KnowledgeEmbedding] = []
        for text, emb in zip(chunks, embeddings):
            row = KnowledgeEmbedding(
                tenant_id=tenant_id,
                domain_id=domain_id,
                agent_id=agent_id,
                luciel_instance_id=luciel_instance_id,
                content=text,
                title=title,
                knowledge_type=knowledge_type,
                source=source,
                source_id=source_id,
                source_version=source_version,
                source_filename=source_filename,
                source_type=source_type,
                ingested_by=ingested_by,
                created_by=created_by,
            )
            # `embedding` column is pgvector; bind via raw attr set.
            # SQLAlchemy doesn't type-check vector(1536).
            row.embedding = emb  # type: ignore[attr-defined]
            self.db.add(row)
            rows.append(row)

        if autocommit:
            self.db.commit()
            for row in rows:
                self.db.refresh(row)
        else:
            self.db.flush()  # assign ids without committing
        return rows

    # ==============================================================
    # READ — single source, single chunk
    # ==============================================================

    def get_active_source(
        self,
        *,
        luciel_instance_id: int,
        source_id: str,
    ) -> list[KnowledgeEmbedding]:
        """Return all active (non-superseded) chunks for one source_id
        on one Luciel instance. Empty list if not found.
        """
        stmt = (
            select(KnowledgeEmbedding)
            .where(
                KnowledgeEmbedding.luciel_instance_id == luciel_instance_id,
                KnowledgeEmbedding.source_id == source_id,
                KnowledgeEmbedding.superseded_at.is_(None),
            )
            .order_by(KnowledgeEmbedding.id.asc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_chunk(self, chunk_id: int) -> KnowledgeEmbedding | None:
        """Fetch a single chunk by primary key. No scope filter —
        authorization happens at the route layer (File 10) before this
        is called.
        """
        return self.db.get(KnowledgeEmbedding, chunk_id)

    # ==============================================================
    # READ — lists
    # ==============================================================

    def list_sources_for_instance(
        self,
        *,
        luciel_instance_id: int,
        include_superseded: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """List distinct sources on one instance, grouped by source_id.

        Returns (items, total_count). Each item is a dict ready to
        feed KnowledgeSourceRead — {luciel_instance_id, source_id,
        source_version, source_filename, source_type, knowledge_type,
        title, chunk_count, ingested_by, created_at, superseded_at}.
        """
        base_filter = [
            KnowledgeEmbedding.luciel_instance_id == luciel_instance_id,
        ]
        if not include_superseded:
            base_filter.append(KnowledgeEmbedding.superseded_at.is_(None))

        # Aggregate per (source_id, source_version).
        group_cols = (
            KnowledgeEmbedding.luciel_instance_id,
            KnowledgeEmbedding.source_id,
            KnowledgeEmbedding.source_version,
            KnowledgeEmbedding.source_filename,
            KnowledgeEmbedding.source_type,
            KnowledgeEmbedding.knowledge_type,
            KnowledgeEmbedding.title,
            KnowledgeEmbedding.ingested_by,
        )
        count_stmt = (
            select(func.count(func.distinct(KnowledgeEmbedding.source_id)))
            .where(*base_filter, KnowledgeEmbedding.source_id.is_not(None))
        )
        total = int(self.db.execute(count_stmt).scalar() or 0)

        stmt = (
            select(
                *group_cols,
                func.count(KnowledgeEmbedding.id).label("chunk_count"),
                func.min(KnowledgeEmbedding.created_at).label("created_at"),
                func.max(KnowledgeEmbedding.superseded_at).label("superseded_at"),
            )
            .where(*base_filter, KnowledgeEmbedding.source_id.is_not(None))
            .group_by(*group_cols)
            .order_by(func.min(KnowledgeEmbedding.created_at).desc())
            .limit(limit)
            .offset(offset)
        )
        items = [
            {
                "luciel_instance_id": row.luciel_instance_id,
                "source_id": row.source_id,
                "source_version": row.source_version,
                "source_filename": row.source_filename,
                "source_type": row.source_type,
                "knowledge_type": row.knowledge_type,
                "title": row.title,
                "chunk_count": row.chunk_count,
                "ingested_by": row.ingested_by,
                "created_at": row.created_at,
                "superseded_at": row.superseded_at,
            }
            for row in self.db.execute(stmt).all()
        ]
        return items, total

    def list_active_chunks_for_scope(
        self,
        *,
        tenant_id: str | None,
        domain_id: str | None,
        luciel_instance_id: int | None,
        knowledge_type: str | None = None,
        limit: int = 1000,
    ) -> list[KnowledgeEmbedding]:
        """Upward-inheritance read used by File 11's retriever.

        Returns active chunks visible to this scope:
            - chunks bound to this luciel_instance_id  (instance-private)
            - chunks at this (tenant_id, domain_id) with luciel_instance_id IS NULL
              (domain-shared)
            - chunks at this tenant_id with domain_id IS NULL and
              luciel_instance_id IS NULL  (tenant-shared)
            - chunks with all of (tenant_id, domain_id, luciel_instance_id) NULL
              (global / domain_knowledge across all tenants for a domain)

        Legacy rows (agent_id set, luciel_instance_id NULL) are read-
        compatible via the same clauses — they travel with the scope
        triple exactly as before, they just never update.
        """
        instance_clause = (
            KnowledgeEmbedding.luciel_instance_id == luciel_instance_id
            if luciel_instance_id is not None
            else None
        )
        domain_clause = and_(
            KnowledgeEmbedding.luciel_instance_id.is_(None),
            KnowledgeEmbedding.tenant_id == tenant_id,
            KnowledgeEmbedding.domain_id == domain_id,
        ) if domain_id is not None else None
        tenant_clause = and_(
            KnowledgeEmbedding.luciel_instance_id.is_(None),
            KnowledgeEmbedding.domain_id.is_(None),
            KnowledgeEmbedding.tenant_id == tenant_id,
        ) if tenant_id is not None else None
        global_clause = and_(
            KnowledgeEmbedding.luciel_instance_id.is_(None),
            KnowledgeEmbedding.tenant_id.is_(None),
            KnowledgeEmbedding.domain_id.is_(None),
        )

        union_parts = [
            c for c in (instance_clause, domain_clause, tenant_clause, global_clause)
            if c is not None
        ]
        if not union_parts:
            return []

        stmt = (
            select(KnowledgeEmbedding)
            .where(
                or_(*union_parts),
                KnowledgeEmbedding.superseded_at.is_(None),
            )
            .order_by(KnowledgeEmbedding.id.asc())
            .limit(limit)
        )
        if knowledge_type is not None:
            stmt = stmt.where(KnowledgeEmbedding.knowledge_type == knowledge_type)
        return list(self.db.execute(stmt).scalars().all())

    # ==============================================================
    # REPLACE / DELETE — soft-supersede
    # ==============================================================

    def supersede_source(
        self,
        *,
        luciel_instance_id: int,
        source_id: str,
        autocommit: bool = True,
    ) -> int:
        """Mark every active chunk of (luciel_instance_id, source_id) as
        superseded.

        Returns the number of rows flipped from active to superseded.
        Idempotent: calling twice is safe (second call returns 0).
        """
        now = datetime.now(tz=timezone.utc)
        stmt = (
            select(KnowledgeEmbedding)
            .where(
                KnowledgeEmbedding.luciel_instance_id == luciel_instance_id,
                KnowledgeEmbedding.source_id == source_id,
                KnowledgeEmbedding.superseded_at.is_(None),
            )
        )
        rows = list(self.db.execute(stmt).scalars().all())
        for row in rows:
            row.superseded_at = now
        if autocommit:
            self.db.commit()
        else:
            self.db.flush()
        return len(rows)

    def latest_version_for_source(
        self,
        *,
        luciel_instance_id: int,
        source_id: str,
    ) -> int:
        """Return the highest source_version seen (active or superseded)
        for this (instance, source_id). 0 if no rows exist.

        File 9's ingest_file uses this to compute next_version = this + 1
        when replace_existing=True.
        """
        stmt = select(func.max(KnowledgeEmbedding.source_version)).where(
            KnowledgeEmbedding.luciel_instance_id == luciel_instance_id,
            KnowledgeEmbedding.source_id == source_id,
        )
        result = self.db.execute(stmt).scalar()
        return int(result or 0)
    # ==============================================================
    # READ — vector similarity (for retriever, chat path)
    # ==============================================================

    def search_similar(
        self,
        *,
        query_embedding: Sequence[float],
        tenant_id: str | None,
        domain_id: str | None,
        luciel_instance_id: int | None = None,
        agent_id: str | None = None,  # legacy read-compat
        knowledge_type: str | None = None,
        limit: int = 5,
    ) -> list[dict]:
        """Vector similarity search with upward inheritance.

        Visibility (union of):
          - luciel_instance_id == given  (instance-private)
          - luciel_instance_id IS NULL AND tenant_id+domain_id match  (domain-shared)
          - luciel_instance_id IS NULL AND domain_id IS NULL AND tenant_id match (tenant-shared)
          - tenant_id IS NULL AND domain_id IS NULL  (global)
          - Legacy compat: agent_id match (pre-Step-24.5 rows)

        Active-only (superseded_at IS NULL).
        Orders by cosine distance ascending (<=>).

        Returns a list of dicts: {id, content, title, knowledge_type,
        luciel_instance_id, tenant_id, domain_id, distance}.
        """
        # Build visibility clauses — all require superseded_at IS NULL.
        clauses: list = []
        if luciel_instance_id is not None:
            clauses.append(
                KnowledgeEmbedding.luciel_instance_id == luciel_instance_id
            )
        if domain_id is not None and tenant_id is not None:
            clauses.append(
                and_(
                    KnowledgeEmbedding.luciel_instance_id.is_(None),
                    KnowledgeEmbedding.tenant_id == tenant_id,
                    KnowledgeEmbedding.domain_id == domain_id,
                )
            )
        if tenant_id is not None:
            clauses.append(
                and_(
                    KnowledgeEmbedding.luciel_instance_id.is_(None),
                    KnowledgeEmbedding.domain_id.is_(None),
                    KnowledgeEmbedding.tenant_id == tenant_id,
                )
            )
        # Global rows (no tenant).
        clauses.append(
            and_(
                KnowledgeEmbedding.luciel_instance_id.is_(None),
                KnowledgeEmbedding.tenant_id.is_(None),
                KnowledgeEmbedding.domain_id.is_(None),
            )
        )
        # Legacy agent-scoped rows.
        if agent_id is not None:
            clauses.append(
                and_(
                    KnowledgeEmbedding.luciel_instance_id.is_(None),
                    KnowledgeEmbedding.tenant_id == tenant_id,
                    KnowledgeEmbedding.agent_id == agent_id,
                )
            )

        # Raw SQL for pgvector similarity — SQLAlchemy core does not
        # know the `vector` type, so we bind parameters and pass the
        # embedding as a literal string in pgvector's expected format.
        from sqlalchemy import literal_column, text

        emb_literal = "[" + ",".join(f"{float(x):.7f}" for x in query_embedding) + "]"
        distance_expr = literal_column(f"embedding <=> '{emb_literal}'::vector")

        stmt = (
            select(
                KnowledgeEmbedding.id,
                KnowledgeEmbedding.content,
                KnowledgeEmbedding.title,
                KnowledgeEmbedding.knowledge_type,
                KnowledgeEmbedding.luciel_instance_id,
                KnowledgeEmbedding.tenant_id,
                KnowledgeEmbedding.domain_id,
                distance_expr.label("distance"),
            )
            .where(
                or_(*clauses),
                KnowledgeEmbedding.superseded_at.is_(None),
            )
            .order_by(distance_expr.asc())
            .limit(limit)
        )
        if knowledge_type is not None:
            stmt = stmt.where(KnowledgeEmbedding.knowledge_type == knowledge_type)

        rows = self.db.execute(stmt).all()
        return [
            {
                "id": r.id,
                "content": r.content,
                "title": r.title,
                "knowledge_type": r.knowledge_type,
                "luciel_instance_id": r.luciel_instance_id,
                "tenant_id": r.tenant_id,
                "domain_id": r.domain_id,
                "distance": float(r.distance) if r.distance is not None else None,
            }
            for r in rows
        ]