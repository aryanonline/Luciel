"""ConversationOverageLedger ORM — Arc 18 (§3.4.1b, spec §33-34).

Mirrors the ``conversation_overage_ledger`` table created at
``alembic/versions/arc18_conversation_budget_metering.py``.

Redis holds the LIVE per-instance conversation counter (ephemeral). This
table is the DURABLE billing audit trail: at cycle close (the Stripe
``invoice.paid`` webhook), the handler snapshots each instance's closed
period — conversations used, the cap, the raw overage, the rounded units
reported to Stripe, the tier/cadence in force at close, and the resulting
Stripe usage-record id — into one row here, THEN resets the counter.

A finance auditor reads this table to answer "what overage did we bill
this instance for period X, at what rate, against which Stripe usage
record?" — the question Redis cannot answer once the counter is reset.

The unique ``(admin_id, instance_id, billing_period_start)`` constraint
makes a redelivered ``invoice.paid`` webhook idempotent at the row level:
the second attempt to close the same period for the same instance
collides and is rejected, complementing the ``last_event_id`` dedup on
the Subscription row.

Walls / RLS
-----------
* Wall-1 (admin) — ``admin_id`` carries the tenant boundary; the table
  has RLS ENABLED + FORCED with a PERMISSIVE policy fencing on it,
  mirroring ``escalation_events`` / ``sibling_call_grants``.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ConversationOverageLedger(Base, TimestampMixin):
    __tablename__ = "conversation_overage_ledger"

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
    # Per-instance scope. Integer (not FK) so a closed period survives a
    # later instance soft-delete — the billing record must outlive the
    # instance it billed for.
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    # The period anchor that was reset at this close (the Redis key's
    # period_start). For paying tiers this is Stripe's current_period_start.
    billing_period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Snapshot of the close.
    conversations_used: Mapped[int] = mapped_column(Integer, nullable=False)
    budget_cap: Mapped[int] = mapped_column(Integer, nullable=False)
    overage_count: Mapped[int] = mapped_column(Integer, nullable=False)
    overage_units_reported: Mapped[int] = mapped_column(Integer, nullable=False)

    tier_at_close: Mapped[str] = mapped_column(String(16), nullable=False)
    cadence_at_close: Mapped[str] = mapped_column(String(16), nullable=False)

    # The Stripe usage record id, when one was reported. NULL when the
    # tier has no overage (Free / Enterprise per-contract) or Stripe is
    # unconfigured (documented no-op — the period still reset).
    stripe_usage_record_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    reported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "admin_id",
            "instance_id",
            "billing_period_start",
            name="uq_overage_ledger_period",
        ),
        Index(
            "ix_overage_ledger_tenant_time",
            "admin_id",
            "billing_period_start",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return (
            f"<ConversationOverageLedger id={self.id} admin={self.admin_id} "
            f"instance={self.instance_id} period={self.billing_period_start} "
            f"overage={self.overage_count} units={self.overage_units_reported}>"
        )
