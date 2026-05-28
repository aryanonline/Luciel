"""
Knowledge retriever (Step 25b, File 11 — Arc 11 Step 3 update).

Retrieves relevant knowledge from the vector database for a user query
in a specific (admin, domain, luciel_instance) context.

Step 25b → Arc 11 Step 3:
    - Primary filter dimension stays ``luciel_instance_id`` (Step 24.5).
    - ``agent_id`` remains accepted for legacy rows (pre-Step-24.5).
    - Upward inheritance is delegated to
      ``KnowledgeRepository.search_similar``.
    - Active-only (``superseded_at IS NULL``), lifecycle-clean
      (``soft_deleted_at IS NULL``,
      ``pending_downgrade_archived_at IS NULL``) and
      ingestion-ready (parent ``KnowledgeSource.ingestion_status =
      'ready'``) filters are all enforced at the repository layer.
      Architecture v1 §3.2 retrieval flow step 1.
    - Never raises: any failure returns ``[]`` so chat is never
      blocked on retrieval.

Two retrieval surfaces (Arc 11 Step 3):
    * ``retrieve(...)`` — unchanged public shape; returns a
      ``list[str]`` of pre-formatted knowledge strings for direct
      injection into the chat prompt-assembly pipeline. This is what
      every existing call site uses; behaviour is preserved.
    * ``retrieve_with_sources(...)`` — new richer surface returning a
      ``list[RetrievedChunk]`` that carries both the formatted
      string and the chunk's ``source_identifier`` (``int`` for
      Arc-11-shape chunks, ``str`` for legacy chunks). Used by:
        - Arc 11 Step 5 (trace instrumentation: populate
          ``traces.source_ids_used``).
        - Arc 11 Step 8 (orchestrator Retrieve step).
      Both call sites need the source identifier; neither needs the
      full ``KnowledgeSource`` row, so we surface the identifier
      alone rather than the whole ORM object.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from app.knowledge.embedder import embed_single
from app.repositories.knowledge_repository import KnowledgeRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetrievedChunk:
    """One chunk returned by the retriever, with the metadata that
    downstream code (orchestrator + trace instrumentation) needs.

    ``source_identifier`` is the value Step 5 records in
    ``traces.source_ids_used``. It is typed ``int | str``:
        - ``int`` when the chunk has a parent ``knowledge_sources``
          row (Arc 11 shape) — the row's primary key.
        - ``str`` when the chunk is a legacy row with no FK — the
          stringy ``source_id``.
        - ``None`` when neither is set (extremely-pre-Arc-11 chunks
          that were ingested without any source identifier at all).
    """

    content: str
    knowledge_type: str
    title: str | None
    distance: float | None
    chunk_id: int
    source_identifier: int | str | None
    formatted: str
    """Pre-formatted ``[<type>] <title>: <content>`` string used by
    the existing chat path. Pre-computed so callers that just want
    the strings don't need to know the format."""


