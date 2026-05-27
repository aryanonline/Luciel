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
    admin_id: Mapped[str | None] = mapped_column(
        String(100),
        ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    domain_id: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    """Legacy (pre-Step-24.5). New writes use luciel_instance_id instead."""

    # ---- Step 25b: Instance binding (Arc 5 Revision C re-pointed) ----
    # Arc 9.1 Phase A (2026-05-25): NOT NULL. See arc9_1_a_tenant_isolation_seal.
    # Pre-Arc-9.1 doctrine permitted NULL for legacy/shared rows; this was
    # the P3 leak surface (204 NULL rows visible across tenants).
    luciel_instance_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "instances.id",
            ondelete="SET NULL",
            name="fk_knowledge_embeddings_luciel_instance_id",
        ),
        index=True,
        nullable=False,
    )
    """The Luciel instance this chunk belongs to. Required."""

    # Arc 5 Revision C — luciel_instance_id now FKs directly to instances.id;
    # the relationship resolves through the natural FK (no longer via the
    # legacy_luciel_instance_id back-pointer, which was dropped at Revision C).
    luciel_instance: Mapped["Instance | None"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "Instance",
        lazy="select",
        foreign_keys="KnowledgeEmbedding.luciel_instance_id",
        viewonly=True,
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

    # Arc 10 (Alembic arc10_lifecycle_subsystem) — two lifecycle flags
    # distinct from superseded_at:
    #
    # soft_deleted_at: set when the parent instance is deactivated and
    # this chunk enters the 30-day soft-delete window per Vision §6.1.
    # The Arc 10 soft-delete worker physically removes chunks whose
    # soft_deleted_at is older than 30 days. Retrieval (Arc 11 will
    # update its filter) must exclude soft_deleted_at IS NOT NULL.
    #
    # pending_downgrade_archived_at: set when DowngradeArchiveService
    # archives this chunk's source at a Pro→Free boundary. All chunks
    # sharing a source_id archive together (LRU at the source level).
    # Recoverable on re-upgrade per Customer Journey Phase 8 Pro
    # ("archived (not deleted) until he upgrades again"). Retrieval
    # must exclude pending_downgrade_archived_at IS NOT NULL.
    #
    # No knowledge_sources table exists today; the "source" is rows-
    # grouped-by-source_id on this table. The downgrade-archive 5th
    # axis (AXIS_KNOWLEDGE) operates via GROUP BY source_id.
    soft_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pending_downgrade_archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

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
        Index("ix_knowledge_scope", "admin_id", "domain_id", "knowledge_type"),
        # Step 25b: composite for fast "find chunks for source" lookups.
        # Declared in the File 2 Alembic migration with the same name; we
        # mirror it here so SQLAlchemy's metadata matches the DB.
        Index(
            "ix_knowledge_embeddings_scope_source",
            "admin_id", "domain_id", "luciel_instance_id", "source_id",
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