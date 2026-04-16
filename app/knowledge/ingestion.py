"""
Knowledge ingestion service.

The shared pipeline that all data sources use to add knowledge
to Luciel's vector database.

Pipeline steps:
  1. Validate the input.
  2. Chunk the content into pieces.
  3. Embed each chunk using OpenAI.
  4. Store each chunk with its embedding and scope tags.
"""

from __future__ import annotations

import logging

from app.knowledge.chunker import chunk_text
from app.knowledge.embedder import embed_texts
from app.repositories.knowledge_repository import KnowledgeRepository

logger = logging.getLogger(__name__)

VALID_KNOWLEDGE_TYPES = (
    "domain_knowledge",
    "tenant_document",
    "role_instruction",
    "agent_knowledge",
)


class IngestionService:

    def __init__(self, repository: KnowledgeRepository) -> None:
        self.repository = repository

    def ingest(
        self,
        *,
        content: str,
        knowledge_type: str,
        tenant_id: str | None = None,
        domain_id: str | None = None,
        agent_id: str | None = None,
        title: str | None = None,
        source: str | None = None,
        created_by: str | None = None,
        max_chunk_size: int = 800,
        replace_existing: bool = False,
    ) -> int:
        """
        Ingest a piece of content into the knowledge base.

        Args:
            content:          The raw text to ingest.
            knowledge_type:   One of domain_knowledge, tenant_document,
                              role_instruction, agent_knowledge.
            tenant_id:        Who this knowledge belongs to (None for shared domain knowledge).
            domain_id:        Which domain this is for (None for tenant-wide knowledge).
            agent_id:         Which agent this is for (None for tenant/domain-wide knowledge).
            title:            Human-readable label for this content.
            source:           Where this content came from (filename, URL, etc.).
            created_by:       Who triggered this ingestion.
            max_chunk_size:   Maximum characters per chunk.
            replace_existing: If True and source is provided, delete existing
                              chunks from the same source before ingesting.

        Returns:
            Number of chunks stored.
        """
        # 1. Validate
        if not content or not content.strip():
            raise ValueError("Content cannot be empty")

        if knowledge_type not in VALID_KNOWLEDGE_TYPES:
            raise ValueError(
                f"Invalid knowledge_type: {knowledge_type}. "
                f"Must be one of: {', '.join(VALID_KNOWLEDGE_TYPES)}"
            )

        if knowledge_type == "tenant_document" and not tenant_id:
            raise ValueError("tenant_document requires a tenant_id")

        if knowledge_type == "role_instruction" and (not tenant_id or not domain_id):
            raise ValueError("role_instruction requires both tenant_id and domain_id")

        if knowledge_type == "agent_knowledge" and (not tenant_id or not agent_id):
            raise ValueError("agent_knowledge requires both tenant_id and agent_id")

        # 2. Delete existing chunks if replacing
        if replace_existing and source and tenant_id:
            deleted = self.repository.delete_by_source(
                tenant_id=tenant_id, source=source,
            )
            if deleted:
                logger.info(
                    "Deleted %d existing chunks from source %s", deleted, source
                )

        # 3. Chunk the content
        chunks = chunk_text(content, max_chunk_size=max_chunk_size)
        if not chunks:
            logger.warning("No chunks produced from content")
            return 0
        logger.info("Content chunked into %d pieces", len(chunks))

        # 4. Embed all chunks in one batch
        embeddings = embed_texts(chunks)
        if len(embeddings) != len(chunks):
            raise RuntimeError(
                f"Embedding count mismatch: {len(chunks)} chunks "
                f"but {len(embeddings)} embeddings"
            )

        # 5. Store each chunk with its embedding
        stored = 0
        for chunk, embedding in zip(chunks, embeddings):
            try:
                self.repository.store_embedding(
                    tenant_id=tenant_id,
                    domain_id=domain_id,
                    agent_id=agent_id,
                    content=chunk,
                    title=title,
                    knowledge_type=knowledge_type,
                    source=source,
                    embedding=embedding,
                    created_by=created_by,
                )
                stored += 1
            except Exception as exc:
                logger.error("Failed to store chunk: %s", exc)

        logger.info(
            "Ingested %d/%d chunks  type=%s  tenant=%s  domain=%s  agent=%s  source=%s",
            stored, len(chunks), knowledge_type, tenant_id, domain_id, agent_id, source,
        )
        return stored