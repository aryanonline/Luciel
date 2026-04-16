"""
Knowledge embeddings model.

Stores vector-indexed knowledge chunks for domain knowledge,
tenant documents, role instructions, and agent-specific knowledge.

Scoping rules:
  - domain_knowledge:   domain_id set, tenant_id NULL    → shared across all tenants in this domain.
  - tenant_document:    tenant_id set                    → private to this tenant.
  - role_instruction:   tenant_id + domain_id set        → private to this tenant/role.
  - agent_knowledge:    tenant_id + agent_id set         → private to this agent.
"""

from __future__ import annotations

from sqlalchemy import Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class KnowledgeEmbedding(Base, TimestampMixin):
    __tablename__ = "knowledge_embeddings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    tenant_id: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    domain_id: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    """These fields determine who can access this knowledge."""

    content: Mapped[str] = mapped_column(Text, nullable=False)
    """The actual text content of this knowledge chunk."""

    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    """Short title or label for this chunk (for admin display)."""

    knowledge_type: Mapped[str] = mapped_column(String(50), nullable=False)
    """What kind of knowledge this is."""

    source: Mapped[str | None] = mapped_column(String(500), nullable=True)
    """Optional source reference (e.g., filename, URL, document ID)."""

    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """--- Audit ---"""

    __table_args__ = (
        Index("ix_knowledge_scope", "tenant_id", "domain_id", "knowledge_type"),
    )
    # embedding column added manually via SQL (vector(1536))