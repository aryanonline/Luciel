"""DashboardService -- scope-bound hierarchical rollups for Step 31.

Three read-only methods that produce the aggregates the operator-facing
dashboard renders (and that a future Step 32 frontend reads):

  * get_tenant_dashboard(tenant_id)  -> TenantDashboard
  * get_domain_dashboard(tenant_id, domain_id) -> DomainDashboard
  * get_agent_dashboard(tenant_id, domain_id, agent_id) -> AgentDashboard

Architectural pins
==================

* No new DB writes. The service reads `traces`, the scope tables
  (`tenant_configs`, `domain_configs`, `agents`, `luciel_instances`),
  and the §3.2.11 identity tables -- nothing else. The
  widget-chat audit-log emissions added in Step 31 sub-branch 1 give
  the operator a second, log-shaped read path for the same data; the
  DB path here is for routine "is Luciel earning its keep?"
  introspection and the log path is for forensic / regulator use.

* Defense-in-depth scope enforcement (mirrors §3.2.11's
  CrossSessionRetriever discipline):
    1. The SQL WHERE clause filters by the resolved scope.
    2. A post-query loop re-asserts the scope on every materialised
       row and drops (with ERROR log) any row that ever slipped
       through. The post-query loop is a defensive belt over the
       suspenders; both layers must agree or we refuse the row.

  The CALLER's scope (resolved at the HTTP layer via ScopePolicy)
  is the upper bound. A tenant-admin's call cannot widen into another
  tenant; the dashboard service trusts that the HTTP-layer
  ScopePolicy gate has already enforced the upper bound and queries
  bounded by what the caller asked for. Service-level callers
  (tests, internal scripts) MUST therefore pass already-validated
  scope ids -- this is the same trust contract as
  SessionService.create_session_with_identity.

* Aggregates surfaced (v1) per ARCHITECTURE.md §3.2.12:
    - turn_count           (count of traces in window)
    - unique_user_count    (distinct traces.user_id, NULL excluded)
    - escalation_count     (traces.escalated=True)
    - tool_call_count      (traces.tool_called=True)
    - moderation_block_count is roadmap -- the moderation block path
      doesn't write a trace row today (it short-circuits before the
      LLM call); the field is declared on the dataclasses so a future
      moderation-trace landing surfaces it without a shape change.
      The v1 value is always 0 until that landing.
    - top-N children at one level below the dashboard's scope
    - seven-day trend (per-day turn counts, oldest first)

  Latency p50/p95 is a roadmap field for the same reason as
  moderation_block_count: the trace table has no latency_ms column
  today. The sub-branch 1 widget-chat completion log carries
  latency_ms in the application log stream, and a future trace
  schema bump will surface it on the trace row. v1 returns None for
  both p50 and p95 so the dataclass shape is forward-stable.

* Window. The default look-back is 7 days. The trend line is exactly
  the 7 day buckets; the headline counts are aggregates over the
  same 7-day window so the operator sees a consistent picture (the
  trend sums to the headline). A `since` argument is supported so
  the harness in sub-branch 4 can pin a deterministic seeded window.

* Top-N. N defaults to 5; the dataclass carries every child the
  query returned so a caller asking for top-3 still gets a 3-row
  payload and a caller asking for top-10 gets up to 10. The
  ordering is turn_count DESC then the child's id ASC as a stable
  tiebreaker.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import Integer, and_, func, select
from sqlalchemy.orm import Session

from app.models.agent import Agent
from app.models.domain_config import DomainConfig
from app.models.luciel_instance import LucielInstance
from app.models.trace import Trace

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------- #
# Default window / top-N. Both are tunable via kwargs but the defaults
# are the v1 operator-facing contract.
# --------------------------------------------------------------------- #

DEFAULT_TREND_DAYS = 7
DEFAULT_TOP_N = 5


# --------------------------------------------------------------------- #
# Result dataclasses (frozen). Mirroring the Step 24.5c precedent of
# SessionWithIdentity: the service returns a typed payload, not a dict,
# so a future frontend / e2e harness can rely on the shape.
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScopeAggregates:
    """Headline rollup for a single scope.

    `moderation_block_count` and `latency_p50_ms`/`latency_p95_ms` are
    intentional v1 placeholders -- see module docstring. They carry
    the v1 contract value (0 / None) but their presence on the
    dataclass freezes the shape so a later landing of trace-row
    latency / moderation traces does NOT break consumers.
    """

    turn_count: int
    unique_user_count: int
    escalation_count: int
    tool_call_count: int
    moderation_block_count: int  # v1 = 0; see module docstring.
    latency_p50_ms: int | None  # v1 = None; trace row has no latency column yet.
    latency_p95_ms: int | None  # v1 = None.


@dataclass(frozen=True)
class TrendBucket:
    """One day's worth of turn count, for the seven-day trend line."""

    day: str  # ISO-8601 date (YYYY-MM-DD) in UTC.
    turn_count: int


