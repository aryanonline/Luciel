"""
Knowledge retriever — post-Cleanup-B.

Retrieves relevant knowledge from the vector database for a user query
in a specific (admin, domain, luciel_instance) context.

Post-Cleanup-C contract:
    - Primary filter dimension stays ``luciel_instance_id`` (Step 24.5).
    - The legacy ``agent_id`` parameter and its read-compat fan-out
      are gone (Cleanup C — column dropped, zero production rows).
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

Two retrieval surfaces:
    * ``retrieve(...)`` — unchanged public shape; returns a
      ``list[str]`` of pre-formatted knowledge strings for direct
      injection into the chat prompt-assembly pipeline. This is what
      every existing call site uses; behaviour is preserved.
    * ``retrieve_with_sources(...)`` — richer surface returning a
      ``list[RetrievedChunk]`` that carries both the formatted
      string and the chunk's ``source_identifier`` (always ``int``
      post-Cleanup-B — the INTEGER FK to ``knowledge_sources.id``).
      Used by trace instrumentation and the orchestrator Retrieve
      step.
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
    ``traces.source_ids_used``. Post-Cleanup-B it is always an
    ``int`` — the ``knowledge_sources.id`` FK, which is NOT NULL on
    every chunk row. The pre-Cleanup-B ``str`` (legacy stringy
    ``source_id``) and ``None`` (no source identifier at all) cases
    are gone with the columns that backed them.
    """

    content: str
    knowledge_type: str
    title: str | None
    distance: float | None
    chunk_id: int
    source_identifier: int
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
        knowledge_type: str | None = None,
        limit: int = 5,
    ) -> list[RetrievedChunk]:
        """Richer surface used by Step 5 (trace instrumentation) and
        Step 8 (orchestrator Retrieve step).

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

            # Post-Cleanup-B: ``source_id`` is the INTEGER FK and is
            # NOT NULL on every chunk row. The repository's
            # search_similar returns it directly.
            ident = int(r["source_id"])

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
            "instance=%s",
            len(out), admin_id, domain_id, luciel_instance_id,
        )
        return out


def collect_source_pks(chunks: Sequence[RetrievedChunk]) -> list[int]:
    """Reduce a list of ``RetrievedChunk`` to the source PKs they
    carry, de-duplicated, preserving relevance-rank order.

    Used by trace instrumentation to populate
    ``traces.source_ids_used`` (a ``BIGINT[]``). Post-Cleanup-B
    every ``RetrievedChunk.source_identifier`` is an ``int`` (the
    ``knowledge_sources.id`` FK), so the pre-Cleanup-B
    isinstance-filter for ``str`` / ``None`` legacy chunks is gone.

    De-duplication preserves *insertion* order so the first chunk
    that contributed a source PK ranks higher than later chunks
    that re-used the same source — relevance-rank order the
    retriever already returned chunks in (cosine distance asc).

    Pure function: no I/O, no side effects.
    """
    seen: set[int] = set()
    out: list[int] = []
    for chunk in chunks:
        ident = chunk.source_identifier
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
