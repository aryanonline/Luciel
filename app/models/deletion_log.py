"""
DeletionLog model.

Immutable audit trail of every retention enforcement action.
PIPEDA requires that organizations maintain records of data
destruction for a minimum of 2 years.

These logs are NOT subject to the retention policies themselves.
"""

from __future__ import annotations

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class DeletionLog(Base, TimestampMixin):
    __tablename__ = "deletion_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    tenant_id: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    """Which tenant this action was performed for. NULL = platform-wide."""

    data_category: Mapped[str] = mapped_column(String(50), nullable=False)
    """Which data type was purged: sessions, messages, memory_items, traces."""

    action_taken: Mapped[str] = mapped_column(String(20), nullable=False)
    """What was done: 'deleted' or 'anonymized'."""

    records_affected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """How many rows were affected."""

    retention_days_applied: Mapped[int] = mapped_column(Integer, nullable=False)
    """The retention period that triggered this action."""

    triggered_by: Mapped[str] = mapped_column(String(50), nullable=False, default="scheduler")
    """Who triggered this: 'scheduler' (automated) or 'admin' (manual)."""

    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Optional reason, required for manual purges."""
