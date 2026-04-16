"""
Data retention models.

RetentionPolicy — defines how long each category of data is kept
and what action to take when it expires (delete or anonymize).

DeletionLog — immutable audit trail of every purge/anonymize action.
Kept for a minimum of 2 years per PIPEDA breach record requirements.

Policies can be platform-wide (tenant_id IS NULL) or tenant-specific.
Tenant-specific policies override platform defaults.
"""
from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class RetentionPolicy(Base, TimestampMixin):
    __tablename__ = "retention_policies"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    tenant_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True,
    )
    """NULL = platform-wide default. Set = tenant-specific override."""

    data_category: Mapped[str] = mapped_column(
        String(50), nullable=False,
    )
    """
    Which data this policy applies to.
    Valid values: sessions, messages, memory_items, traces, knowledge_embeddings
    """

    retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False,
    )
    """Number of days to keep data after creation. 0 = no auto-purge."""

    action: Mapped[str] = mapped_column(
        String(20), nullable=False, default="anonymize",
    )
    """What to do when data expires. 'delete' or 'anonymize'."""

    purpose: Mapped[str | None] = mapped_column(
        Text, nullable=True,
    )
    """PIPEDA requires documenting the purpose for retention. Audit field."""

    active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False,
    )

    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "data_category",
            name="uq_retention_tenant_category",
        ),
    )


class DeletionLog(Base, TimestampMixin):
    __tablename__ = "deletion_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    tenant_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True,
    )

    data_category: Mapped[str] = mapped_column(
        String(50), nullable=False,
    )
    """Which data category was purged."""

    action_taken: Mapped[str] = mapped_column(
        String(20), nullable=False,
    )
    """'deleted' or 'anonymized'."""

    rows_affected: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )

    cutoff_date: Mapped[str] = mapped_column(
        String(50), nullable=False,
    )
    """ISO date string — records older than this were affected."""

    triggered_by: Mapped[str] = mapped_column(
        String(50), nullable=False, default="scheduler",
    )
    """'scheduler' for automatic, 'admin:<user>' for manual purge."""

    reason: Mapped[str | None] = mapped_column(
        Text, nullable=True,
    )
    """Optional reason, required for manual purges."""