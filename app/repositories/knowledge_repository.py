"""
Knowledge repository.

Handles database operations for knowledge embeddings.

Knowledge is scoped by tenant_id, domain_id, and agent_id:
  - domain_knowledge:   domain_id set, tenant_id NULL     → shared across all tenants
  - tenant_document:    tenant_id set                     → private to this tenant
  - role_instruction:   tenant_id + domain_id set         → private to this tenant/role
  - agent_knowledge:    tenant_id + agent_id set          → private to this agent
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeEmbedding

logger = logging.getLogger(__name__)


class KnowledgeRepository:

    def __init__(self, db: Session) -> None:
        self.db = db

    def store_embedding(
        self,
        *,
        tenant_id: str | None = None,
        domain_id: str | None = None,
        agent_id: str | None = None,
        content: str,
        title: str | None = None,
        knowledge_type: str,
        source: str | None = None,
        embedding: list[float],
        created_by: str | None = None,
    ) -> KnowledgeEmbedding:
        """Store a knowledge chunk with its vector embedding."""
        item = KnowledgeEmbedding(
            tenant_id=tenant_id,
            domain_id=domain_id,
            agent_id=agent_id,
            content=content,
            title=title,
            knowledge_type=knowledge_type,
            source=source,
            created_by=created_by,
        )
        self.db.add(item)
        self.db.flush()

        # Store embedding via raw SQL (SQLAlchemy doesn't natively handle pgvector).
        self.db.execute(
            text(
                "UPDATE knowledge_embeddings SET embedding = :emb WHERE id = :id"
            ),
            {"emb": str(embedding), "id": item.id},
        )
        self.db.commit()
        self.db.refresh(item)
        return item

    def search_similar(
        self,
        *,
        query_embedding: list[float],
        tenant_id: str | None = None,
        domain_id: str | None = None,
        agent_id: str | None = None,
        knowledge_type: str | None = None,
        limit: int = 5,
    ) -> list[dict]:
        """
        Find the most semantically similar knowledge chunks.

        Scoping rules:
          - Always includes rows where tenant_id IS NULL (global/domain knowledge).
          - If tenant_id is provided, also includes that tenant's rows.
          - If domain_id is provided, also includes that domain's rows.
          - If agent_id is provided, also includes that agent's rows.
          - This means an agent-level query sees: global + tenant + domain + agent knowledge.
        """
        conditions = []
        params = {
            "query_emb": str(query_embedding),
            "limit": limit,
        }

        # Only search rows that have embeddings.
        conditions.append("embedding IS NOT NULL")

        # Tenant scoping: include tenant's rows + global rows (tenant_id IS NULL).
        if tenant_id:
            conditions.append("(tenant_id = :tenant_id OR tenant_id IS NULL)")
            params["tenant_id"] = tenant_id
        else:
            conditions.append("tenant_id IS NULL")

        # Domain scoping: include domain's rows + rows without domain (domain_id IS NULL).
        if domain_id:
            conditions.append("(domain_id = :domain_id OR domain_id IS NULL)")
            params["domain_id"] = domain_id

        # Agent scoping: include agent's rows + rows without agent (agent_id IS NULL).
        if agent_id:
            conditions.append("(agent_id = :agent_id OR agent_id IS NULL)")
            params["agent_id"] = agent_id
        else:
            conditions.append("(agent_id IS NULL)")

        if knowledge_type:
            conditions.append("knowledge_type = :knowledge_type")
            params["knowledge_type"] = knowledge_type

        where_clause = " AND ".join(conditions)

        query = text(f"""
            SELECT id, content, title, knowledge_type, tenant_id, domain_id, agent_id, source,
                   embedding <=> cast(:query_emb as vector) AS distance
            FROM knowledge_embeddings
            WHERE {where_clause}
            ORDER BY distance ASC
            LIMIT :limit
        """)

        try:
            results = self.db.execute(query, params).fetchall()
            return [
                {
                    "id": row.id,
                    "content": row.content,
                    "title": row.title,
                    "knowledge_type": row.knowledge_type,
                    "tenant_id": row.tenant_id,
                    "domain_id": row.domain_id,
                    "agent_id": row.agent_id,
                    "source": row.source,
                    "distance": row.distance,
                }
                for row in results
            ]
        except Exception as exc:
            logger.warning("Vector search failed: %s", exc)
            self.db.rollback()
            return []

    def delete_by_source(
        self,
        *,
        tenant_id: str,
        source: str,
    ) -> int:
        """Delete all knowledge chunks from a specific source."""
        result = self.db.execute(
            text(
                "DELETE FROM knowledge_embeddings "
                "WHERE tenant_id = :tenant_id AND source = :source"
            ),
            {"tenant_id": tenant_id, "source": source},
        )
        self.db.commit()
        return result.rowcount