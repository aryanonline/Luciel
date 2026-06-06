"""EscalationEvent ORM — Arc 14 U2 (§3.4.5 Escalation Judgment Module).

Mirrors the ``escalation_events`` table created at
``alembic/versions/arc14_u2_escalation_events.py``.

Every escalation decision the §3.4.5 module makes writes exactly one
row here. The row is the durable forensic record an operator (and a
compliance auditor) reads to answer "why did Luciel hand this turn to a
human, and on what evidence?" The doctrinal triggers are NOT
admin-configurable — the four signals + their thresholds are fixed in
code — so this table records WHICH fixed signal fired and the inputs
that led to the call, never a per-tenant rule.

Captured per the U2 spec:
  * ``signal``            — which of the four fixed signals fired.
  * ``gate``              — INTAKE (pre-PLAN) or OUTCOME (post-REFLECT).
  * ``signal_confidence`` — the firing signal's confidence/score.
  * ``reasoning_excerpt`` — a short model-reasoning / decision excerpt.
  * ``signal_inputs``     — JSONB of the raw inputs the judge saw
                            (the message, classifier outputs, loop
                            confidence, grounding, etc.).
  * scope ``(admin_id, luciel_instance_id, session_id)`` + ``user_id``.
  * ``created_at``        — the timestamp (TimestampMixin).

Walls / RLS
-----------
* Wall-1 (admin) — ``admin_id`` carries the tenant boundary; the table
  has RLS ENABLED + FORCED with a PERMISSIVE policy fencing on it,
  mirroring ``sibling_call_grants`` (Arc 12 WU4) exactly.
* Scope columns ``luciel_instance_id`` / ``session_id`` make the event
  answerable per-instance and per-conversation.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# Rescan Tier-C — delivery status literals for exactly-once idempotency.
DELIVERY_STATUS_PENDING = "pending"
DELIVERY_STATUS_DELIVERED = "delivered"
DELIVERY_STATUS_ACKED = "acked"
DELIVERY_STATUS_FAILED = "failed"

ALLOWED_DELIVERY_STATUSES: frozenset[str] = frozenset({
    DELIVERY_STATUS_PENDING,
    DELIVERY_STATUS_DELIVERED,
    DELIVERY_STATUS_ACKED,
    DELIVERY_STATUS_FAILED,
})


# ---------------------------------------------------------------------
# Signal + gate literals — module constants so the judge, the service,
# the tests, and the CHECK constraint share one source of truth. The
# four signals are the §3.4.5 doctrinal triggers (NOT admin-configurable).
# ---------------------------------------------------------------------

# Gate 1 — INTAKE (pre-PLAN), knowable from the inbound message alone.
SIGNAL_EXPLICIT_HUMAN_REQUEST = "explicit_human_request"
SIGNAL_STRONG_NEGATIVE_SENTIMENT = "strong_negative_sentiment"
# Gate 2 — OUTCOME (post-REFLECT), needs the loop output.
SIGNAL_CANNOT_CONFIDENTLY_ANSWER = "cannot_confidently_answer"
SIGNAL_HIGH_VALUE_LEAD = "high_value_lead"
# Arc 18 — fires at GATE_INTAKE (pre-PLAN) when a Free instance is at/over
# its conversation budget. The session is gracefully handled WITHOUT an
# LLM call: a cannot_answer-style handoff with reason code
# budget_exhausted (§3.4.1b). NOT one of the four §3.4.5 doctrinal
# triggers — it is a capacity condition, surfaced via the same escalation
# machinery so the audit + notify side-effects reuse one path.
SIGNAL_BUDGET_EXHAUSTED = "budget_exhausted"
# Unit 9 (part 2) — fires at GATE_OUTCOME (post-loop) when BOTH LLM
# providers are down (router raises "All LLM providers failed"). Architecture
# line 1354: Luciel returns the canonical "I've let the team know" phrase,
# escalates, and notifies the admin rather than fabricating a response to
# mask provider unavailability. Like budget_exhausted it is not one of the
# four §3.4.5 doctrinal triggers — it is an availability condition surfaced
# via the same escalation machinery.
SIGNAL_LLM_UNAVAILABLE = "llm_unavailable"

ALLOWED_SIGNALS: frozenset[str] = frozenset({
    SIGNAL_EXPLICIT_HUMAN_REQUEST,
    SIGNAL_STRONG_NEGATIVE_SENTIMENT,
    SIGNAL_CANNOT_CONFIDENTLY_ANSWER,
    SIGNAL_HIGH_VALUE_LEAD,
    SIGNAL_BUDGET_EXHAUSTED,
    SIGNAL_LLM_UNAVAILABLE,
})

GATE_INTAKE = "intake"
GATE_OUTCOME = "outcome"

ALLOWED_GATES: frozenset[str] = frozenset({GATE_INTAKE, GATE_OUTCOME})


class EscalationEvent(Base, TimestampMixin):
    __tablename__ = "escalation_events"

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
    # Scope: which Instance the escalation happened under. Nullable so a
    # turn that never resolved an instance (defensive) still records.
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

    # WHICH signal fired + at which gate.
    signal: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    gate: Mapped[str] = mapped_column(String(16), nullable=False)

    # The firing signal's confidence / score (intent confidence,
    # sentiment magnitude, loop confidence, lead score — normalised by
    # the judge into a single comparable float for the record).
    signal_confidence: Mapped[float | None] = mapped_column(nullable=True)

    # A short model-reasoning / decision excerpt explaining the call.
    reasoning_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The raw inputs the judge evaluated (message, classifier outputs,
    # loop confidence, grounding, retrieval-failure flag, etc.).
    signal_inputs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Rescan Tier-C — delivery lifecycle columns (§3.5 notification delivery).
    # ``delivery_status`` tracks exactly-once idempotency: pending →
    # delivered/acked/failed. The partial unique index on (session_id, signal,
    # gate) WHERE delivery_status IN ('delivered','acked') prevents duplicate
    # deliveries on replay. See migration rescanc_escalation_delivery.
    delivery_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=DELIVERY_STATUS_PENDING,
        comment="Rescan Tier-C §3.5 delivery lifecycle: pending/delivered/acked/failed.",
    )
    # Cumulative send attempts across all channels for this event.
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
        comment="Rescan Tier-C §3.5 — cumulative send attempts.",
    )
    # Timestamp of the most recent send attempt.
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Rescan Tier-C §3.5 — timestamp of the last send attempt.",
    )

    __table_args__ = (
        CheckConstraint(
            "signal IN ("
            "'explicit_human_request', 'strong_negative_sentiment', "
            "'cannot_confidently_answer', 'high_value_lead', "
            "'budget_exhausted', 'llm_unavailable')",
            name="ck_escalation_events_signal",
        ),
        CheckConstraint(
            "gate IN ('intake', 'outcome')",
            name="ck_escalation_events_gate",
        ),
        Index(
            "ix_escalation_events_tenant_time",
            "admin_id",
            "created_at",
        ),
        Index(
            "ix_escalation_events_session",
            "session_id",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return (
            f"<EscalationEvent id={self.id} admin={self.admin_id} "
            f"instance={self.luciel_instance_id} gate={self.gate} "
            f"signal={self.signal}>"
        )
