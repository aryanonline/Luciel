"""
Knowledge ingestion service (Step 25b, File 9 — Arc 11 Step 3 update).

Orchestrates the full pipeline:

    bytes / text
        -> detect source_type (from filename suffix, or caller-supplied)
        -> Parser.parse()                    (File 5)
        -> resolve EffectiveChunkingConfig   (File 6, three-level inheritance)
        -> chunk_text()                      (File 6)
        -> KnowledgeSourceRepository.create_source()   (Arc 11 Step 3)
        -> embed_texts()                              (existing embedder)
        -> KnowledgeRepository.add_chunks(source_fk=...) (Arc 11 Step 3)
        -> KnowledgeSourceRepository.mark_status('ready' | 'failed')

Two-table cutover (Arc 11):
    * When the caller supplies BOTH ``admin_id`` AND
      ``luciel_instance_id``, a row in ``knowledge_sources`` is
      created first. Every chunk row gets ``source_fk = source.id``.
      The legacy stringy ``source_id`` column is *also* populated
      (default ``f"src-{source.id}"`` when the caller does not pass
      one of their own) so the legacy read paths
      (data_export_service, downgrade_archive_service) keep working
      until Step 11 retires them.
    * When ``admin_id`` or ``luciel_instance_id`` is None, the
      ingest predates the two-table model. The service writes
      chunks with ``source_fk=None`` and the caller-supplied (or
      None) stringy ``source_id``. No ``knowledge_sources`` row is
      created — its NOT NULL columns can't be satisfied. The
      retriever's fallback to the stringy ``source_id`` handles
      these rows.

Scope binding:
    Every ingest targets exactly one (admin_id, domain_id,
    luciel_instance_id) triple. Authorization happens upstream in the
    admin route (File 10) via ScopePolicy.enforce_luciel_instance_scope
    — this service does NOT re-check scope. Pure orchestration.

Versioning:
    - New ingest with no source_id          -> source_version = 1
    - New ingest with fresh source_id       -> source_version = 1
    - Re-ingest with replace_existing=True  -> supersede current active
      version, new rows written at previous_max + 1. The matching
      ``knowledge_sources`` row's ``source_version`` is also bumped.
    - Re-ingest with replace_existing=False and source_id collides
      -> IngestionError (caller must opt in to replace)

Quota:
    Quota enforcement is NOT done here — Step 7 enforces it at the
    API boundary per Architecture v1 §3.2 ("Per-Admin and per-tier
    quotas (file-size caps + total quotas) enforced at the API
    boundary").
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
# Arc 5 Path A: DomainConfig REMOVED (V2 has no Domain layer)
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
    """Legacy stringy source identifier (chunk-level grouping key).
    Still populated; will be retired in Arc 11 Step 11."""
    source_pk: int | None
    """Arc 11 Step 3: primary key of the corresponding
    ``knowledge_sources`` row (if one was created — see module
    docstring). ``None`` for legacy/global ingests that skip the
    source row."""
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
        # Arc 11 Step 3 — the source-row repository for the new
        # two-table model. Created here so the service owns both
        # writes inside a single transaction.
        self.source_repository = KnowledgeSourceRepository(db)

    # ==============================================================
    # Public entry points
    # ==============================================================

    def ingest_file(
        self,
        *,
        file_bytes: bytes,
        filename: str,
        admin_id: str | None,
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

        # 3. Delegate to the shared text-path. ``size_bytes`` is the
        # length of the raw upload (what the API quota check sees);
        # NOT the parsed text length — the quota guards storage cost.
        return self._ingest_text(
            text=parsed.text,
            admin_id=admin_id,
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
            size_bytes=len(file_bytes),
        )

    def ingest_text(
        self,
        *,
        content: str,
        admin_id: str | None,
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
            admin_id=admin_id,
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
            size_bytes=len(content.encode("utf-8")),
        )

    # ==============================================================
    # Shared internal path
    # ==============================================================

    def _ingest_text(
        self,
        *,
        text: str,
        admin_id: str | None,
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
        size_bytes: int,
    ) -> IngestResult:
        if not text or not text.strip():
            raise IngestionError("Ingested content is empty after parsing")

        # 4. Resolve the effective chunking config (instance -> domain -> tenant).
        cfg = self._resolve_chunking_config(
            admin_id=admin_id,
            domain_id=domain_id,
            luciel_instance_id=luciel_instance_id,
        )
        logger.info(
            "Ingest: instance=%s tenant=%s domain=%s strategy=%s "
            "size=%s overlap=%s size_bytes=%s "
            "(size_src=%s overlap_src=%s strategy_src=%s)",
            luciel_instance_id, admin_id, domain_id,
            cfg.chunk_strategy, cfg.chunk_size, cfg.chunk_overlap,
            size_bytes,
            cfg.size_source, cfg.overlap_source, cfg.strategy_source,
        )

        # 5. Chunk.
        chunks = chunk_text(text, cfg)
        if not chunks:
            raise IngestionError("Chunker produced no chunks from non-empty input")

        # 6. Version / replace handling on the chunk side (legacy
        # stringy source_id grouping). The source-row side bumps its
        # own version below if we created one earlier; the chunk-side
        # supersede stays grouped by the stringy source_id during
        # the cutover because pre-Arc-11 chunks exist only with that
        # column populated. Step 11 unifies the grouping on source_fk.
        next_version, superseded = self._prepare_versioning(
            luciel_instance_id=luciel_instance_id,
            source_id=source_id,
            replace_existing=replace_existing,
        )

        # 7. Source-row (Arc 11 two-table model).
        #
        # Strict prerequisites: both ``admin_id`` and
        # ``luciel_instance_id`` must be set. Legacy global / shared
        # ingests (admin_id None, instance None) skip the source row
        # — their chunks carry NULL source_fk and the retriever's
        # legacy fallback handles them. Until Step 11, this
        # branching is by design.
        source_row: KnowledgeSource | None = None
        if admin_id and luciel_instance_id is not None:
            source_row = self._create_or_bump_source(
                admin_id=admin_id,
                luciel_instance_id=luciel_instance_id,
                filename=source_filename or source,
                source_type=source_type or "txt",
                size_bytes=size_bytes,
                ingested_by=ingested_by or created_by or "unknown",
                superseded=superseded,
            )

        # 7b. If the caller did not supply a stringy source_id but we
        # created a source row, synthesise one from the row's id so
        # legacy reads (data_export, downgrade_archive) still find
        # the chunks via their stringy key. ``src-<pk>`` is the
        # canonical form; chosen over ``str(source_uuid)`` because
        # downgrade_archive_service already sorts by source_id and
        # numeric-suffix strings sort deterministically.
        effective_string_source_id = source_id
        if effective_string_source_id is None and source_row is not None:
            effective_string_source_id = f"src-{source_row.id}"

        # 8. Embed. Failures here flip the source row to 'failed' so
        # the admin UI surfaces the error before the exception
        # bubbles to the route.
        try:
            embeddings = embed_texts(chunks)
        except Exception as exc:
            if source_row is not None:
                self._mark_source_failed(source_row, admin_id, str(exc))
            raise IngestionError(f"Embedding failed: {exc}") from exc
        if len(embeddings) != len(chunks):
            if source_row is not None:
                self._mark_source_failed(
                    source_row, admin_id,
                    f"Embedder returned {len(embeddings)} vectors for "
                    f"{len(chunks)} chunks",
                )
            raise IngestionError(
                f"Embedder returned {len(embeddings)} vectors for "
                f"{len(chunks)} chunks"
            )

        # 9. Persist chunks. ``source_fk`` is set when we created a
        # source row; otherwise NULL (legacy path).
        try:
            self.repository.add_chunks(
                chunks=chunks,
                embeddings=embeddings,
                admin_id=admin_id,
                domain_id=domain_id,
                agent_id=None,        # Step 25b writes use luciel_instance_id
                luciel_instance_id=luciel_instance_id,
                knowledge_type=knowledge_type,
                title=title,
                source=source,
                source_id=effective_string_source_id,
                source_fk=source_row.id if source_row is not None else None,
                source_version=next_version,
                source_filename=source_filename,
                source_type=source_type,
                ingested_by=ingested_by,
                created_by=created_by,
                autocommit=False,
            )
        except Exception as exc:
            if source_row is not None:
                self._mark_source_failed(source_row, admin_id, str(exc))
            raise

        # 10. Mark source ready.
        if source_row is not None:
            self.source_repository.mark_status(
                source_row.id,
                admin_id=admin_id,  # type: ignore[arg-type]  # known non-None
                status="ready",
                autocommit=False,
            )

        return IngestResult(
            luciel_instance_id=luciel_instance_id,
            source_id=effective_string_source_id,
            source_pk=source_row.id if source_row is not None else None,
            source_version=next_version,
            source_type=source_type,
            source_filename=source_filename,
            knowledge_type=knowledge_type,
            chunk_count=len(chunks),
            superseded_previous_version=superseded,
            effective_config=cfg,
        )

    def _create_or_bump_source(
        self,
        *,
        admin_id: str,
        luciel_instance_id: int,
        filename: str | None,
        source_type: str,
        size_bytes: int,
        ingested_by: str,
        superseded: int,
    ) -> KnowledgeSource:
        """Create a fresh source row, or bump the version of the
        existing one when we just superseded its chunks.

        Re-ingest detection is by ``(admin_id, luciel_instance_id,
        filename)`` because the stringy ``source_id`` is the chunk-
        side grouping key, not a source-row column. When the chunk
        supersede already happened (``superseded > 0``) and a non-
        soft-deleted source row exists for this (admin, instance,
        filename), we bump that row instead of creating a duplicate.
        Anchored to ARC11_PLAN.md §3.4: re-ingest keeps the
        ``source_id`` and bumps ``source_version``.
        """
        # Fast path: fresh ingest — just create.
        if superseded == 0:
            return self.source_repository.create_source(
                admin_id=admin_id,
                luciel_instance_id=luciel_instance_id,
                filename=filename,
                source_type=source_type,
                size_bytes=size_bytes,
                ingested_by=ingested_by,
                ingestion_status="processing",
            )

        # Re-ingest: locate the prior source row (active, same
        # filename on same scope). If multiple match, pick the
        # newest. If none match — unusual — fall back to creating
        # a fresh row so we don't lose the audit trail.
        from sqlalchemy import select as sa_select
        stmt = (
            sa_select(KnowledgeSource)
            .where(
                KnowledgeSource.admin_id == admin_id,
                KnowledgeSource.luciel_instance_id == luciel_instance_id,
                KnowledgeSource.filename == filename,
                KnowledgeSource.soft_deleted_at.is_(None),
            )
            .order_by(KnowledgeSource.ingested_at.desc())
            .limit(1)
        )
        existing = self.db.execute(stmt).scalar_one_or_none()
        if existing is None:
            return self.source_repository.create_source(
                admin_id=admin_id,
                luciel_instance_id=luciel_instance_id,
                filename=filename,
                source_type=source_type,
                size_bytes=size_bytes,
                ingested_by=ingested_by,
                ingestion_status="processing",
            )
        # Found — bump it. The bumped row's size_bytes is updated
        # to reflect the new upload's bytes; this is the value the
        # quota worker reads.
        existing.size_bytes = size_bytes
        self.source_repository.bump_version(existing.id, admin_id=admin_id)
        return existing

    def _mark_source_failed(
        self,
        source_row: KnowledgeSource,
        admin_id: str | None,
        error: str,
    ) -> None:
        """Helper to flip a source row to ``failed`` without breaking
        the outer exception chain. Swallows secondary failures so the
        original exception still propagates."""
        if not admin_id:
            return
        try:
            self.source_repository.mark_status(
                source_row.id,
                admin_id=admin_id,
                status="failed",
                error=error[:2000],  # ingestion_error is Text but cap defensively
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
        admin_id: str | None,
        domain_id: str | None,  # V2: ignored, kept for call-site compat
        luciel_instance_id: int | None,
    ) -> EffectiveChunkingConfig:
        """Arc 5 Path A (V2): resolves the Admin → Instance chain.

        The ``domain_id`` argument is accepted for source-compatibility
        with legacy callers but ignored — V2 has no Domain layer.
        """
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
            tenant=tenant, instance=instance
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
        admin_id: str | None = None,
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
            admin_id=admin_id,
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