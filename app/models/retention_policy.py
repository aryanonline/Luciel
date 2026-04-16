"""
RetentionPolicy model.

Defines how long each category of personal data is kept
before being deleted or anonymized. Required for PIPEDA
Principle 4.5 (Limiting Use, Disclosure, and Retention).

Scoping:
  - tenant_id = NULL  →  platform-wide default
  - tenant_id set     →  tenant-specific override
"""

from __future__ import annotations

from sqlalchemy import Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class RetentionPolicy(Base, TimestampMixin):
    __tablename__ = "retention_policies"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    tenant_id: Mapped[str | None] = mapped_column(
        String(100), index=True, nullable=True,
    )
    """NULL = platform default. Set = tenant override."""

    data_category: Mapped[str] = mapped_column(String(50), nullable=False)
    """Which table/data type: sessions, messages, memory_items, traces, knowledge_embeddings."""

    retention_days: Mapped[int] = mapped_column(Integer, nullable=False)
    """How many days to keep records after created_at. 0 = no auto-purge."""

    action: Mapped[str] = mapped_column(String(20), nullable=False, default="delete")
    """What to do when retention expires: 'delete' or 'anonymize'."""

    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    """PIPEDA requires documenting the purpose for retaining this data."""

    __table_args__ = (
        UniqueConstraint("tenant_id", "data_category", name="uq_retention_tenant_category"),
    )
