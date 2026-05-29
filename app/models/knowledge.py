"""
Knowledge chunks model.

Post-Cleanup-B (Arc 11 closeout):
    * Table is ``knowledge_chunks`` (renamed from
      ``knowledge_embeddings`` in Arc 11 Step 2).
    * Class is ``KnowledgeChunk``. The Step-2 backwards-compat alias
      ``KnowledgeEmbedding = KnowledgeChunk`` has been removed —
      every caller now references ``KnowledgeChunk`` directly.
    * ``source_id`` is now an INTEGER FK to ``knowledge_sources.id``,
      NOT NULL. The legacy stringy ``source_id`` String column and
      the orthogonal free-text ``source`` String column are both
      dropped (Cleanup B alembic migration). The relationship that
      lived under ``source_record`` (Step-2 collision-avoidance
      name) is renamed to ``source``.

Stores vector-indexed knowledge chunks. Each chunk belongs to
exactly one ``KnowledgeSource`` (the FK is mandatory) and inherits
its admin / instance / soft-delete posture from the source row.

Cleanup C removed the legacy pre-Step-24.5 ``agent_id`` column;
no read-side compat branches remain in the repository, retriever,
or ingestion modules.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from pgvector.sqlalchemy import Vector


class KnowledgeChunk(Base, TimestampMixin):
    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # ---- Scope pair ----
    admin_id: Mapped[str | None] = mapped_column(
        String(100),
        ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    # ---- Step 25b: Instance binding (Arc 5 Revision C re-pointed) ----
    luciel_instance_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "instances.id",
            ondelete="SET NULL",
            name="fk_knowledge_chunks_luciel_instance_id",
        ),
        index=True,
        nullable=False,
    )
    """The Luciel instance this chunk belongs to. Required."""

    luciel_instance: Mapped["Instance | None"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "Instance",
        lazy="select",
        foreign_keys="KnowledgeChunk.luciel_instance_id",
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

    # ---- Source binding (Cleanup B — single-FK shape) ----
    # ``source_id`` is now the BIGINT FK to ``knowledge_sources.id``,
    # NOT NULL. Pre-Cleanup-B the column had two names:
    #   * a legacy String(100) ``source_id`` (chunk-side grouping key)
    #   * an additive BIGINT ``source_fk`` introduced in Arc 11 Step 1
    # Cleanup B's migration drops the legacy String and renames
    # ``source_fk`` → ``source_id``. The relationship reclaims its
    # natural name ``source`` (was ``source_record`` to avoid the
    # collision with the now-dropped free-text ``source`` String
    # column).
    source_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "knowledge_sources.id",
            ondelete="CASCADE",
            name="fk_knowledge_chunks_source_id",
        ),
        nullable=False,
        index=True,
    )
    source: Mapped["KnowledgeSource"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "KnowledgeSource",
        back_populates="chunks",
        lazy="select",
        foreign_keys="KnowledgeChunk.source_id",
    )

    # ---- Versioning + provenance audit ----
    source_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    """Bumped each time this source is re-ingested. New version's rows
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
    """Set when a newer version of this source is ingested. NULL = active.
    Retrieval (KnowledgeRepository.search_similar) filters on
    superseded_at IS NULL."""

    # ---- Arc 10 lifecycle flags ----
    # soft_deleted_at: set when the parent instance is deactivated
    # and this chunk enters the 30-day soft-delete window
    # (Vision §6.1). The soft-delete worker physically removes
    # chunks whose soft_deleted_at is older than 30 days. Retrieval
    # excludes soft_deleted_at IS NOT NULL.
    #
    # pending_downgrade_archived_at: set when DowngradeArchiveService
    # archives this chunk's source at a Pro→Free boundary. All chunks
    # sharing a source archive together (LRU at the source level).
    # Recoverable on re-upgrade. Retrieval excludes
    # pending_downgrade_archived_at IS NOT NULL.
    soft_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pending_downgrade_archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- Audit ----
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # ---- Vector embedding (pgvector) ----
    # Declared here so SQLAlchemy persists the column on INSERT. The
    # DB column was created manually via raw SQL (pre-Step-25b); no
    # alembic migration. Do NOT run autogenerate against this table.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(1536), nullable=True
    )

    # ---- Indexes ----
    # Arc 12 EX3: ``domain_id`` was dropped (v2 scopes knowledge by
    # admin_id + luciel_instance_id only). The legacy
    # ``ix_knowledge_scope`` composite went with it; the source-
    # grouped composite is recreated without ``domain_id``.
    __table_args__ = (
        Index(
            "ix_knowledge_chunks_scope_source",
            "admin_id", "luciel_instance_id", "source_id",
        ),
    )

    # ---- Convenience ----
    @hybrid_property
    def is_active(self) -> bool:
        """True iff this chunk has not been superseded by a newer version."""
        return self.superseded_at is None

    @is_active.expression  # type: ignore[no-redef]
    def is_active(cls):  # noqa: N805  (SQLAlchemy hybrid_property convention)
        """SQL-side expression so repository code can do
        ``.filter(KnowledgeChunk.is_active)``."""
        return cls.superseded_at.is_(None)
