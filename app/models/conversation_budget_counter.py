"""Conversation budget write-through ORM — Unit 13g (§3.4.1b + §4.5).

Mirrors the two tables created at
``app/migrations/versions/unit13g_budget_counter_writethrough.py``.

The §4.5 founder ruling makes Postgres the SOURCE OF TRUTH for the
conversation budget counter and Redis a cache. These two tables are the
authoritative store:

* ``ConversationBudgetCounter`` — the live per-period count, UNIQUE on
  ``(admin_id, instance_id, billing_period_start)``. ``billing_period_start``
  is the ISO date STRING the Redis key uses (e.g. ``'2026-06-01'``) so the
  two stores key on byte-identical anchors.

* ``ConversationCountedSession`` — the per-session idempotency record,
  UNIQUE on ``(admin_id, session_id)``. This row is the SINGLE authority
  for "this session has been counted" across BOTH stores; it is what
  guarantees exactly-once (no double-charge) across the Redis path and
  the Postgres path.

Walls / RLS
-----------
Both tables carry Wall-1 tenant data; both have RLS ENABLED + FORCED with
a PERMISSIVE policy fencing on ``admin_id``, mirroring
``conversation_overage_ledger`` / ``session_summaries``.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ConversationBudgetCounter(Base, TimestampMixin):
    __tablename__ = "conversation_budget_counter"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )

    # Wall-1 tenant boundary. RLS fences on this column.
    admin_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Per-instance scope. Integer (not FK) so the counter outlives an
    # instance soft-delete, mirroring the overage ledger.
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False)

    # ISO date string anchor (e.g. '2026-06-01'), byte-identical to the
    # Redis key's period_start so the two stores agree.
    billing_period_start: Mapped[str] = mapped_column(
        String(32), nullable=False
    )

    conversation_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )

    __table_args__ = (
        UniqueConstraint(
            "admin_id",
            "instance_id",
            "billing_period_start",
            name="uq_budget_counter_period",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return (
            f"<ConversationBudgetCounter admin={self.admin_id} "
            f"instance={self.instance_id} period={self.billing_period_start} "
            f"count={self.conversation_count}>"
        )


class ConversationCountedSession(Base, TimestampMixin):
    __tablename__ = "conversation_counted_sessions"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )

    # Wall-1 tenant boundary. RLS fences on this column.
    admin_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False)

    billing_period_start: Mapped[str] = mapped_column(
        String(32), nullable=False
    )

    # The conversation identifier. The UNIQUE(admin_id, session_id) row is
    # the single authority for "session counted" across BOTH stores.
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "admin_id",
            "session_id",
            name="uq_counted_sessions_admin_session",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return (
            f"<ConversationCountedSession admin={self.admin_id} "
            f"session={self.session_id} period={self.billing_period_start}>"
        )
