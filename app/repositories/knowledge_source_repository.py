"""KnowledgeSource repository — Arc 11 Step 3.

Scope-aware CRUD over the ``knowledge_sources`` table introduced in
Arc 11 Step 1. One row per uploaded / pasted / crawled source;
provenance, ingestion-lifecycle, version, soft-delete and downgrade-
archive bookkeeping all live here.

Three-layer defence (Architecture v1 §3.7.1) discipline:
  * L1 — every read filters by ``admin_id`` in Python even though
    the L2 RLS policy (added in Step 4) also enforces it. Belt +
    braces; the L1 filter is the audit-trail.
  * L2 — RLS on ``knowledge_sources`` is added in Step 4. Until
    then this repository's L1 filter is the only enforcement.
  * L3 — the SessionLocal connection-pool wrapper (Arc 9
    ``app/db/tenant_context.py``) sets ``app.admin_id`` before
    every BEGIN. This file does not need to know about it; it
    only needs to add the L1 ``WHERE admin_id = :x`` filter.

Soft-delete posture (Architecture §3.6.1):
  * Every list / get filters ``soft_deleted_at IS NULL`` by
    default. Callers that need the archived rows opt in via
    ``include_soft_deleted=True``.

What this repository does NOT do:
  * Quota enforcement — Step 7 owns that at the API boundary
    (Architecture §3.2).
  * Cascade chunks' ``soft_deleted_at`` — application-side cascade
    lives in ``IngestionService`` / ``KnowledgeRepository`` so
    that the chunk-side soft-delete worker (Arc 10) keeps owning
    that lifecycle column.
  * RLS — see L2 above.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.knowledge_source import KnowledgeSource

logger = logging.getLogger(__name__)


_VALID_STATUSES = frozenset({"pending", "processing", "ready", "failed"})


class KnowledgeSourceRepository:
    """CRUD over knowledge_sources, scoped by (admin_id, instance_id)."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ==============================================================
    # CREATE
    # ==============================================================

    def create_source(
        self,
        *,
        admin_id: str,
        luciel_instance_id: int,
        source_type: str,
        size_bytes: int,
        ingested_by: str,
        filename: str | None = None,
        s3_key: str | None = None,
        origin_url: str | None = None,
        ingestion_status: str = "pending",
        autocommit: bool = False,
    ) -> KnowledgeSource:
        """Insert a fresh source row. Returns the persisted instance.

        The default ``ingestion_status='pending'`` matches the embed-
        worker contract (Step 6): the API boundary writes the source
        row, then enqueues a Celery task that flips status to
        ``processing`` -> ``ready`` (or ``failed`` on error).

        ``size_bytes`` is computed by the caller from the upload
        (Content-Length for HTTP uploads, ``len(text.encode("utf-8"))``
        for paste-text). The repository does not infer it.
        """
        if ingestion_status not in _VALID_STATUSES:
            raise ValueError(
                f"ingestion_status must be one of {sorted(_VALID_STATUSES)}, "
                f"got {ingestion_status!r}"
            )
        row = KnowledgeSource(
            admin_id=admin_id,
            luciel_instance_id=luciel_instance_id,
            filename=filename,
            source_type=source_type,
            size_bytes=size_bytes,
            s3_key=s3_key,
            origin_url=origin_url,
            ingestion_status=ingestion_status,
            ingested_by=ingested_by,
        )
        self.db.add(row)
        if autocommit:
            self.db.commit()
            self.db.refresh(row)
        else:
            self.db.flush()
        return row

    # ==============================================================
    # READ
    # ==============================================================

    def get_source(
        self,
        source_id: int,
        *,
        admin_id: str,
        include_soft_deleted: bool = False,
    ) -> KnowledgeSource | None:
        """Fetch one source by id, scoped to ``admin_id``.

        Returns ``None`` if the row does not exist, belongs to another
        admin (per the L1 filter), or is soft-deleted (unless
        ``include_soft_deleted=True``).
        """
        stmt = select(KnowledgeSource).where(
            KnowledgeSource.id == source_id,
            KnowledgeSource.admin_id == admin_id,
        )
        if not include_soft_deleted:
            stmt = stmt.where(KnowledgeSource.soft_deleted_at.is_(None))
        return self.db.execute(stmt).scalar_one_or_none()

    def list_sources_for_instance(
        self,
        *,
        admin_id: str,
        luciel_instance_id: int,
        include_soft_deleted: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[KnowledgeSource]:
        """List sources for one (admin, instance), newest first."""
        stmt = (
            select(KnowledgeSource)
            .where(
                KnowledgeSource.admin_id == admin_id,
                KnowledgeSource.luciel_instance_id == luciel_instance_id,
            )
            .order_by(KnowledgeSource.ingested_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if not include_soft_deleted:
            stmt = stmt.where(KnowledgeSource.soft_deleted_at.is_(None))
        return list(self.db.execute(stmt).scalars().all())

    # ==============================================================
    # UPDATE — lifecycle stamps
    # ==============================================================

    def mark_status(
        self,
        source_id: int,
        *,
        admin_id: str,
        status: str,
        error: str | None = None,
        error_code: str | None = None,
        autocommit: bool = False,
    ) -> KnowledgeSource:
        """Move a source through its ingestion lifecycle.

        Valid status transitions are not policed here — that lives in
        the embed-worker (Step 6), which is the only writer that
        legitimately drives the full state machine. This method just
        enforces the enum domain.

        On ``status='failed'`` the caller is expected to pass
        ``error`` so the failure surfaces in the admin UI, and
        optionally ``error_code`` (canonical values in
        ``app.models.knowledge_source_errors.IngestionErrorCode``)
        for the machine-readable cross-repo contract. On any
        non-failed status both fields are cleared (a retry that
        succeeds wipes the prior failure note).
        """
        if status not in _VALID_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(_VALID_STATUSES)}, "
                f"got {status!r}"
            )
        row = self.get_source(
            source_id, admin_id=admin_id, include_soft_deleted=True,
        )
        if row is None:
            raise KnowledgeSourceNotFound(
                f"knowledge_sources.id={source_id} not visible to "
                f"admin_id={admin_id!r}"
            )
        row.ingestion_status = status
        row.ingestion_error = error if status == "failed" else None
        row.ingestion_error_code = error_code if status == "failed" else None
        row.updated_at = datetime.now(tz=timezone.utc)
        if autocommit:
            self.db.commit()
            self.db.refresh(row)
        else:
            self.db.flush()
        return row

    def soft_delete(
        self,
        source_id: int,
        *,
        admin_id: str,
        autocommit: bool = False,
    ) -> KnowledgeSource:
        """Stamp ``soft_deleted_at`` so the 30-day window starts.

        Idempotent: a second call within the same window is a no-op
        (the existing stamp is preserved). The chunk-side cascade
        — stamping ``soft_deleted_at`` on every chunk with this
        ``source_id`` FK — lives in ``IngestionService`` / the API
        handler so the chunks' lifecycle column keeps moving with
        the worker that already owns it (Arc 10 soft-delete worker).
        """
        row = self.get_source(
            source_id, admin_id=admin_id, include_soft_deleted=True,
        )
        if row is None:
            raise KnowledgeSourceNotFound(
                f"knowledge_sources.id={source_id} not visible to "
                f"admin_id={admin_id!r}"
            )
        if row.soft_deleted_at is None:
            row.soft_deleted_at = datetime.now(tz=timezone.utc)
            row.updated_at = row.soft_deleted_at
        if autocommit:
            self.db.commit()
            self.db.refresh(row)
        else:
            self.db.flush()
        return row

    def rename(
        self,
        source_id: int,
        *,
        admin_id: str,
        new_filename: str,
        autocommit: bool = False,
    ) -> KnowledgeSource:
        """Change ``filename``. The version is NOT bumped (a rename is
        cosmetic; chunks remain the same content)."""
        row = self.get_source(source_id, admin_id=admin_id)
        if row is None:
            raise KnowledgeSourceNotFound(
                f"knowledge_sources.id={source_id} not visible to "
                f"admin_id={admin_id!r}"
            )
        row.filename = new_filename
        row.updated_at = datetime.now(tz=timezone.utc)
        if autocommit:
            self.db.commit()
            self.db.refresh(row)
        else:
            self.db.flush()
        return row

    def bump_version(
        self,
        source_id: int,
        *,
        admin_id: str,
        autocommit: bool = False,
    ) -> KnowledgeSource:
        """Increment ``source_version`` by 1 and reset status to
        ``processing``. Called on re-ingest after the prior version's
        chunks have been superseded by the chunk repository."""
        row = self.get_source(source_id, admin_id=admin_id)
        if row is None:
            raise KnowledgeSourceNotFound(
                f"knowledge_sources.id={source_id} not visible to "
                f"admin_id={admin_id!r}"
            )
        row.source_version = (row.source_version or 1) + 1
        row.ingestion_status = "processing"
        row.ingestion_error = None
        row.updated_at = datetime.now(tz=timezone.utc)
        if autocommit:
            self.db.commit()
            self.db.refresh(row)
        else:
            self.db.flush()
        return row

    def touch_last_viewed(
        self,
        source_ids: Sequence[int],
        *,
        admin_id: str,
        autocommit: bool = False,
    ) -> None:
        """Update ``last_viewed_at`` for a batch of sources.

        Called by the raw-view list endpoint (Step 7) each time the
        admin UI surfaces these rows. No-ops on empty input.
        """
        if not source_ids:
            return
        now = datetime.now(tz=timezone.utc)
        self.db.execute(
            update(KnowledgeSource)
            .where(
                KnowledgeSource.id.in_(list(source_ids)),
                KnowledgeSource.admin_id == admin_id,
            )
            .values(last_viewed_at=now)
        )
        if autocommit:
            self.db.commit()
        else:
            self.db.flush()


class KnowledgeSourceNotFound(LookupError):
    """Raised when a source lookup hits no row visible to the caller."""
