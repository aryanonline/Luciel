"""
MemoryItem model.

Stores durable facts Luciel has learned about a user across sessions.
Memories are now scoped to user + tenant + agent (optional).

Step 24.5b File 2.6a additions:
- actor_user_id (nullable UUID FK -> users.id) -- the platform User
  identity whose Agent wrote this row. Distinct from the existing
  `user_id` string column (free-form end-user identifier supplied by
  the client at session creation, predates Step 24.5b). Drift D7
  resolution: the two fields coexist with distinct semantics so audit
  queries can ask both "all memory written by Sarah's platform identity"
  AND "all memory about prospect-1234" cleanly.
- actor_user relationship back-populating User.memory_items (the
  back-populate side lands in File 2.8 -- patches the User model to
  restore the relationship that was deferred from Commit 1).

Nullable in this commit; flipped to NOT NULL in Commit 3's migration
alongside agents.user_id after backfill verified clean (Invariant 12).

ON DELETE RESTRICT protects identity history -- a User cannot be
hard-deleted while their MemoryItems reference them. Soft-delete via
User.active=False is the only lifecycle path.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class MemoryItem(Base, TimestampMixin):
    __tablename__ = "memory_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    user_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    """Free-form end-user identifier supplied by the client at session
    creation. Predates Step 24.5b. NOT a FK -- this is the brokerage's
    internal ID for the human chatting (e.g. 'sarah-listings-user-1234'),
    not a platform identity row. See actor_user_id below for the
    platform identity FK."""

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

    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    luciel_instance_id: Mapped[int | None] = mapped_column(
        ForeignKey("luciel_instances.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ---- Step 24.5b File 2.6a -- platform User identity attribution ----
    # Nullable until Commit 3 backfill flips to NOT NULL alongside
    # agents.user_id (Invariant 12). Indexed for "show me all memory
    # rows attributable to User X across role changes" queries
    # (Pillar 12 will exercise this in Commit 3).
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
     # ---- Step 24.5b File 2.8 -- restored from File 2.6a deferral (D8) ----
    # Bidirectional with User.memory_items (also restored in 2.8).
    # Both sides land in the same file change so configure_mappers
    # never sees a half-resolved back_populates pair.
    actor_user: Mapped["User | None"] = relationship(
        "User",
        back_populates="memory_items",
        foreign_keys=[actor_user_id],
        lazy="select",
    )