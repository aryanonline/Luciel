"""
Knowledge retriever.

Retrieves relevant knowledge from the vector database
for a given user query in a specific tenant/domain context.
"""

from __future__ import annotations

import logging

from app.knowledge.embedder import embed_single
from app.repositories.knowledge_repository import KnowledgeRepository

logger = logging.getLogger(__name__)


class KnowledgeRetriever:

    def __init__(self, repository: KnowledgeRepository) -> None:
        self.repository = repository

    def retrieve(
        self,
        *,
        query: str,
        tenant_id: str | None = None,
        domain_id: str | None = None,
        limit: int = 5,
    ) -> list[str]:
        """
        Retrieve relevant knowledge for a query.
        Returns empty list on any failure so chat is never blocked.
        """
        if not query or not query.strip():
            return []

        try:
            query_embedding = embed_single(query)

            results = self.repository.search_similar(
                query_embedding=query_embedding,
                tenant_id=tenant_id,
                domain_id=domain_id,
                limit=limit,
            )

            knowledge_strings = []
            for result in results:
                k_type = result["knowledge_type"]
                content = result["content"]
                title = result.get("title", "")

                if title:
                    knowledge_strings.append(f"[{k_type}] {title}: {content}")
                else:
                    knowledge_strings.append(f"[{k_type}] {content}")

            logger.info(
                "Retrieved %d knowledge chunks for tenant=%s domain=%s",
                len(knowledge_strings), tenant_id, domain_id,
            )

            return knowledge_strings

        except Exception as exc:
            logger.warning("Knowledge retrieval failed: %s", exc)
            return []