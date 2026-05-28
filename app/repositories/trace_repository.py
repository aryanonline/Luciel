"""
Trace repository.

Handles persistence for trace records.

PATCHED:
  T1 — get_trace() now accepts optional admin_id for ownership check.
  T2 — list_traces_for_session() now accepts optional admin_id filter.
  T3 — Added list_traces_for_tenant() and list_traces_for_agent().
  T4 — Arc 11 Step 5: list_recent_traces_using_source() — read path
       for the §3.2.2 delete-confirm modal "affected questions"
       preview, keyed on the GIN index over ``traces.source_ids_used``
       (a ``BIGINT[]`` introduced in Arc 11 Step 1).
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
        admin_id: str | None = None,
    ) -> Trace | None:
        """
        Look up a trace by its unique trace_id.

        If admin_id is provided, also verifies the trace belongs
        to that tenant — preventing cross-tenant reads.
        """
        stmt = select(Trace).where(Trace.trace_id == trace_id)
        if admin_id:
            stmt = stmt.where(Trace.admin_id == admin_id)
        return self.db.scalars(stmt).first()

    def list_traces_for_session(
        self,
        session_id: str,
        *,
        admin_id: str | None = None,
        limit: int = 50,
    ) -> list[Trace]:
        """Get all traces for a session, newest first."""
        stmt = (
            select(Trace)
            .where(Trace.session_id == session_id)
            .order_by(Trace.created_at.desc())
            .limit(limit)
        )
        if admin_id:
            stmt = stmt.where(Trace.admin_id == admin_id)
        return list(self.db.scalars(stmt).all())

    def list_traces_for_tenant(
        self,
        admin_id: str,
        *,
        domain_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 100,
    ) -> list[Trace]:
        """Get traces scoped to a tenant, optionally filtered by domain/agent."""
        stmt = (
            select(Trace)
            .where(Trace.admin_id == admin_id)
            .order_by(Trace.created_at.desc())
            .limit(limit)
        )
        if domain_id:
            stmt = stmt.where(Trace.domain_id == domain_id)
        if agent_id:
            stmt = stmt.where(Trace.agent_id == agent_id)
        return list(self.db.scalars(stmt).all())

    def list_recent_traces_using_source(
        self,
        *,
        admin_id: str,
        luciel_instance_id: int,
        source_id: int,
        limit: int = 5,
    ) -> list[Trace]:
        """Recent traces whose retrieval contributed from a given source.

        Used by the Architecture v1 §3.2.2 delete-confirm modal
        preview (Step 7 builds the HTTP endpoint on top of this).

        Implementation notes:

          * L1 filter on ``admin_id``: belt + braces alongside the
            Arc 9 RLS policy on ``traces``. The repository never
            assumes the GUC is set; the explicit predicate makes
            the query semantics auditable in isolation.
          * Instance scope (``luciel_instance_id``): Wall 3 (per
            Arc 9 C4.3). A source is bound to one instance, so the
            modal preview is meaningfully scoped to that instance's
            traces.
          * The ``source_ids_used @> ARRAY[:source_id]::bigint[]``
            containment predicate uses the GIN index from Arc 11
            Step 1 (``ix_traces_source_ids_used``). Linear scans
            on a populated ``traces`` table would be unworkable;
            the GIN lookup is what makes the modal preview cheap.
          * Newest-first ordering by ``created_at DESC`` matches
            the Architecture §3.2.2 "recent" intent. The
            ``LIMIT :limit`` (default 5) matches the Customer
            Journey §4.3 modal copy ("The {N} questions about
            {topic} in the last 7 days used this source").

        Returns an empty list when no trace has ever referenced
        the source — Step 7's modal then degrades gracefully to
        the Journey §4.3 "Luciel hasn't drawn on this source in
        any customer conversations yet" copy.
        """
        # Cleanup A (Arc 11 closeout): ``Trace.source_ids_used`` is
        # now declared with ``sqlalchemy.dialects.postgresql.ARRAY``,
        # which exposes ``.contains()`` at the SQL layer. ``.contains``
        # compiles to the same ``@>`` operator + ``BIGINT[]`` cast the
        # workaround used to produce, so the GIN index from Arc 11
        # Step 1 (``ix_traces_source_ids_used``) is still picked.
        stmt = (
            select(Trace)
            .where(
                Trace.admin_id == admin_id,
                Trace.luciel_instance_id == luciel_instance_id,
                Trace.source_ids_used.contains([int(source_id)]),
            )
            .order_by(Trace.created_at.desc())
            .limit(limit)
        )
        return list(self.db.scalars(stmt).all())