@dataclass(frozen=True)
class ChildRollup:
    """One child entity's headline aggregates, for the top-N list."""

    child_id: str
    child_display_name: str | None
    turn_count: int
    unique_user_count: int


@dataclass(frozen=True)
class TenantDashboard:
    """Aggregates over every domain and every agent under a tenant.

    top_domains and top_luciel_instances are the most-active children
    at the two scope levels DIRECTLY below the tenant. Agents are not
    listed at the tenant level by design -- the operator drills down
    into a domain first.
    """

    tenant_id: str
    window_days: int
    aggregates: ScopeAggregates
    trend: list[TrendBucket]
    top_domains: list[ChildRollup] = field(default_factory=list)
    top_luciel_instances: list[ChildRollup] = field(default_factory=list)


@dataclass(frozen=True)
class DomainDashboard:
    tenant_id: str
    domain_id: str
    window_days: int
    aggregates: ScopeAggregates
    trend: list[TrendBucket]
    top_agents: list[ChildRollup] = field(default_factory=list)


@dataclass(frozen=True)
class AgentDashboard:
    tenant_id: str
    domain_id: str
    agent_id: str
    window_days: int
    aggregates: ScopeAggregates
    trend: list[TrendBucket]
    top_luciel_instances: list[ChildRollup] = field(default_factory=list)


# --------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------- #