class KnowledgeRetriever:
    """Thin wrapper over embedder + ``KnowledgeRepository.search_similar``."""

    def __init__(self, repository: KnowledgeRepository) -> None:
        self.repository = repository

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def retrieve(
        self,
        *,
        query: str,
        admin_id: str | None = None,
        domain_id: str | None = None,
        luciel_instance_id: int | None = None,
        agent_id: str | None = None,
        knowledge_type: str | None = None,
        limit: int = 5,
    ) -> list[str]:
        """Retrieve knowledge strings for a query — unchanged shape.

        Returns a list of human-readable strings formatted as
        ``[<knowledge_type>] <title>: <content>`` (or without title
        when absent) for direct injection into the prompt-assembly
        pipeline in ChatService.

        Never raises. On any failure, returns ``[]`` and logs a
        warning — chat must always be answerable even when retrieval
        fails.
        """
        chunks = self.retrieve_with_sources(
            query=query,
            admin_id=admin_id,
            domain_id=domain_id,
            luciel_instance_id=luciel_instance_id,
            agent_id=agent_id,
            knowledge_type=knowledge_type,
            limit=limit,
        )
        return [c.formatted for c in chunks]

    def retrieve_with_sources(
        self,
        *,
        query: str,
        admin_id: str | None = None,
        domain_id: str | None = None,
        luciel_instance_id: int | None = None,
        agent_id: str | None = None,
        knowledge_type: str | None = None,
        limit: int = 5,
    ) -> list[RetrievedChunk]:
        """Richer surface used by Arc 11 Step 5 (trace instrumentation)
        and Step 8 (orchestrator Retrieve step).

        Same filtering semantics as ``retrieve``. Returns one
        ``RetrievedChunk`` per row, in the same order. Never raises.
        """
        if not query or not query.strip():
            return []

        try:
            query_embedding = embed_single(query)
            results = self.repository.search_similar(
                query_embedding=query_embedding,
                admin_id=admin_id,
                domain_id=domain_id,
                luciel_instance_id=luciel_instance_id,
                agent_id=agent_id,
                knowledge_type=knowledge_type,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001 — retrieval must not 500 chat
            logger.warning("Knowledge retrieval failed: %s", exc)
            return []

        out: list[RetrievedChunk] = []
        for r in results:
            content = r["content"]
            k_type = r["knowledge_type"]
            title = r.get("title") or ""
            if title:
                formatted = f"[{k_type}] {title}: {content}"
            else:
                formatted = f"[{k_type}] {content}"

            # Prefer the source-row PK (Arc 11 shape) over the legacy
            # stringy id. Falls back to the stringy id when the chunk
            # has no FK. ``None`` only for pre-Step-25b chunks that
            # never carried a source identifier at all.
            source_record_id = r.get("source_record_id")
            legacy_string = r.get("source_id")
            if source_record_id is not None:
                ident: int | str | None = int(source_record_id)
            elif legacy_string:
                ident = legacy_string
            else:
                ident = None

            out.append(
                RetrievedChunk(
                    content=content,
                    knowledge_type=k_type,
                    title=title or None,
                    distance=r.get("distance"),
                    chunk_id=r["id"],
                    source_identifier=ident,
                    formatted=formatted,
                )
            )

        logger.info(
            "Retrieved %d knowledge chunks for tenant=%s domain=%s "
            "instance=%s agent=%s",
            len(out), admin_id, domain_id, luciel_instance_id, agent_id,
        )
        return out


def collect_source_pks(chunks: Sequence[RetrievedChunk]) -> list[int]:
    """Reduce a list of ``RetrievedChunk`` to the ``int`` source PKs
    they carry, de-duplicated, preserving relevance-rank order.

    Used by Arc 11 Step 5 (trace instrumentation) to populate
    ``traces.source_ids_used`` (a ``BIGINT[]``). Three cases for
    ``RetrievedChunk.source_identifier``:

      * ``int``  — modern chunk with a ``knowledge_sources`` row.
                  Included.
      * ``str``  — legacy chunk whose only source identifier is the
                  free-form ``source_id`` string. **Excluded**: the
                  ``traces.source_ids_used`` column is ``BIGINT[]``
                  so a string would be a type error, and the
                  Architecture §3.2.2 delete-confirm modal preview
                  is only meaningful for knowledge_sources rows
                  anyway (legacy stringless rows are not deletable
                  through the new UI).
      * ``None`` — neither populated. Excluded.

    De-duplication preserves *insertion* order so the first chunk
    that contributed a source PK ranks higher than later chunks
    that re-used the same source. This is the relevance-rank
    ordering the retriever already returned chunks in (sort by
    cosine distance ascending) — preserving it gives Architecture
    §5.1's "what sources actually contributed" semantics for free.

    Pure function: no I/O, no side effects. Step 8's orchestrator
    invokes it between ``retrieve_with_sources(...)`` and
    ``TraceService.record_trace(...)``. Step 6 (Celery embed-worker
    smoke probe) and Step 7 (``POST /internal/v1/retrieve``) will
    also call it.
    """
    seen: set[int] = set()
    out: list[int] = []
    for chunk in chunks:
        ident = chunk.source_identifier
        if not isinstance(ident, int):
            # Catches both ``str`` and ``None``. Booleans are
            # technically ``isinstance(True, int)`` in Python, but
            # ``source_identifier`` can never be a bool given the
            # retriever populates it from a DB column / dict get;
            # not worth a defensive check.
            continue
        if ident in seen:
            continue
        seen.add(ident)
        out.append(ident)
    return out


__all__: Sequence[str] = (
    "KnowledgeRetriever",
    "RetrievedChunk",
    "collect_source_pks",
)
