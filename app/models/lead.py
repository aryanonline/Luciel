"""Lead ORM — Arc 14 U4 (§3.4.4 Lead Capture + §3.4.7 Summarization).

Mirrors the ``leads`` table created at
``alembic/versions/arc14_u4_leads.py``.

Lead capture is COGNITION (§3.4): always-on, every tier, NOT in the tool
registry, NOT admin-configurable. When a conversation crosses the lead
threshold (customer gave contact info, asked about a specific listing
with intent, mentioned a budget, or otherwise signals sales-qualified)
the orchestrator's COGNITION FINALIZATION step writes one structured
lead row to this table — VantageMind's OWN record that lights up the
dashboard lead view. This is distinct from the ``push_to_crm`` tool:
push_to_crm extends a captured lead OUTWARD to an external CRM; lead
capture writes the internal row push_to_crm would later read.

Captured per the U4 spec (§3.4.4):
  * ``name``               — the customer's name (best-effort; nullable).
  * ``contact_channel``    — the channel the customer is reachable on
                             (widget / email / sms).
  * ``contact_identifier`` — the address on that channel (email address,
                             phone, widget visitor id).
  * ``intent``             — the sales intent in plain language.
  * ``key_facts``          — JSONB list of the salient facts mentioned
                             (listing id, budget, timeline, etc.).
  * ``next_step``          — the recommended next action.

Summarization (§3.4.7): the same finalization step persists a structured
``summary`` alongside the lead row (no button, always-on) so the operator
who picks up the lead has the conversation recap inline.

Walls / RLS
-----------
* Wall-1 (admin) — ``admin_id`` carries the tenant boundary; the table
  has RLS ENABLED + FORCED with a PERMISSIVE policy fencing on it,
  mirroring ``escalation_events`` (Arc 14 U2) and ``sibling_call_grants``
  (Arc 12 WU4) exactly.
* Scope columns ``luciel_instance_id`` / ``session_id`` make the lead
  answerable per-instance and per-conversation.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Lead(Base, TimestampMixin):
    __tablename__ = "leads"

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
    # Scope: which Instance the lead was captured under. Nullable so a
    # turn that never resolved an instance (defensive) still records, and
    # SET NULL on delete so the lead survives instance removal (matches
    # the escalation_events / traces FK posture for this scope column).
    luciel_instance_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("instances.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    session_id: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    user_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # --- §3.4.4 structured lead fields ---
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # The channel + identifier the customer is reachable on (the contact
    # info that, when given, is itself a lead-threshold trigger).
    contact_channel: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    contact_identifier: Mapped[str | None] = mapped_column(
        String(320), nullable=True
    )
    # The sales intent in plain language ("wants to view 123 Main St").
    intent: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Salient facts the customer mentioned (listing id, budget, timeline).
    key_facts: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # The recommended next action ("schedule a viewing").
    next_step: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- §3.4.7 structured summary persisted alongside the lead ---
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "ix_leads_tenant_time",
            "admin_id",
            "created_at",
        ),
        Index(
            "ix_leads_session",
            "session_id",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return (
            f"<Lead id={self.id} admin={self.admin_id} "
            f"instance={self.luciel_instance_id} session={self.session_id}>"
        )