class DashboardService:
    """Read-only rollup service.

    Constructed with a SQLAlchemy `Session`. No repository wrapping at
    v1 -- the queries are inline because (a) they're inherently
    cross-table and (b) wrapping each one in a single-call repository
    method would just be a rename layer. If the service grows past
    three methods we revisit.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    # ----------------------------------------------------------------- #
    # Public API
    # ----------------------------------------------------------------- #

    def get_tenant_dashboard(
        self,
        tenant_id: str,
        *,
        window_days: int = DEFAULT_TREND_DAYS,
        top_n: int = DEFAULT_TOP_N,
        since: datetime | None = None,
    ) -> TenantDashboard:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        window_start = self._window_start(window_days, since)

        base_filter = [
            Trace.tenant_id == tenant_id,
            Trace.created_at >= window_start,
        ]
        aggregates = self._aggregates(base_filter, scope_tag=("tenant", tenant_id))
        trend = self._trend(base_filter, window_days, window_start)
        top_domains = self._top_children(
            base_filter=base_filter + [Trace.domain_id.is_not(None)],
            group_col=Trace.domain_id,
            display_lookup=self._domain_display_lookup(tenant_id),
            top_n=top_n,
            scope_tag=("tenant.top_domains", tenant_id),
            scope_check=lambda row_id: row_id is not None,
        )
        top_instances = self._top_children(
            base_filter=base_filter + [Trace.luciel_instance_id.is_not(None)],
            group_col=Trace.luciel_instance_id,
            display_lookup=self._instance_display_lookup_for_tenant(tenant_id),
            top_n=top_n,
            scope_tag=("tenant.top_instances", tenant_id),
            scope_check=lambda row_id: row_id is not None,
            id_cast=str,
        )

        return TenantDashboard(
            tenant_id=tenant_id,
            window_days=window_days,
            aggregates=aggregates,
            trend=trend,
            top_domains=top_domains,
            top_luciel_instances=top_instances,
        )

    def get_domain_dashboard(
        self,
        tenant_id: str,
        domain_id: str,
        *,
        window_days: int = DEFAULT_TREND_DAYS,
        top_n: int = DEFAULT_TOP_N,
        since: datetime | None = None,
    ) -> DomainDashboard:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        if not domain_id:
            raise ValueError("domain_id is required")
        window_start = self._window_start(window_days, since)

        base_filter = [
            Trace.tenant_id == tenant_id,
            Trace.domain_id == domain_id,
            Trace.created_at >= window_start,
        ]
        aggregates = self._aggregates(
            base_filter, scope_tag=("domain", f"{tenant_id}/{domain_id}")
        )
        trend = self._trend(base_filter, window_days, window_start)
        top_agents = self._top_children(
            base_filter=base_filter + [Trace.agent_id.is_not(None)],
            group_col=Trace.agent_id,
            display_lookup=self._agent_display_lookup(tenant_id, domain_id),
            top_n=top_n,
            scope_tag=("domain.top_agents", f"{tenant_id}/{domain_id}"),
            scope_check=lambda row_id: row_id is not None,
        )

        return DomainDashboard(
            tenant_id=tenant_id,
            domain_id=domain_id,
            window_days=window_days,
            aggregates=aggregates,
            trend=trend,
            top_agents=top_agents,
        )

    def get_agent_dashboard(
        self,
        tenant_id: str,
        domain_id: str,
        agent_id: str,
        *,
        window_days: int = DEFAULT_TREND_DAYS,
        top_n: int = DEFAULT_TOP_N,
        since: datetime | None = None,
    ) -> AgentDashboard:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        if not domain_id:
            raise ValueError("domain_id is required")
        if not agent_id:
            raise ValueError("agent_id is required")
        window_start = self._window_start(window_days, since)

        base_filter = [
            Trace.tenant_id == tenant_id,
            Trace.domain_id == domain_id,
            Trace.agent_id == agent_id,
            Trace.created_at >= window_start,
        ]
        aggregates = self._aggregates(
            base_filter,
            scope_tag=("agent", f"{tenant_id}/{domain_id}/{agent_id}"),
        )
        trend = self._trend(base_filter, window_days, window_start)
        top_instances = self._top_children(
            base_filter=base_filter + [Trace.luciel_instance_id.is_not(None)],
            group_col=Trace.luciel_instance_id,
            display_lookup=self._instance_display_lookup_for_agent(
                tenant_id, domain_id, agent_id
            ),
            top_n=top_n,
            scope_tag=(
                "agent.top_instances",
                f"{tenant_id}/{domain_id}/{agent_id}",
            ),
            scope_check=lambda row_id: row_id is not None,
            id_cast=str,
        )

        return AgentDashboard(
            tenant_id=tenant_id,
            domain_id=domain_id,
            agent_id=agent_id,
            window_days=window_days,
            aggregates=aggregates,
            trend=trend,
            top_luciel_instances=top_instances,
        )

    # ----------------------------------------------------------------- #
    # Internals
    # ----------------------------------------------------------------- #

    @staticmethod
    def _window_start(window_days: int, since: datetime | None) -> datetime:
        if since is not None:
            # Caller-supplied since wins. Used by the validation-gate
            # harness in sub-branch 4 to pin a deterministic window.
            return since
        # Trace.created_at is stored as UTC by TimestampMixin (server_default
        # CURRENT_TIMESTAMP). We compute the window in UTC for the same
        # reason -- a tz-naive comparison against the column would either
        # silently misbehave on a non-UTC client or trip the SQLAlchemy
        # tz-aware/tz-naive guard.
        return datetime.now(timezone.utc) - timedelta(days=window_days)

    def _aggregates(
        self,
        base_filter: list,
        *,
        scope_tag: tuple[str, str],
    ) -> ScopeAggregates:
        """Headline counts. One round trip; SUM/COUNT in SQL."""
        stmt = select(
            func.count(Trace.id).label("turn_count"),
            func.count(func.distinct(Trace.user_id)).label("unique_user_count"),
            func.coalesce(
                func.sum(
                    # Booleans -> int for sum() compatibility across
                    # SQLAlchemy dialects. The CASE shape is dialect-
                    # portable in a way that SUM(boolean) is not.
                    func.cast(Trace.escalated, type_=Integer)
                ),
                0,
            ).label("escalation_count"),
            func.coalesce(
                func.sum(
                    func.cast(Trace.tool_called, type_=Integer)
                ),
                0,
            ).label("tool_call_count"),
        ).where(and_(*base_filter))
        row = self.db.execute(stmt).one()
        return ScopeAggregates(
            turn_count=int(row.turn_count or 0),
            unique_user_count=int(row.unique_user_count or 0),
            escalation_count=int(row.escalation_count or 0),
            tool_call_count=int(row.tool_call_count or 0),
            moderation_block_count=0,  # v1 -- see module docstring.
            latency_p50_ms=None,
            latency_p95_ms=None,
        )

    def _trend(
        self,
        base_filter: list,
        window_days: int,
        window_start: datetime,
    ) -> list[TrendBucket]:
        """Per-day turn counts, oldest first, zero-filled.

        Days with no traces still appear with turn_count=0 so the
        dashboard renders a continuous line. This matters: a customer
        with a Sunday-only spike should see six zero days and one
        peak, not a single point.
        """
        # SQL: group by date(created_at). func.date is portable enough
        # for SQLite (e2e harness) and Postgres (prod) -- both implement
        # it. We then zero-fill in Python so the result is dialect-
        # independent and predictable.
        # Guard against an empty base_filter (a unit test may pass
        # []); SQLAlchemy 2.x deprecates `and_()` with zero args.
        where_clause = and_(True, *base_filter) if base_filter else and_(True)
        stmt = (
            select(
                func.date(Trace.created_at).label("day"),
                func.count(Trace.id).label("turn_count"),
            )
            .where(where_clause)
            .group_by(func.date(Trace.created_at))
            .order_by(func.date(Trace.created_at))
        )
        rows = self.db.execute(stmt).all()
        seen: dict[str, int] = {}
        for r in rows:
            # Postgres returns datetime.date; SQLite returns str. Normalise.
            key = r.day.isoformat() if hasattr(r.day, "isoformat") else str(r.day)
            seen[key] = int(r.turn_count or 0)

        # Trend = exactly `window_days` buckets ending on today (UTC).
        # Walking BACKWARDS from today guarantees today is the last
        # bucket so the headline turn_count and the trend sum agree:
        # if a customer's window_start lands at 11pm UTC and traces
        # arrive at 8am the next UTC day, both ends of the
        # [window_start, now] interval fall inside the buckets. A
        # forward walk from window_start.date() drops today and
        # silently undercounts.
        today = datetime.now(timezone.utc).date()
        if window_start.date() > today:
            # Pathological caller-supplied since (future). Anchor on
            # window_start.date() so the buckets still cover the
            # requested interval rather than 7 days of empty past.
            anchor = window_start.date() + timedelta(days=window_days - 1)
        else:
            anchor = today
        buckets: list[TrendBucket] = []
        for offset in range(window_days - 1, -1, -1):
            day = anchor - timedelta(days=offset)
            iso = day.isoformat()
            buckets.append(
                TrendBucket(day=iso, turn_count=seen.get(iso, 0))
            )
        return buckets

    def _top_children(
        self,
        *,
        base_filter: list,
        group_col,
        display_lookup: dict[str, str],
        top_n: int,
        scope_tag: tuple[str, str],
        scope_check,
        id_cast=str,
    ) -> list[ChildRollup]:
        """Top-N by turn_count, ties broken by child id ASC."""
        stmt = (
            select(
                group_col.label("child_id"),
                func.count(Trace.id).label("turn_count"),
                func.count(func.distinct(Trace.user_id)).label("unique_user_count"),
            )
            .where(and_(*base_filter))
            .group_by(group_col)
            .order_by(func.count(Trace.id).desc(), group_col.asc())
            .limit(top_n)
        )
        rows = self.db.execute(stmt).all()

        out: list[ChildRollup] = []
        for r in rows:
            child_id_raw = r.child_id
            # Defense-in-depth: the SQL WHERE pinned the scope, but we
            # re-assert here. The scope_check predicate returns False
            # if a row's id is somehow None despite the IS NOT NULL
            # filter (or, in a more elaborate scope, if the row drifts
            # outside the resolved bound). Any drop is logged at ERROR
            # because it represents a real invariant violation.
            if not scope_check(child_id_raw):
                logger.error(
                    "dashboard_service_scope_violation",
                    extra={
                        "event": "dashboard_service_scope_violation",
                        "scope_tag": scope_tag[0],
                        "scope_key": scope_tag[1],
                        "dropped_child_id": str(child_id_raw),
                    },
                )
                continue
            cid = id_cast(child_id_raw)
            out.append(
                ChildRollup(
                    child_id=cid,
                    child_display_name=display_lookup.get(cid),
                    turn_count=int(r.turn_count or 0),
                    unique_user_count=int(r.unique_user_count or 0),
                )
            )
        return out

    # --- display-name lookups (cheap because they're small lists) --- #

    def _domain_display_lookup(self, tenant_id: str) -> dict[str, str]:
        stmt = select(DomainConfig.domain_id, DomainConfig.display_name).where(
            DomainConfig.tenant_id == tenant_id
        )
        return {row.domain_id: row.display_name for row in self.db.execute(stmt).all()}

    def _agent_display_lookup(
        self, tenant_id: str, domain_id: str
    ) -> dict[str, str]:
        stmt = select(Agent.agent_id, Agent.display_name).where(
            and_(
                Agent.tenant_id == tenant_id,
                Agent.domain_id == domain_id,
            )
        )
        return {row.agent_id: row.display_name for row in self.db.execute(stmt).all()}

    def _instance_display_lookup_for_tenant(
        self, tenant_id: str
    ) -> dict[str, str]:
        stmt = select(LucielInstance.id, LucielInstance.display_name).where(
            LucielInstance.scope_owner_tenant_id == tenant_id
        )
        return {str(row.id): row.display_name for row in self.db.execute(stmt).all()}

    def _instance_display_lookup_for_agent(
        self, tenant_id: str, domain_id: str, agent_id: str
    ) -> dict[str, str]:
        stmt = select(LucielInstance.id, LucielInstance.display_name).where(
            and_(
                LucielInstance.scope_owner_tenant_id == tenant_id,
                LucielInstance.scope_owner_domain_id == domain_id,
                LucielInstance.scope_owner_agent_id == agent_id,
            )
        )
        return {str(row.id): row.display_name for row in self.db.execute(stmt).all()}
