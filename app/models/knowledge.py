"""
Knowledge embeddings model.

Stores vector-indexed knowledge chunks for domain knowledge,
tenant documents, role instructions, and Luciel-instance-specific knowledge.

Scoping rules (legacy tenant/domain/agent triple retained for reads of
pre-Step-25b rows; new writes bind to luciel_instance_id):

    - domain_knowledge:   domain_id set, tenant_id NULL   -> shared across all tenants in this domain.
    - tenant_document:    tenant_id set                   -> private to this tenant.
    - role_instruction:   tenant_id + domain_id set       -> private to this tenant/role.
    - agent_knowledge:    tenant_id + agent_id set        -> LEGACY: private to this agent (pre-Step-24.5).
    - luciel_knowledge:   tenant_id + luciel_instance_id  -> Step 25b: knowledge attached to a specific Luciel.

PATCHED (Step 25b):
    - luciel_instance_id FK to luciel_instances.id (ON DELETE SET NULL — preserves history).
    - source_id / source_version / source_filename / source_type / ingested_by:
      versioning + audit columns. Enables "replace by source_id" workflow.
    - superseded_at: soft-supersede column. is_active = (superseded_at IS NULL).
    - Composite index ix_knowledge_embeddings_scope_source on
      (tenant_id, domain_id, luciel_instance_id, source_id) for fast
      replace_by_source_id lookups in the repository (File 8).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from pgvector.sqlalchemy import Vector

class KnowledgeEmbedding(Base, TimestampMixin):
    __tablename__ = "knowledge_embeddings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # ---- Scope triple (legacy + new) ----
    tenant_id: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    domain_id: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    """Legacy (pre-Step-24.5). New writes use luciel_instance_id instead."""

    # ---- Step 25b: Luciel-instance binding ----
    luciel_instance_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "luciel_instances.id",
            ondelete="SET NULL",
            name="fk_knowledge_embeddings_luciel_instance_id",
        ),
        index=True,
        nullable=True,
    )
    """The Luciel instance this chunk belongs to (Step 25b writes set this).
    NULL for legacy pre-Step-25b rows and for domain/tenant-level shared
    knowledge attached only via the scope triple."""

    luciel_instance: Mapped["LucielInstance | None"] = relationship(  # type: ignore[name-defined]
        "LucielInstance",
        lazy="select",
        foreign_keys=[luciel_instance_id],
    )

    # ---- Content ----
    content: Mapped[str] = mapped_column(Text, nullable=False)
    """The actual text content of this knowledge chunk."""

    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    """Short title or label for this chunk (for admin display)."""

    knowledge_type: Mapped[str] = mapped_column(String(50), nullable=False)
    """What kind of knowledge this is: domain_knowledge | tenant_document |
    role_instruction | agent_knowledge | luciel_knowledge."""

    source: Mapped[str | None] = mapped_column(String(500), nullable=True)
    """Optional source reference (e.g., filename, URL, document ID).
    Free-form, human-readable. For machine-level "find all chunks for this
    upload" semantics, use source_id instead."""

    # ---- Step 25b: versioning + audit ----
    source_id: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    """Stable per-upload identifier. All chunks derived from the same ingest
    call share one source_id, so replace_by_source_id can find and supersede
    them atomically. NULL for legacy rows."""

    source_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    """Bumped each time this source_id is re-ingested. New version's rows
    are inserted; old version's rows get superseded_at set."""

    source_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    """Original filename of the uploaded document (e.g., 'Q3_report.pdf')."""

    source_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    """Detected/declared source type: txt | md | html | pdf | docx | csv | json."""

    ingested_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """Actor label of the key that performed the ingest (from request.state.actor_label)."""

    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """Set when a newer version of this source_id is ingested. NULL = active.
    Retrieval (File 11) filters on superseded_at IS NULL."""

    # ---- Audit ----
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """--- Audit ---"""
    
    # ---- Vector embedding (pgvector) ----
    # Declared here (Step 25b, File 11 fix) so SQLAlchemy actually
    # persists the column on INSERT. The DB column was already created
    # manually via raw SQL (pre-Step-25b); no Alembic migration needed.
    # DO NOT run `alembic revision --autogenerate` against this table —
    # autogenerate still mis-handles vector(1536) dim arg on some
    # SQLAlchemy/pgvector combinations.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(1536), nullable=True
    )

    # ---- Indexes ----
    __table_args__ = (
        Index("ix_knowledge_scope", "tenant_id", "domain_id", "knowledge_type"),
        # Step 25b: composite for fast "find chunks for source" lookups.
        # Declared in the File 2 Alembic migration with the same name; we
        # mirror it here so SQLAlchemy's metadata matches the DB.
        Index(
            "ix_knowledge_embeddings_scope_source",
            "tenant_id", "domain_id", "luciel_instance_id", "source_id",
        ),
    )
    # NOTE: `embedding` column is pgvector vector(1536), added manually via
    # SQL outside Alembic. Do not declare it here — SQLAlchemy core does not
    # know the `vector` type, and autogenerate would try to drop it.

    # ---- Convenience ----
    @hybrid_property
    def is_active(self) -> bool:
        """True iff this chunk has not been superseded by a newer version."""
        return self.superseded_at is None

    @is_active.expression  # type: ignore[no-redef]
    def is_active(cls):  # noqa: N805  (SQLAlchemy hybrid_property convention)
        """SQL-side expression so repository code can do
        `.filter(KnowledgeEmbedding.is_active)`."""
        return cls.superseded_at.is_(None)