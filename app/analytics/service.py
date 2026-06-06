"""§3.9 AnalyticsService — read-only aggregate metrics (Unit 13d).

Every method here is a SELECT of AGGREGATES (counts / fractions /
percentiles / top-N) scoped ``WHERE admin_id = :admin_id``. The service
is handed a SQLAlchemy ``Session`` that is ALREADY RLS-bound (the caller
passes its TenantScoped request session, or a ``bind_tenant_scope``-wrapped
worker session) — so even the explicit admin_id filter is belt-and-
suspenders on top of the database RLS policy. No method writes; no method
returns another tenant's rows.

Tier shape (§3.9)
-----------------
* Free → BASIC subset only: total conversations this period, total leads
  captured, budget utilization.
* Pro  → the FULL metric list + conversion-by-source/channel + CSV export.

The route enforces the gate by calling :meth:`compute` with the resolved
tier; ``compute`` returns ONLY the Free subset for a Free admin (it never
errors on a Free caller — it simply omits the Pro-only metrics).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Float, String, cast, func, select
from sqlalchemy.orm import Session

from app.models.admin_audit_log import (
    ACTION_ESCALATION_ACKED,
    AdminAuditLog,
)
from app.models.escalation_event import EscalationEvent
from app.models.lead import (
    OUTCOME_CONVERTED,
    OUTCOME_IN_PROGRESS,
    OUTCOME_LOST,
    Lead,
)
from app.models.session import SessionModel
from app.models.trace import Trace
from app.policy.entitlements import TIER_FREE, TIER_PRO

logger = logging.getLogger(__name__)

# The book_appointment tool's catalog id (app/tools/implementations/
# book_appointment_tool.py::tool_id). A trace row with tool_called=True
# and tool_name == this is a booked appointment in the trace store.
_BOOK_APPOINTMENT_TOOL = "book_appointment"

# The four §3.4.5 doctrinal escalation signals the §3.9 "escalations by
# signal_type" metric reports. The capacity/availability signals
# (budget_exhausted, llm_unavailable) are NOT doctrinal triggers; they are
# reported too (all signals present are surfaced) but these four are the
# always-present keys so the shape is stable even with zero rows.
_DOCTRINAL_SIGNALS = (
    "explicit_human_request",
    "cannot_confidently_answer",
    "high_value_lead",
    "strong_negative_sentiment",
)

# Metric keys returned to EVERY tier (the Free BASIC subset).
BASIC_METRIC_KEYS = frozenset({
    "conversations",
    "leads",
    "budget_utilization",
})

# Metric keys returned ONLY to Pro (the full surface minus the basic set).
PRO_ONLY_METRIC_KEYS = frozenset({
    "escalations_by_signal",
    "escalation_first_response",
    "appointments_booked",
    "conversion",
    "channel_mix",
    "top_knowledge_sources",
    "busiest_times",
})


@dataclass(frozen=True)
class AnalyticsPeriod:
    """The window an analytics report covers.

    ``start`` is inclusive, ``end`` exclusive (both UTC). ``label`` is a
    human/file-name-friendly tag (e.g. the billing-period start ISO date
    or 'last_30d'). The route resolves this from the ``period`` query
    param (defaulting to the open billing period via resolve_billing_context).
    """

    start: datetime
    end: datetime
    label: str

    @classmethod
    def last_n_days(cls, days: int = 30, *, now: datetime | None = None) -> "AnalyticsPeriod":
        end = now or datetime.now(timezone.utc)
        return cls(start=end - timedelta(days=days), end=end, label=f"last_{days}d")


class AnalyticsService:
    """Compute §3.9 aggregate metrics for one tenant over one period."""

    def __init__(self, db: Session) -> None:
        # The session MUST already be RLS-bound to the target admin_id
        # (TenantScoped request session, or bind_tenant_scope worker
        # session). The service still filters on admin_id explicitly.
        self._db = db

    # -- individual metrics ------------------------------------------------

    def conversations(self, *, admin_id: str, period: AnalyticsPeriod) -> dict[str, int]:
        """Conversations handled this period / total (from sessions)."""
        total = self._db.execute(
            select(func.count())
            .select_from(SessionModel)
            .where(SessionModel.admin_id == admin_id)
        ).scalar_one()
        this_period = self._db.execute(
            select(func.count())
            .select_from(SessionModel)
            .where(
                SessionModel.admin_id == admin_id,
                SessionModel.created_at >= period.start,
                SessionModel.created_at < period.end,
            )
        ).scalar_one()
        return {"this_period": int(this_period), "total": int(total)}

    def leads(self, *, admin_id: str, period: AnalyticsPeriod) -> dict[str, int]:
        """Leads captured this period / total (from leads)."""
        total = self._db.execute(
            select(func.count())
            .select_from(Lead)
            .where(Lead.admin_id == admin_id)
        ).scalar_one()
        this_period = self._db.execute(
            select(func.count())
            .select_from(Lead)
            .where(
                Lead.admin_id == admin_id,
                Lead.created_at >= period.start,
                Lead.created_at < period.end,
            )
        ).scalar_one()
        return {"this_period": int(this_period), "total": int(total)}

    def escalations_by_signal(
        self, *, admin_id: str, period: AnalyticsPeriod
    ) -> dict[str, int]:
        """Count of escalation_events per signal in the period.

        The four doctrinal signals are always present (0 when none fired);
        any other signal that actually occurred is included too.
        """
        rows = self._db.execute(
            select(EscalationEvent.signal, func.count())
            .where(
                EscalationEvent.admin_id == admin_id,
                EscalationEvent.created_at >= period.start,
                EscalationEvent.created_at < period.end,
            )
            .group_by(EscalationEvent.signal)
        ).all()
        out: dict[str, int] = {s: 0 for s in _DOCTRINAL_SIGNALS}
        for signal, count in rows:
            out[signal] = int(count)
        return out

    def escalation_first_response(
        self, *, admin_id: str, period: AnalyticsPeriod
    ) -> dict[str, float | None]:
        """p50/p95 of escalation_fired → escalation_acked latency (seconds).

        The fired time is ``escalation_events.created_at``; the acked time
        is the ``admin_audit_log`` row's ``created_at`` for the matching
        ACTION_ESCALATION_ACKED event (after_json.event_id == the event id;
        the acked timestamp the audit row carries since Unit 9). Both
        tables are admin-fenced (RLS + explicit WHERE). Returns
        ``{"p50": s, "p95": s, "count": n}``; the percentiles are None when
        no acked escalation falls in the period.
        """
        # The audit row's after_json.event_id is the escalation id (stored
        # as a JSON number). Compare it as text against the event id cast
        # to text so the join is dialect-portable.
        acked_event_id = AdminAuditLog.after_json["event_id"].astext
        latency_seconds = func.extract(
            "epoch",
            AdminAuditLog.created_at - EscalationEvent.created_at,
        )

        joined = (
            select(latency_seconds.label("latency"))
            .select_from(EscalationEvent)
            .join(
                AdminAuditLog,
                (acked_event_id == cast(EscalationEvent.id, String))
                & (AdminAuditLog.admin_id == EscalationEvent.admin_id)
                & (AdminAuditLog.action == ACTION_ESCALATION_ACKED),
            )
            .where(
                EscalationEvent.admin_id == admin_id,
                EscalationEvent.created_at >= period.start,
                EscalationEvent.created_at < period.end,
            )
            .subquery()
        )

        row = self._db.execute(
            select(
                func.percentile_cont(0.5).within_group(
                    cast(joined.c.latency, Float).asc()
                ),
                func.percentile_cont(0.95).within_group(
                    cast(joined.c.latency, Float).asc()
                ),
                func.count(),
            )
        ).one()
        p50, p95, count = row
        return {
            "p50_seconds": float(p50) if p50 is not None else None,
            "p95_seconds": float(p95) if p95 is not None else None,
            "count": int(count),
        }

    def appointments_booked(
        self, *, admin_id: str, period: AnalyticsPeriod
    ) -> int:
        """Successful book_appointment tool calls in the trace store.

        A trace row with ``tool_called=True`` and
        ``tool_name='book_appointment'`` is a booked appointment — the
        trace is only written for a completed turn that produced a reply.
        """
        return int(
            self._db.execute(
                select(func.count())
                .select_from(Trace)
                .where(
                    Trace.admin_id == admin_id,
                    Trace.tool_called.is_(True),
                    Trace.tool_name == _BOOK_APPOINTMENT_TOOL,
                    Trace.created_at >= period.start,
                    Trace.created_at < period.end,
                )
            ).scalar_one()
        )

    def conversion(self, *, admin_id: str, period: AnalyticsPeriod) -> dict[str, Any]:
        """Lead → outcome conversion over leads CAPTURED in the period.

        ``rate`` is converted / (converted + lost) — the closed-deal rate,
        ignoring still-in-progress and not-yet-worked (NULL) leads — or
        None when no lead has reached a terminal outcome.
        """
        rows = self._db.execute(
            select(Lead.outcome, func.count())
            .where(
                Lead.admin_id == admin_id,
                Lead.created_at >= period.start,
                Lead.created_at < period.end,
            )
            .group_by(Lead.outcome)
        ).all()
        counts = {
            OUTCOME_CONVERTED: 0,
            OUTCOME_LOST: 0,
            OUTCOME_IN_PROGRESS: 0,
            "unset": 0,
        }
        for outcome, count in rows:
            key = outcome if outcome is not None else "unset"
            counts[key] = int(count)
        decided = counts[OUTCOME_CONVERTED] + counts[OUTCOME_LOST]
        rate = (counts[OUTCOME_CONVERTED] / decided) if decided else None
        return {"by_outcome": counts, "rate": rate}

    def channel_mix(self, *, admin_id: str, period: AnalyticsPeriod) -> dict[str, Any]:
        """Fraction of conversations per channel (spans all channels seen).

        Reads ``sessions.channel`` directly — the actual channel each
        conversation arrived on — so the mix spans every channel present
        rather than a fixed subset.
        """
        rows = self._db.execute(
            select(SessionModel.channel, func.count())
            .where(
                SessionModel.admin_id == admin_id,
                SessionModel.created_at >= period.start,
                SessionModel.created_at < period.end,
            )
            .group_by(SessionModel.channel)
        ).all()
        counts = {channel: int(count) for channel, count in rows}
        total = sum(counts.values())
        fractions = {
            channel: (count / total if total else 0.0)
            for channel, count in counts.items()
        }
        return {"counts": counts, "fractions": fractions, "total": total}

    def top_knowledge_sources(
        self, *, admin_id: str, period: AnalyticsPeriod, limit: int = 10
    ) -> list[dict[str, int]]:
        """Top-N knowledge source_ids by retrieval frequency in the window.

        ``traces.source_ids_used`` is a Postgres array of knowledge-source
        ids that contributed chunks to each turn; UNNEST + count gives the
        retrieval frequency per source.
        """
        unnested = func.unnest(Trace.source_ids_used).label("source_id")
        subq = (
            select(unnested)
            .where(
                Trace.admin_id == admin_id,
                Trace.created_at >= period.start,
                Trace.created_at < period.end,
            )
            .subquery()
        )
        rows = self._db.execute(
            select(subq.c.source_id, func.count().label("freq"))
            .group_by(subq.c.source_id)
            .order_by(func.count().desc(), subq.c.source_id.asc())
            .limit(limit)
        ).all()
        return [
            {"source_id": int(source_id), "retrievals": int(freq)}
            for source_id, freq in rows
        ]

    def busiest_times(
        self, *, admin_id: str, period: AnalyticsPeriod
    ) -> list[dict[str, int]]:
        """Conversation-volume heatmap by hour-of-day × day-of-week.

        Buckets ``sessions.created_at`` into (dow 0=Sunday..6, hour 0..23)
        cells with a count each. Only non-empty cells are returned; the
        consumer fills the grid.
        """
        dow = func.extract("dow", SessionModel.created_at)
        hour = func.extract("hour", SessionModel.created_at)
        rows = self._db.execute(
            select(dow, hour, func.count())
            .where(
                SessionModel.admin_id == admin_id,
                SessionModel.created_at >= period.start,
                SessionModel.created_at < period.end,
            )
            .group_by(dow, hour)
            .order_by(dow, hour)
        ).all()
        return [
            {"day_of_week": int(d), "hour": int(h), "count": int(c)}
            for d, h, c in rows
        ]

    def budget_utilization(self, *, admin_id: str) -> dict[str, Any]:
        """Current-period conversations vs. budget (reuse the budget meter).

        Delegates to the SAME BudgetMeter + entitlement the usage panel
        (app/api/v1/admin/usage.py) reads, summed across the admin's
        instances — never a fabricated number. Read-only.
        """
        from app.billing.metering import BudgetMeter
        from app.policy.entitlements import conversation_budget
        from app.runtime.billing_period import resolve_billing_context
        from app.services.instance_service import InstanceService

        ctx = resolve_billing_context(self._db, admin_id=admin_id)
        cap_per_instance = conversation_budget(ctx.tier, ctx.cadence)
        meter = BudgetMeter()
        instances = InstanceService(self._db).list_for_admin(
            admin_id=admin_id, active_only=False
        )
        current = 0
        for inst in instances:
            current += meter.current_count(
                admin_id=admin_id,
                instance_id=inst.id,
                period_start=ctx.period_start,
            )
        cap_total = cap_per_instance * len(instances)
        util = (round(100 * current / cap_total) if cap_total else 0)
        return {
            "tier": ctx.tier,
            "cadence": ctx.cadence,
            "billing_period_start": ctx.period_start,
            "current": current,
            "cap": cap_total,
            "cap_per_instance": cap_per_instance,
            "instance_count": len(instances),
            "utilization_pct": util,
        }

    # -- tier-shaped aggregate --------------------------------------------

    def compute(
        self,
        *,
        admin_id: str,
        tier: str,
        period: AnalyticsPeriod,
    ) -> dict[str, Any]:
        """Assemble the tier-shaped metric bundle for the period.

        Free admins get ONLY the BASIC subset. Pro admins get the full
        set. The gate is here (and re-checked at the route) so a Free
        caller can never receive a Pro-only metric.
        """
        report: dict[str, Any] = {
            "admin_id": admin_id,
            "tier": tier,
            "period": {
                "start": period.start.isoformat(),
                "end": period.end.isoformat(),
                "label": period.label,
            },
            # BASIC subset — every tier.
            "conversations": self.conversations(admin_id=admin_id, period=period),
            "leads": self.leads(admin_id=admin_id, period=period),
            "budget_utilization": self.budget_utilization(admin_id=admin_id),
        }
        if tier == TIER_FREE:
            return report

        # Pro (and any future paid tier) — the full surface.
        report.update(
            {
                "escalations_by_signal": self.escalations_by_signal(
                    admin_id=admin_id, period=period
                ),
                "escalation_first_response": self.escalation_first_response(
                    admin_id=admin_id, period=period
                ),
                "appointments_booked": self.appointments_booked(
                    admin_id=admin_id, period=period
                ),
                "conversion": self.conversion(admin_id=admin_id, period=period),
                "channel_mix": self.channel_mix(admin_id=admin_id, period=period),
                "top_knowledge_sources": self.top_knowledge_sources(
                    admin_id=admin_id, period=period
                ),
                "busiest_times": self.busiest_times(
                    admin_id=admin_id, period=period
                ),
            }
        )
        return report

    @staticmethod
    def is_pro(tier: str) -> bool:
        return tier == TIER_PRO
