"""
Trace repository.

Handles persistence for trace records.

PATCHED:
  T1 — get_trace() now accepts optional tenant_id for ownership check.
  T2 — list_traces_for_session() now accepts optional tenant_id filter.
  T3 — Added list_traces_for_tenant() and list_traces_for_agent().
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

    def get_trace(
        self,
        trace_id: str,
        *,
        tenant_id: str | None = None,
    ) -> Trace | None:
        """
        Look up a trace by its unique trace_id.

        If tenant_id is provided, also verifies the trace belongs
        to that tenant — preventing cross-tenant reads.
        """
        stmt = select(Trace).where(Trace.trace_id == trace_id)
        if tenant_id:
            stmt = stmt.where(Trace.tenant_id == tenant_id)
        return self.db.scalars(stmt).first()

    def list_traces_for_session(
        self,
        session_id: str,
        *,
        tenant_id: str | None = None,
        limit: int = 50,
    ) -> list[Trace]:
        """Get all traces for a session, newest first."""
        stmt = (
            select(Trace)
            .where(Trace.session_id == session_id)
            .order_by(Trace.created_at.desc())
            .limit(limit)
        )
        if tenant_id:
            stmt = stmt.where(Trace.tenant_id == tenant_id)
        return list(self.db.scalars(stmt).all())

    def list_traces_for_tenant(
        self,
        tenant_id: str,
        *,
        domain_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 100,
    ) -> list[Trace]:
        """Get traces scoped to a tenant, optionally filtered by domain/agent."""
        stmt = (
            select(Trace)
            .where(Trace.tenant_id == tenant_id)
            .order_by(Trace.created_at.desc())
            .limit(limit)
        )
        if domain_id:
            stmt = stmt.where(Trace.domain_id == domain_id)
        if agent_id:
            stmt = stmt.where(Trace.agent_id == agent_id)
        return list(self.db.scalars(stmt).all())
