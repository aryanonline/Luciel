"""
Trace repository.

Handles persistence for trace records.
Keeps trace storage separate from trace creation logic.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.trace import Trace


class TraceRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def save_trace(self, trace: Trace) -> Trace:
        """Persist a trace record."""
        self.db.add(trace)
        self.db.commit()
        self.db.refresh(trace)
        return trace

    def get_trace(self, trace_id: str) -> Trace | None:
        """Look up a trace by its unique trace_id."""
        stmt = select(Trace).where(Trace.trace_id == trace_id)
        return self.db.scalars(stmt).first()

    def list_traces_for_session(
        self,
        session_id: str,
        limit: int = 50,
    ) -> list[Trace]:
        """Get all traces for a session, newest first."""
        stmt = (
            select(Trace)
            .where(Trace.session_id == session_id)
            .order_by(Trace.created_at.desc())
            .limit(limit)
        )
        return list(self.db.scalars(stmt).all())