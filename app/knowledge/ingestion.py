"""
Knowledge ingestion service (Step 25b, File 9 — rewrite).

Orchestrates the full pipeline:

    bytes / text
        -> detect source_type (from filename suffix, or caller-supplied)
        -> Parser.parse()                    (File 5)
        -> resolve EffectiveChunkingConfig   (File 6, three-level inheritance)
        -> chunk_text()                      (File 6)
        -> embed_texts()                     (existing embedder)
        -> KnowledgeRepository.add_chunks()  (File 8)

Scope binding:
    Every ingest targets exactly one (tenant_id, domain_id,
    luciel_instance_id) triple. Authorization happens upstream in the
    admin route (File 10) via ScopePolicy.enforce_luciel_instance_scope
    — this service does NOT re-check scope. Pure orchestration.

Versioning:
    - New ingest with no source_id          -> source_version = 1
    - New ingest with fresh source_id       -> source_version = 1
    - Re-ingest with replace_existing=True  -> supersede current active
      version, new rows written at previous_max + 1
    - Re-ingest with replace_existing=False and source_id collides
      -> IngestionError (caller must opt in to replace)
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
from app.models.domain_config import DomainConfig
from app.models.knowledge import KnowledgeEmbedding
from app.models.luciel_instance import LucielInstance
from app.models.tenant import TenantConfig
from app.repositories.knowledge_repository import KnowledgeRepository
from app.schemas.knowledge import KNOWLEDGE_TYPES

logger = logging.getLogger(__name__)


class IngestionError(Exception):
    """Raised when an ingest cannot be completed for a semantic reason.

    Distinct from ParserError (raised by File 5 parsers) which is about
    the raw bytes being unparseable; IngestionError is about the
    overall ingest request being invalid or conflicting.
    """


@dataclass(frozen=True)
class IngestResult:
    """Return shape of every successful ingest call."""
    luciel_instance_id: int | None
    source_id: str | None
    source_version: int
    source_type: str | None
    source_filename: str | None
    knowledge_type: str
    chunk_count: int
    superseded_previous_version: int  # count of rows superseded, 0 for fresh
    effective_config: EffectiveChunkingConfig


class IngestionService:
    """Knowledge ingestion orchestrator."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repository = KnowledgeRepository(db)

    # ==============================================================
    # Public entry points
    # ==============================================================

    def ingest_file(
        self,
        *,
        file_bytes: bytes,
        filename: str,
        tenant_id: str | None,
        domain_id: str | None,
        luciel_instance_id: int | None,
        knowledge_type: str = "luciel_knowledge",
        title: str | None = None,
        source_id: str | None = None,
        source_type: str | None = None,
        ingested_by: str | None = None,
        created_by: str | None = None,
        replace_existing: bool = False,
    ) -> IngestResult:
        """Ingest raw file bytes. Detects source_type from filename
        unless explicitly supplied.
        """
        self._validate_knowledge_type(knowledge_type)

        # 1. Resolve source_type.
        if source_type is None:
            try:
                source_type = detect_source_type(filename)
            except UnsupportedSourceType as exc:
                raise IngestionError(str(exc)) from exc

        # 2. Parse raw bytes -> normalized text.
        try:
            parser = get_parser(source_type)
        except UnsupportedSourceType as exc:
            raise IngestionError(str(exc)) from exc
        try:
            parsed = parser.parse(file_bytes, filename)
        except ParserError as exc:
            raise IngestionError(f"Parse failed for {filename!r}: {exc}") from exc

        # 3. Delegate to the shared text-path.
        return self._ingest_text(
            text=parsed.text,
            tenant_id=tenant_id,
            domain_id=domain_id,
            luciel_instance_id=luciel_instance_id,
            knowledge_type=knowledge_type,
            title=title,
            source=filename,
            source_id=source_id,
            source_filename=filename,
            source_type=source_type,
            ingested_by=ingested_by,
            created_by=created_by,
            replace_existing=replace_existing,
        )

    def ingest_text(
        self,
        *,
        content: str,
        tenant_id: str | None,
        domain_id: str | None,
        luciel_instance_id: int | None,
        knowledge_type: str = "luciel_knowledge",
        title: str | None = None,
        source: str | None = None,
        source_id: str | None = None,
        source_filename: str | None = None,
        ingested_by: str | None = None,
        created_by: str | None = None,
        replace_existing: bool = False,
    ) -> IngestResult:
        """Ingest already-extracted text (no file parsing)."""
        self._validate_knowledge_type(knowledge_type)
        return self._ingest_text(
            text=content,
            tenant_id=tenant_id,
            domain_id=domain_id,
            luciel_instance_id=luciel_instance_id,
            knowledge_type=knowledge_type,
            title=title,
            source=source,
            source_id=source_id,
            source_filename=source_filename,
            source_type=None,
            ingested_by=ingested_by,
            created_by=created_by,
            replace_existing=replace_existing,
        )

    # ==============================================================
    # Shared internal path
    # ==============================================================

    def _ingest_text(
        self,
        *,
        text: str,
        tenant_id: str | None,
        domain_id: str | None,
        luciel_instance_id: int | None,
        knowledge_type: str,
        title: str | None,
        source: str | None,
        source_id: str | None,
        source_filename: str | None,
        source_type: str | None,
        ingested_by: str | None,
        created_by: str | None,
        replace_existing: bool,
    ) -> IngestResult:
        if not text or not text.strip():
            raise IngestionError("Ingested content is empty after parsing")

        # 4. Resolve the effective chunking config (instance -> domain -> tenant).
        cfg = self._resolve_chunking_config(
            tenant_id=tenant_id,
            domain_id=domain_id,
            luciel_instance_id=luciel_instance_id,
        )
        logger.info(
            "Ingest: instance=%s tenant=%s domain=%s strategy=%s "
            "size=%s overlap=%s (size_src=%s overlap_src=%s strategy_src=%s)",
            luciel_instance_id, tenant_id, domain_id,
            cfg.chunk_strategy, cfg.chunk_size, cfg.chunk_overlap,
            cfg.size_source, cfg.overlap_source, cfg.strategy_source,
        )

        # 5. Chunk.
        chunks = chunk_text(text, cfg)
        if not chunks:
            raise IngestionError("Chunker produced no chunks from non-empty input")

        # 6. Version / replace handling.
        next_version, superseded = self._prepare_versioning(
            luciel_instance_id=luciel_instance_id,
            source_id=source_id,
            replace_existing=replace_existing,
        )

        # 7. Embed.
        try:
            embeddings = embed_texts(chunks)
        except Exception as exc:
            # Embedder failures are infra issues, not caller errors — surface
            # as IngestionError so the admin route returns a clean 502/500.
            raise IngestionError(f"Embedding failed: {exc}") from exc
        if len(embeddings) != len(chunks):
            raise IngestionError(
                f"Embedder returned {len(embeddings)} vectors for "
                f"{len(chunks)} chunks"
            )

        # 8. Persist.
        self.repository.add_chunks(
            chunks=chunks,
            embeddings=embeddings,
            tenant_id=tenant_id,
            domain_id=domain_id,
            agent_id=None,               # Step 25b writes use luciel_instance_id
            luciel_instance_id=luciel_instance_id,
            knowledge_type=knowledge_type,
            title=title,
            source=source,
            source_id=source_id,
            source_version=next_version,
            source_filename=source_filename,
            source_type=source_type,
            ingested_by=ingested_by,
            created_by=created_by,
            autocommit=False,
        )

        return IngestResult(
            luciel_instance_id=luciel_instance_id,
            source_id=source_id,
            source_version=next_version,
            source_type=source_type,
            source_filename=source_filename,
            knowledge_type=knowledge_type,
            chunk_count=len(chunks),
            superseded_previous_version=superseded,
            effective_config=cfg,
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
        tenant_id: str | None,
        domain_id: str | None,
        luciel_instance_id: int | None,
    ) -> EffectiveChunkingConfig:
        if not tenant_id:
            raise IngestionError(
                "tenant_id is required to resolve chunking config"
            )
        tenant = (
            self.db.query(TenantConfig)
            .filter(TenantConfig.tenant_id == tenant_id)
            .one_or_none()
        )
        if tenant is None:
            raise IngestionError(f"Unknown tenant_id: {tenant_id!r}")

        domain = None
        if domain_id is not None:
            domain = (
                self.db.query(DomainConfig)
                .filter(
                    DomainConfig.tenant_id == tenant_id,
                    DomainConfig.domain_id == domain_id,
                )
                .one_or_none()
            )

        instance = None
        if luciel_instance_id is not None:
            instance = self.db.get(LucielInstance, luciel_instance_id)
            if instance is None:
                raise IngestionError(
                    f"Unknown luciel_instance_id: {luciel_instance_id}"
                )

        return resolve_effective_config(
            tenant=tenant, domain=domain, instance=instance
        )

    def _prepare_versioning(
        self,
        *,
        luciel_instance_id: int | None,
        source_id: str | None,
        replace_existing: bool,
    ) -> tuple[int, int]:
        """Decide the next source_version and (if replacing) supersede
        the current active version in the same transaction-ish flow.

        Returns (next_version, superseded_row_count).

        Cases:
          - luciel_instance_id is None OR source_id is None:
                no versioning possible; returns (1, 0). Legacy / shared
                chunks ingested without a source identifier can't be
                meaningfully replaced — callers that need replace must
                supply both.
          - Fresh (no existing rows for this instance+source):
                (1, 0).
          - Existing rows, replace_existing=False:
                raise IngestionError (collision).
          - Existing rows, replace_existing=True:
                supersede active rows, return (max_version + 1, count).
        """
        if luciel_instance_id is None or source_id is None:
            return 1, 0

        current_max = self.repository.latest_version_for_source(
            luciel_instance_id=luciel_instance_id,
            source_id=source_id,
        )
        if current_max == 0:
            return 1, 0

        if not replace_existing:
            raise IngestionError(
                f"source_id {source_id!r} already exists on "
                f"luciel_instance_id={luciel_instance_id} at version "
                f"{current_max}. Pass replace_existing=True to supersede."
            )

        superseded = self.repository.supersede_source(
            luciel_instance_id=luciel_instance_id,
            source_id=source_id,
            autocommit=False,
        )
        return current_max + 1, superseded

    # ==============================================================
    # Backward-compat shim
    # ==============================================================

    def ingest(
        self,
        *,
        content: str,
        knowledge_type: str,
        tenant_id: str | None = None,
        domain_id: str | None = None,
        agent_id: str | None = None,   # accepted but not written (Step 25b)
        title: str | None = None,
        source: str | None = None,
        created_by: str | None = None,
        max_chunk_size: int | None = None,   # accepted, ignored — config-driven now
        replace_existing: bool = False,
    ) -> int:
        """Legacy entry point kept for pre-Step-25b callers.

        Signature matches the old IngestionService.ingest() in spirit
        (text blob in, chunk count out). Internally routes through the
        new ingest_text() path. `agent_id` and `max_chunk_size` are
        accepted for call-site compatibility but ignored: new writes
        bind via luciel_instance_id (None = tenant/domain/global shared
        knowledge), and chunk size comes from the resolved config.
        """
        result = self.ingest_text(
            content=content,
            tenant_id=tenant_id,
            domain_id=domain_id,
            luciel_instance_id=None,
            knowledge_type=knowledge_type,
            title=title,
            source=source,
            source_id=None,
            source_filename=None,
            ingested_by=created_by,
            created_by=created_by,
            replace_existing=replace_existing,
        )
        return result.chunk_count