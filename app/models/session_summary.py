"""SessionSummary ORM model — Unit 13e §3.4.10 cross-session memory store.

§3.4.10 specifies a PERSISTED session summary as the cross-session memory
store, with its own retention (90 days Free / 1 year Pro, stays in
Postgres). Before Unit 13e, summaries were folded into the ``leads.summary``
column only — there was no dedicated store keyed on the participant
(resolved_lead_id) with its own retention clock. This model is that store.

It is written by the finalization pipeline at session end (the §3.4.7
summarization moment) and is the proper source the cross-session retriever
reads (the message-based reader stays as the v1 raw-history leg; this store
is the summary leg with its own retention TTL).

Tenant scoping + RLS (§3.7.5):
  Holds lead-derived content, so it is tenant-scoped on ``admin_id`` with a
  PERMISSIVE + FORCE RLS policy — identical to ``leads`` (arc14_u4). An
  isolation test (tests/db/test_unit13e_session_summary_isolation.py)
  proves tenant A cannot read tenant B's summaries.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class SessionSummary(Base, TimestampMixin):
    __tablename__ = "session_summaries"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    admin_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    luciel_instance_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("instances.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # §3.4.8 session-key participant id this summary belongs to. NULL =
    # anonymous session (the summary still persists, but it is not a
    # cross-session recall anchor since an anonymous participant never
    # matches another, §3.4.9).
    resolved_lead_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True,
    )
    session_id: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True,
    )
    summary: Mapped[str] = mapped_column(Text(), nullable=False)
