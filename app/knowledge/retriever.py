"""
Knowledge retriever (Step 25b, File 11).

Retrieves relevant knowledge from the vector database for a user query
in a specific (tenant, domain, luciel_instance) context.

Step 25b rewrite:
    - Primary filter dimension is luciel_instance_id (Step 24.5 split).
    - agent_id remains accepted for legacy rows (pre-Step-24.5) so chat
      against the legacy path keeps returning those chunks during the
      transition window.
    - Upward inheritance is delegated to KnowledgeRepository.search_similar.
    - Active-only (superseded_at IS NULL) is enforced at the repository
      layer, not here.
    - Never raises: any failure returns [] so chat is never blocked on
      retrieval.
"""
from __future__ import annotations

import logging

from app.knowledge.embedder import embed_single
from app.repositories.knowledge_repository import KnowledgeRepository

logger = logging.getLogger(__name__)


class KnowledgeRetriever:
    """Thin wrapper over embedder + KnowledgeRepository.search_similar."""

    def __init__(self, repository: KnowledgeRepository) -> None:
        self.repository = repository

    def retrieve(
        self,
        *,
        query: str,
        tenant_id: str | None = None,
        domain_id: str | None = None,
        luciel_instance_id: int | None = None,
        agent_id: str | None = None,
        knowledge_type: str | None = None,
        limit: int = 5,
    ) -> list[str]:
        """Retrieve knowledge strings for a query.

        Returns a list of human-readable strings formatted as
        `[<knowledge_type>] <title>: tent>` (or without title when
        absent) for direct injection into the prompt-assembly pipeline
        in ChatService.

        Never raises. On any failure, returns [] and logs a warning —
        chat must always be answerable even when retrieval fails.
        """
        if not query or not query.strip():
            return []

        try:
            query_embedding = embed_single(query)
            results = self.repository.search_similar(
                query_embedding=query_embedding,
                tenant_id=tenant_id,
                domain_id=domain_id,
                luciel_instance_id=luciel_instance_id,
                agent_id=agent_id,
                knowledge_type=knowledge_type,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001 — retrieval must not 500 chat
            logger.warning("Knowledge retrieval failed: %s", exc)
            return []

        knowledge_strings: list[str] = []
        for r in results:
            k_type = r["knowledge_type"]
            content = r["content"]
            title = r.get("title") or ""
            if title:
                knowledge_strings.append(f"[{k_type}] {title}: {content}")
            else:
                knowledge_strings.append(f"[{k_type}] {content}")

        logger.info(
            "Retrieved %d knowledge chunks for tenant=%s domain=%s "
            "instance=%s agent=%s",
            len(knowledge_strings),
            tenant_id, domain_id, luciel_instance_id, agent_id,
        )
        return knowledge_strings