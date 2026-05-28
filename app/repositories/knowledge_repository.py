"""
Knowledge repository (Step 25b, File 8 — Arc 11 Step 3 update).

Scope-aware CRUD over the ``knowledge_chunks`` table (renamed from
``knowledge_embeddings`` in Arc 11 Step 2). As of Arc 11 Step 3 the
table also carries a ``source_fk`` column FK'd to
``knowledge_sources.id``; ``add_chunks`` accepts that FK as an
optional pass-through. New writes from ``IngestionService`` populate
both ``source_fk`` (the new canonical relation) AND the legacy
stringy ``source_id`` column so reads remain compatible during the
cutover. Step 11 drops the legacy string column and flips
``source_fk`` to NOT NULL.

Write discipline:
    - Writes always carry the full scope (admin_id, domain_id,
      luciel_instance_id) so retrieval inheritance works.
    - ``source_fk`` is the new authoritative source binding; the
      legacy stringy ``source_id`` is preserved during cutover.
    - ``source_id`` + ``source_version`` remain the atomic unit of
      "replace by source": supersede the active rows by their
      stringy ``source_id`` and insert the new version's rows next
      to them. Step 11 migrates this to grouping by ``source_fk``.
    - Soft supersede: never UPDATE-in-place, never DELETE rows. Set
      ``superseded_at`` on the old version, insert new rows.

Read discipline:
    - ``list_for_luciel_instance`` reads instance-scoped chunks only.
    - ``list_active_chunks_for_scope`` reads with upward inheritance
      (instance -> domain -> tenant -> global) — what the retriever
      uses for retrieval-time union.
    - Active-only filter (``superseded_at IS NULL``) on every read
      that serves chat or admin-list; admins can opt into
      ?include_superseded=true later if we need audit surface.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable, Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeChunk

logger = logging.getLogger(__name__)


# Arc 11 Step 2 backwards-compat: the class was renamed
# ``KnowledgeEmbedding`` -> ``KnowledgeChunk``. The legacy name still
# resolves at the model module level via the alias. Code within this
# repository now uses ``KnowledgeChunk`` exclusively.
KnowledgeEmbedding = KnowledgeChunk


class KnowledgeRepository:
    """CRUD over knowledge_chunks, scoped by LucielInstance + legacy triple."""

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
        source_fk: int | None = None,
        autocommit: bool = True,
    ) -> list[KnowledgeChunk]:
        """Bulk-insert chunks with matching embeddings.

        ``chunks`` and ``embeddings`` must be the same length. Each
        chunk becomes one ``knowledge_chunks`` row. Returns the
        persisted model instances (refreshed with DB-assigned id +
        timestamps).

        Arc 11 Step 3: ``source_fk`` is optional and defaults to
        ``None`` for callers that pre-date the two-table model.
        New writes from ``IngestionService`` pass the FK explicitly
        and *also* keep populating the legacy stringy ``source_id``
        for backward compatibility until Step 11.
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
                domain_id=domain_id,
                agent_id=agent_id,
                luciel_instance_id=luciel_instance_id,
                content=text,
                title=title,
                knowledge_type=knowledge_type,
                source=source,
                source_id=source_id,
                source_fk=source_fk,
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
    ) -> list[KnowledgeChunk]:
        """Return all active (non-superseded) chunks for one source_id
        on one Luciel instance. Empty list if not found.
        """
        stmt = (
            select(KnowledgeChunk)
            .where(
                KnowledgeChunk.luciel_instance_id == luciel_instance_id,
                KnowledgeChunk.source_id == source_id,
                KnowledgeChunk.superseded_at.is_(None),
            )
            .order_by(KnowledgeChunk.id.asc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_chunk(self, chunk_id: int) -> KnowledgeChunk | None:
        """Fetch a single chunk by primary key. No scope filter —
        authorization happens at the route layer (File 10) before this
        is called.
        """
        return self.db.get(KnowledgeChunk, chunk_id)

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
            KnowledgeChunk.luciel_instance_id == luciel_instance_id,
        ]
        if not include_superseded:
            base_filter.append(KnowledgeChunk.superseded_at.is_(None))

        # Aggregate per (source_id, source_version).
        group_cols = (
            KnowledgeChunk.luciel_instance_id,
            KnowledgeChunk.source_id,
            KnowledgeChunk.source_version,
            KnowledgeChunk.source_filename,
            KnowledgeChunk.source_type,
            KnowledgeChunk.knowledge_type,
            KnowledgeChunk.title,
            KnowledgeChunk.ingested_by,
        )
        count_stmt = (
            select(func.count(func.distinct(KnowledgeChunk.source_id)))
            .where(*base_filter, KnowledgeChunk.source_id.is_not(None))
        )
        total = int(self.db.execute(count_stmt).scalar() or 0)

        stmt = (
            select(
                *group_cols,
                func.count(KnowledgeChunk.id).label("chunk_count"),
                func.min(KnowledgeChunk.created_at).label("created_at"),
                func.max(KnowledgeChunk.superseded_at).label("superseded_at"),
            )
            .where(*base_filter, KnowledgeChunk.source_id.is_not(None))
            .group_by(*group_cols)
            .order_by(func.min(KnowledgeChunk.created_at).desc())
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
        admin_id: str | None,
        domain_id: str | None,
        luciel_instance_id: int | None,
        knowledge_type: str | None = None,
        limit: int = 1000,
    ) -> list[KnowledgeChunk]:
        """Upward-inheritance read used by File 11's retriever.

        Returns active chunks visible to this scope:
            - chunks bound to this luciel_instance_id  (instance-private)
            - chunks at this (admin_id, domain_id) with luciel_instance_id IS NULL
              (domain-shared)
            - chunks at this admin_id with domain_id IS NULL and
              luciel_instance_id IS NULL  (tenant-shared)
            - chunks with all of (admin_id, domain_id, luciel_instance_id) NULL
              (global / domain_knowledge across all tenants for a domain)

        Legacy rows (agent_id set, luciel_instance_id NULL) are read-
        compatible via the same clauses — they travel with the scope
        triple exactly as before, they just never update.
        """
        instance_clause = (
            KnowledgeChunk.luciel_instance_id == luciel_instance_id
            if luciel_instance_id is not None
            else None
        )
        domain_clause = and_(
            KnowledgeChunk.luciel_instance_id.is_(None),
            KnowledgeChunk.admin_id == admin_id,
            KnowledgeChunk.domain_id == domain_id,
        ) if domain_id is not None else None
        tenant_clause = and_(
            KnowledgeChunk.luciel_instance_id.is_(None),
            KnowledgeChunk.domain_id.is_(None),
            KnowledgeChunk.admin_id == admin_id,
        ) if admin_id is not None else None
        global_clause = and_(
            KnowledgeChunk.luciel_instance_id.is_(None),
            KnowledgeChunk.admin_id.is_(None),
            KnowledgeChunk.domain_id.is_(None),
        )

        union_parts = [
            c for c in (instance_clause, domain_clause, tenant_clause, global_clause)
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
    # REPLACE / DELETE — soft-supersede
    # ==============================================================

    def soft_delete_chunks_for_source_fk(
        self,
        *,
        source_fk: int,
        admin_id: str,
        autocommit: bool = False,
    ) -> int:
        """Stamp ``soft_deleted_at`` on every active chunk whose
        ``source_fk`` matches. Mirrors the application-side cascade
        from ``KnowledgeSourceRepository.soft_delete``.

        Why two cascade helpers (this one + the legacy-string one
        below): mid-cutover the chunk table has rows with NULL
        ``source_fk`` (pre-Arc-11 ingests) and rows with NULL legacy
        ``source_id`` (Step-11+ ingests). The API handler for
        ``DELETE /sources/{id}`` calls both so the cascade lands
        regardless of which era the chunks come from. After Step 11
        only the FK helper remains.

        Returns the number of rows newly stamped (already-soft-deleted
        rows are not double-counted; idempotent).
        """
        now = datetime.now(tz=timezone.utc)
        stmt = select(KnowledgeChunk).where(
            KnowledgeChunk.source_fk == source_fk,
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
            select(KnowledgeChunk)
            .where(
                KnowledgeChunk.luciel_instance_id == luciel_instance_id,
                KnowledgeChunk.source_id == source_id,
                KnowledgeChunk.superseded_at.is_(None),
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
        stmt = select(func.max(KnowledgeChunk.source_version)).where(
            KnowledgeChunk.luciel_instance_id == luciel_instance_id,
            KnowledgeChunk.source_id == source_id,
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
        admin_id: str | None,
        domain_id: str | None,
        luciel_instance_id: int | None = None,
        agent_id: str | None = None,  # legacy read-compat
        knowledge_type: str | None = None,
        limit: int = 5,
    ) -> list[dict]:
        """Vector similarity search with upward inheritance.

        Visibility (union of):
          - luciel_instance_id == given  (instance-private)
          - luciel_instance_id IS NULL AND admin_id+domain_id match  (domain-shared)
          - luciel_instance_id IS NULL AND domain_id IS NULL AND admin_id match (tenant-shared)
          - admin_id IS NULL AND domain_id IS NULL  (global)
          - Legacy compat: agent_id match (pre-Step-24.5 rows)

        Active-only (``superseded_at IS NULL``). Excludes lifecycle
        flagged rows: ``soft_deleted_at IS NULL`` (Arc 10) and
        ``pending_downgrade_archived_at IS NULL`` (Arc 10 5th axis).

        Arc 11 Step 3: also filters out chunks whose parent source
        is not in ``ingestion_status='ready'``. Chunks with NULL
        ``source_fk`` (pre-Arc-11 legacy rows, or paste-text writes
        on the legacy path) are included unconditionally — they
        have no source-row gate. Architecture v1 §3.2 retrieval
        flow step 1 ("Filter by admin_id, instance_id, and
        ingestion_status = 'ready'") is satisfied for the new-shape
        rows; legacy rows are grandfathered until Step 11.

        Orders by cosine distance ascending (<=>).

        Returns a list of dicts: ``{id, content, title,
        knowledge_type, luciel_instance_id, admin_id, domain_id,
        distance, source_fk, source_id, source_record_id,
        source_record_status}``. The last four are Arc 11 Step 3
        additions used by the retriever's ``source_identifier``
        property.
        """
        # Build visibility clauses — all require superseded_at IS NULL.
        clauses: list = []
        if luciel_instance_id is not None:
            clauses.append(
                KnowledgeChunk.luciel_instance_id == luciel_instance_id
            )
        if domain_id is not None and admin_id is not None:
            clauses.append(
                and_(
                    KnowledgeChunk.luciel_instance_id.is_(None),
                    KnowledgeChunk.admin_id == admin_id,
                    KnowledgeChunk.domain_id == domain_id,
                )
            )
        if admin_id is not None:
            clauses.append(
                and_(
                    KnowledgeChunk.luciel_instance_id.is_(None),
                    KnowledgeChunk.domain_id.is_(None),
                    KnowledgeChunk.admin_id == admin_id,
                )
            )
        # Global rows (no tenant).
        clauses.append(
            and_(
                KnowledgeChunk.luciel_instance_id.is_(None),
                KnowledgeChunk.admin_id.is_(None),
                KnowledgeChunk.domain_id.is_(None),
            )
        )
        # Legacy agent-scoped rows.
        if agent_id is not None:
            clauses.append(
                and_(
                    KnowledgeChunk.luciel_instance_id.is_(None),
                    KnowledgeChunk.admin_id == admin_id,
                    KnowledgeChunk.agent_id == agent_id,
                )
            )

        # Raw SQL for pgvector similarity — SQLAlchemy core does not
        # know the `vector` type, so we bind parameters and pass the
        # embedding as a literal string in pgvector's expected format.
        from sqlalchemy import literal_column
        from sqlalchemy.orm import aliased

        from app.models.knowledge_source import KnowledgeSource

        emb_literal = "[" + ",".join(f"{float(x):.7f}" for x in query_embedding) + "]"
        distance_expr = literal_column(f"embedding <=> '{emb_literal}'::vector")

        # Arc 11 Step 3: LEFT OUTER JOIN onto knowledge_sources so we
        # can read the source row's ingestion_status in the same
        # round-trip (no N+1) and gate the result set on it. LEFT
        # join (not INNER) because legacy chunks have NULL source_fk
        # — those rows must still be returned.
        ks = aliased(KnowledgeSource)

        stmt = (
            select(
                KnowledgeChunk.id,
                KnowledgeChunk.content,
                KnowledgeChunk.title,
                KnowledgeChunk.knowledge_type,
                KnowledgeChunk.luciel_instance_id,
                KnowledgeChunk.admin_id,
                KnowledgeChunk.domain_id,
                KnowledgeChunk.source_fk,
                KnowledgeChunk.source_id.label("legacy_source_id"),
                ks.id.label("source_record_id"),
                ks.ingestion_status.label("source_record_status"),
                distance_expr.label("distance"),
            )
            .outerjoin(ks, KnowledgeChunk.source_fk == ks.id)
            .where(
                or_(*clauses),
                KnowledgeChunk.superseded_at.is_(None),
                KnowledgeChunk.soft_deleted_at.is_(None),
                KnowledgeChunk.pending_downgrade_archived_at.is_(None),
                # Source-gate: either no source row (legacy) OR source
                # ready AND source not soft-deleted/archived.
                or_(
                    KnowledgeChunk.source_fk.is_(None),
                    and_(
                        ks.ingestion_status == "ready",
                        ks.soft_deleted_at.is_(None),
                        ks.pending_downgrade_archived_at.is_(None),
                    ),
                ),
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
                "domain_id": r.domain_id,
                "distance": float(r.distance) if r.distance is not None else None,
                "source_fk": r.source_fk,
                "source_id": r.legacy_source_id,
                "source_record_id": r.source_record_id,
                "source_record_status": r.source_record_status,
            }
            for r in rows
        ]