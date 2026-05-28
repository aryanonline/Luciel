"""KnowledgeSource ORM model — Arc 11 Step 1.

One row per uploaded / pasted / crawled knowledge source. Replaces the
implicit "source = rows-grouped-by-string-source_id on
knowledge_embeddings" representation that predated Arc 11 (see the
note in app/models/knowledge.py). Provenance and lifecycle live here;
chunks live on the ``knowledge_chunks`` table (renamed from
``knowledge_embeddings`` in Arc 11 Step 2) and FK back via
``source_fk``.

Anchored to:
  * Architecture v1 §3.2 (Knowledge Subsystem — two-table model).
  * ARC11_PLAN.md §2.1 for the column / index shape.

Step-1 scope (this file): the model exists and is registered, but no
service code reads or writes it yet. Step 3 wires the repository,
ingestion, and retriever to the new table.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class KnowledgeSource(Base):
    __tablename__ = "knowledge_sources"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )

    # ---- Identity ----
    source_uuid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        unique=True,
        server_default=text("gen_random_uuid()"),
    )
    """Stable external identifier. Surfaced in API responses; the
    integer ``id`` is internal."""

    # ---- Tenant scope ----
    admin_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=False,
    )
    luciel_instance_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("instances.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # ---- Provenance ----
    filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Original filename for uploaded sources. NULL for paste-text."""

    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    """pdf | docx | txt | csv | paste | crawl."""

    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    """Bytes counted at ingest. Drives the per-file and total quota
    checks at the API boundary."""

    s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    """S3 key under the knowledge bucket. NULL for paste-text."""

    origin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Populated for crawl sources."""

    # ---- Ingestion lifecycle ----
    ingestion_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending"
    )
    """pending | processing | ready | failed."""

    ingestion_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    ingested_by: Mapped[str] = mapped_column(Text, nullable=False)
    """User id of the team member who initiated the ingest."""

    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    last_viewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """Updated by the raw-view list endpoint each time the source is
    surfaced in the admin UI."""

    # ---- Versioning ----
    source_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    """Bumped on re-ingest. Older chunk rows get ``superseded_at`` set."""

    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- Soft delete ----
    soft_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- Downgrade archive (Arc 10 pattern preserved) ----
    pending_downgrade_archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- Audit ----
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    # ---- Relationships ----
    # ``back_populates="source_record"`` because the KnowledgeChunk
    # side already has a legacy free-text ``source`` string column; the
    # relationship lives under ``source_record`` until the legacy
    # column is dropped in Arc 11 Step 11. ``KnowledgeChunk`` is the
    # Arc-11-Step-2 rename of ``KnowledgeEmbedding``; the latter
    # remains as a module-level alias so existing imports still work.
    chunks: Mapped[list["KnowledgeChunk"]] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "KnowledgeChunk",
        back_populates="source_record",
        lazy="select",
        foreign_keys="KnowledgeChunk.source_fk",
    )

    # ---- Indexes / constraints — mirror the migration. ----
    __table_args__ = (
        CheckConstraint(
            "ingestion_status IN ('pending','processing','ready','failed')",
            name="ck_knowledge_sources_ingestion_status_valid",
        ),
        Index(
            "ix_knowledge_sources_tenant_scope",
            "admin_id",
            "luciel_instance_id",
        ),
        Index(
            "ix_knowledge_sources_status",
            "admin_id",
            "luciel_instance_id",
            "ingestion_status",
            postgresql_where=text("soft_deleted_at IS NULL"),
        ),
        Index(
            "ix_knowledge_sources_soft_delete",
            "soft_deleted_at",
            postgresql_where=text("soft_deleted_at IS NOT NULL"),
        ),
    )
