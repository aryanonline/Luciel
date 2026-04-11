"""
MemoryItem model.

Stores durable facts Luciel has learned about a user across sessions.
Each memory item is a single piece of knowledge, categorized by type.

Memory types:
- preference:  User preferences (e.g., "prefers 2-bedroom condos")
- constraint:  Hard limits (e.g., "budget is 700k")
- goal:        What the user is trying to achieve (e.g., "buying first home")
- fact:        Factual context (e.g., "works in downtown Toronto")
- operational: System-level notes (e.g., "user prefers concise replies")

To add new memory types later, just use a new string value.
No migration needed since the column is a free-text string.
"""

from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class MemoryItem(Base, TimestampMixin):
    __tablename__ = "memory_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Who this memory belongs to.
    # Memories are scoped to user + tenant so they stay isolated.
    user_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)

    # What kind of memory this is (preference, constraint, goal, fact, operational).
    category: Mapped[str] = mapped_column(String(50), nullable=False)

    # The actual memory content in plain language.
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Which session this memory was extracted from.
    # Useful for debugging and tracing where a memory originated.
    source_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # Whether this memory is still considered valid.
    # Memories can be soft-deleted or superseded without removing the row.
    active: Mapped[bool] = mapped_column(default=True, nullable=False)