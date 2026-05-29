"""
Knowledge ingestion service — post-Cleanup-B.

Orchestrates the synchronous parse + chunk + embed + persist
pipeline used by the embed-worker (Step 6) and by callers that
need an in-process ingest (test fixtures, the chunking-config
diagnostic). Step 7's API uploads bytes to S3 and enqueues
``embed_source`` directly — that worker reimplements this flow
inline; it does not call ``IngestionService.ingest_text`` /
``ingest_file``. Both surfaces share the same source-row
contract:

    bytes / text
        -> detect source_type (from filename suffix, or caller-supplied)
        -> Parser.parse()
        -> resolve EffectiveChunkingConfig
        -> chunk_text()
        -> KnowledgeSourceRepository.create_source()
        -> embed_texts()
        -> KnowledgeRepository.add_chunks(source_id=<int FK>)
        -> KnowledgeSourceRepository.mark_status('ready' | 'failed')

Post-Cleanup-B contract:
    * ``admin_id`` AND ``luciel_instance_id`` are MANDATORY. The
      pre-Cleanup-B "global / shared knowledge" path (admin_id
      None, instance None, write chunks with NULL FK) is gone —
      ``knowledge_chunks.source_id`` is NOT NULL and the source
      table requires both columns. The legacy
      ``IngestionService.ingest()`` shim (one-arg form for
      pre-Step-25b callers) is removed; its only consumer was
      the deleted ``POST /admin/knowledge/ingest`` route.
    * Versioning lives at the source-row level. Re-ingest is
      handled by Step 7's PATCH route (``bump_version`` on the
      source row); ``IngestionService`` itself no longer
      branches on a re-ingest flag.
    * No ``source_id`` String parameter — the column is gone. The
      ``IngestResult`` ``source_id`` field is the INTEGER FK to
      ``knowledge_sources.id``.

Scope binding:
    Authorization happens upstream in the admin route via
    ``ScopePolicy.enforce_admin_owns_instance``. This service does
    NOT re-check scope.

Quota:
    Quota enforcement is NOT done here — Step 7 enforces it at the
    API boundary per Architecture v1 §3.2.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy.orm import Session

from app.knowledge.chunker import (
    EffectiveChunkingConfig,
    chunk_text,
    resolve_effective_config,
)
from app.knowledge.embedder import embed_texts
from app.knowledge.parsers import (
    ParserError,
    UnsupportedSourceType,
    detect_source_type,
    get_parser,
)
from app.models.admin import Admin
from app.models.instance import Instance
from app.models.knowledge_source import KnowledgeSource

# Legacy names kept as aliases during the transition.
TenantConfig = Admin
LucielInstance = Instance
from app.repositories.knowledge_repository import KnowledgeRepository
from app.repositories.knowledge_source_repository import (
    KnowledgeSourceRepository,
)
from app.schemas.knowledge import KNOWLEDGE_TYPES

logger = logging.getLogger(__name__)


class IngestionError(Exception):
    """Raised when an ingest cannot be completed for a semantic
    reason. Distinct from ParserError (about un-parseable bytes);
    IngestionError is about the ingest request itself being
    invalid or conflicting."""


@dataclass(frozen=True)
class IngestResult:
    """Return shape of every successful ingest call."""
    luciel_instance_id: int
    source_id: int
    """INTEGER FK to ``knowledge_sources.id`` (post-Cleanup-B).
    Cleanup A wrote this as a synthesised ``f"src-{pk}"`` String;
    Cleanup B drops the legacy column and this field becomes the
    int FK directly."""
    source_version: int
    source_type: str | None
    source_filename: str | None
    knowledge_type: str
    chunk_count: int
    effective_config: EffectiveChunkingConfig


class IngestionService:
    """Knowledge ingestion orchestrator."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repository = KnowledgeRepository(db)
        self.source_repository = KnowledgeSourceRepository(db)

    # ==============================================================
    # Public entry points
    # ==============================================================

    def ingest_file(
        self,
        *,
        file_bytes: bytes,
        filename: str,
        admin_id: str,
        luciel_instance_id: int,
        knowledge_type: str = "luciel_knowledge",
        title: str | None = None,
        source_type: str | None = None,
        ingested_by: str | None = None,
        created_by: str | None = None,
    ) -> IngestResult:
        """Ingest raw file bytes. Detects source_type from filename
        unless explicitly supplied."""
        self._validate_knowledge_type(knowledge_type)

        if source_type is None:
            try:
                source_type = detect_source_type(filename)
            except UnsupportedSourceType as exc:
                raise IngestionError(str(exc)) from exc

        try:
            parser = get_parser(source_type)
        except UnsupportedSourceType as exc:
            raise IngestionError(str(exc)) from exc
        try:
            parsed = parser.parse(file_bytes, filename)
        except ParserError as exc:
            raise IngestionError(
                f"Parse failed for {filename!r}: {exc}"
            ) from exc

        return self._ingest_text(
            text=parsed.text,
            admin_id=admin_id,
            luciel_instance_id=luciel_instance_id,
            knowledge_type=knowledge_type,
            title=title,
            source_filename=filename,
            source_type=source_type,
            ingested_by=ingested_by,
            created_by=created_by,
            size_bytes=len(file_bytes),
        )

    def ingest_text(
        self,
        *,
        content: str,
        admin_id: str,
        luciel_instance_id: int,
        knowledge_type: str = "luciel_knowledge",
        title: str | None = None,
        source_filename: str | None = None,
        ingested_by: str | None = None,
        created_by: str | None = None,
    ) -> IngestResult:
        """Ingest already-extracted text (no file parsing)."""
        self._validate_knowledge_type(knowledge_type)
        return self._ingest_text(
            text=content,
            admin_id=admin_id,
            luciel_instance_id=luciel_instance_id,
            knowledge_type=knowledge_type,
            title=title,
            source_filename=source_filename,
            source_type=None,
            ingested_by=ingested_by,
            created_by=created_by,
            size_bytes=len(content.encode("utf-8")),
        )

    # ==============================================================
    # Shared internal path
    # ==============================================================

    def _ingest_text(
        self,
        *,
        text: str,
        admin_id: str,
        luciel_instance_id: int,
        knowledge_type: str,
        title: str | None,
        source_filename: str | None,
        source_type: str | None,
        ingested_by: str | None,
        created_by: str | None,
        size_bytes: int,
    ) -> IngestResult:
        if not text or not text.strip():
            raise IngestionError("Ingested content is empty after parsing")
        if not admin_id:
            raise IngestionError(
                "admin_id is required (Cleanup B: legacy global/shared "
                "ingests are no longer supported)"
            )
        if luciel_instance_id is None:
            raise IngestionError(
                "luciel_instance_id is required (Cleanup B: legacy "
                "global/shared ingests are no longer supported)"
            )

        # 1. Chunking config.
        cfg = self._resolve_chunking_config(
            admin_id=admin_id,
            luciel_instance_id=luciel_instance_id,
        )
        logger.info(
            "Ingest: instance=%s tenant=%s strategy=%s size=%s "
            "overlap=%s size_bytes=%s",
            luciel_instance_id, admin_id,
            cfg.chunk_strategy, cfg.chunk_size, cfg.chunk_overlap,
            size_bytes,
        )

        # 2. Chunk.
        chunks = chunk_text(text, cfg)
        if not chunks:
            raise IngestionError(
                "Chunker produced no chunks from non-empty input"
            )

        # 3. Source row (always created — post-Cleanup-B there's no
        # "skip the source row" branch).
        source_row = self.source_repository.create_source(
            admin_id=admin_id,
            luciel_instance_id=luciel_instance_id,
            filename=source_filename,
            source_type=source_type or "txt",
            size_bytes=size_bytes,
            ingested_by=ingested_by or created_by or "unknown",
            ingestion_status="processing",
        )

        # 4. Embed.
        try:
            embeddings = embed_texts(chunks)
        except Exception as exc:
            self._mark_source_failed(source_row, admin_id, str(exc))
            raise IngestionError(f"Embedding failed: {exc}") from exc
        if len(embeddings) != len(chunks):
            self._mark_source_failed(
                source_row, admin_id,
                f"Embedder returned {len(embeddings)} vectors for "
                f"{len(chunks)} chunks",
            )
            raise IngestionError(
                f"Embedder returned {len(embeddings)} vectors for "
                f"{len(chunks)} chunks"
            )

        # 5. Persist chunks with the INTEGER FK source_id.
        try:
            self.repository.add_chunks(
                chunks=chunks,
                embeddings=embeddings,
                admin_id=admin_id,
                luciel_instance_id=luciel_instance_id,
                knowledge_type=knowledge_type,
                title=title,
                source_id=source_row.id,
                source_version=source_row.source_version or 1,
                source_filename=source_filename,
                source_type=source_type,
                ingested_by=ingested_by,
                created_by=created_by,
                autocommit=False,
            )
        except Exception as exc:
            self._mark_source_failed(source_row, admin_id, str(exc))
            raise

        # 6. Mark source ready.
        self.source_repository.mark_status(
            source_row.id,
            admin_id=admin_id,
            status="ready",
            autocommit=False,
        )

        return IngestResult(
            luciel_instance_id=luciel_instance_id,
            source_id=source_row.id,
            source_version=source_row.source_version or 1,
            source_type=source_type,
            source_filename=source_filename,
            knowledge_type=knowledge_type,
            chunk_count=len(chunks),
            effective_config=cfg,
        )

    def _mark_source_failed(
        self,
        source_row: KnowledgeSource,
        admin_id: str,
        error: str,
    ) -> None:
        """Helper to flip a source row to ``failed`` without breaking
        the outer exception chain. Swallows secondary failures so the
        original exception still propagates."""
        try:
            self.source_repository.mark_status(
                source_row.id,
                admin_id=admin_id,
                status="failed",
                error=error[:2000],
                autocommit=False,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to mark knowledge_sources.id=%s as failed; "
                "original ingestion error will still propagate",
                source_row.id,
            )

    # ==============================================================
    # Helpers
    # ==============================================================

    @staticmethod
    def _validate_knowledge_type(knowledge_type: str) -> None:
        if knowledge_type not in KNOWLEDGE_TYPES:
            raise IngestionError(
                f"knowledge_type must be one of {KNOWLEDGE_TYPES}, "
                f"got {knowledge_type!r}"
            )

    def _resolve_chunking_config(
        self,
        *,
        admin_id: str,
        luciel_instance_id: int | None = None,
    ) -> EffectiveChunkingConfig:
        """Resolves the Admin → Instance chunking chain. The
        ``/admin/instances/{instance_id}/chunking-config`` diagnostic
        endpoint also calls this directly."""
        if not admin_id:
            raise IngestionError(
                "admin_id is required to resolve chunking config"
            )
        tenant = (
            self.db.query(Admin)
            .filter(Admin.id == admin_id)
            .one_or_none()
        )
        if tenant is None:
            raise IngestionError(f"Unknown admin_id: {admin_id!r}")

        instance = None
        if luciel_instance_id is not None:
            instance = self.db.get(Instance, luciel_instance_id)
            if instance is None:
                raise IngestionError(
                    f"Unknown luciel_instance_id: {luciel_instance_id}"
                )

        return resolve_effective_config(
            tenant=tenant, instance=instance,
        )
