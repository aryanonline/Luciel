"""
MemoryItem model.

Stores durable facts Luciel has learned about a user across sessions.
Memories are now scoped to user + tenant + agent (optional).
"""

from __future__ import annotations

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class MemoryItem(Base, TimestampMixin):
    __tablename__ = "memory_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    user_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    """Memories are scoped to user + tenant + agent so they stay isolated."""

    category: Mapped[str] = mapped_column(String(50), nullable=False)
    """What kind of memory this is (preference, constraint, goal, fact, operational)."""

    content: Mapped[str] = mapped_column(Text, nullable=False)
    """The actual memory content in plain language."""

    source_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    """Useful for debugging and tracing where a memory originated."""

    active: Mapped[bool] = mapped_column(default=True, nullable=False)
    """Memories can be soft-deleted or superseded without removing the row."""