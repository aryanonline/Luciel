"""Lead repository — Arc 14 U4 (§3.4.4 Lead Capture + §3.4.7 Summary).

Thin persistence wrapper over the ``leads`` table. Mirrors the
``escalation_events`` posture: the orchestrator's COGNITION FINALIZATION
step (always-on cognition, §3.4) hands a fully-built ``Lead`` row here to
write. The repository does NOT decide whether a lead crossed the
threshold (that is ``app/cognition/lead_capture.detect``) — it only
persists.

The write is committed within the caller-supplied session so the lead
row + its audit row land in one transaction (matching
``EscalationService.record_escalation``). RLS on the table fences every
read/write on ``admin_id``; the repository keeps the explicit
``admin_id`` column on the row as belt-and-braces.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.lead import Lead


class LeadRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, lead: Lead) -> Lead:
        """Stage a lead row for the caller's transaction.

        Uses ``flush`` (not ``commit``) so the caller controls the
        transaction boundary — the lead row + its audit row commit
        together (mirrors ``EscalationService._write_event``). Returns
        the row with its server-assigned ``id`` populated.
        """
        self.db.add(lead)
        self.db.flush()
        return lead

    def get(self, lead_id: int, *, admin_id: str | None = None) -> Lead | None:
        """Look up a lead by primary key, optionally tenant-checked.

        When ``admin_id`` is given, the predicate is belt-and-braces
        alongside RLS — a cross-tenant id never resolves.
        """
        stmt = select(Lead).where(Lead.id == lead_id)
        if admin_id:
            stmt = stmt.where(Lead.admin_id == admin_id)
        return self.db.scalars(stmt).first()

    def list_for_session(
        self,
        session_id: str,
        *,
        admin_id: str | None = None,
        limit: int = 50,
    ) -> list[Lead]:
        """Every lead captured for a conversation, newest first."""
        stmt = (
            select(Lead)
            .where(Lead.session_id == session_id)
            .order_by(Lead.created_at.desc())
            .limit(limit)
        )
        if admin_id:
            stmt = stmt.where(Lead.admin_id == admin_id)
        return list(self.db.scalars(stmt).all())

    def list_for_tenant(
        self,
        admin_id: str,
        *,
        luciel_instance_id: int | None = None,
        limit: int = 100,
    ) -> list[Lead]:
        """Dashboard lead view: leads for a tenant, newest first."""
        stmt = (
            select(Lead)
            .where(Lead.admin_id == admin_id)
            .order_by(Lead.created_at.desc())
            .limit(limit)
        )
        if luciel_instance_id is not None:
            stmt = stmt.where(Lead.luciel_instance_id == luciel_instance_id)
        return list(self.db.scalars(stmt).all())
