"""Backend-free contract tests for Step 31 sub-branch 2.

Sub-branch 2 lands `app/services/dashboard_service.py`, the read-only
rollup layer behind the §3.2.12 hierarchical dashboards. Three
methods, one per scope level, each returning a frozen dataclass.

Coverage (AST + import + tiny in-memory SQLite -- no Postgres):

    * DashboardService class exists; constructor takes a `db: Session`.
    * Three public methods (get_tenant_dashboard, get_domain_dashboard,
      get_agent_dashboard) with the documented signatures.
    * Six result dataclasses exist with the documented field sets
      and are frozen.
    * No new DB writes -- the service module has no .add(), .commit(),
      or .flush() calls. AST-grep enforces this.
    * Defense-in-depth scope re-check: the top-children helper logs
      a `dashboard_service_scope_violation` ERROR row and DROPS any
      child whose scope_check returns False. We exercise this by
      stubbing scope_check to return False for one row and asserting
      it never lands in the output AND the logger captured the event.
    * End-to-end correctness on an in-memory SQLite DB: seed two
      tenants with overlapping shapes, call get_tenant_dashboard for
      one, and assert: zero leakage from the other tenant; correct
      headline counts; trend buckets sum to the headline; top-N
      ordering by turn_count DESC.

Postgres-required correctness (live DB, scope enforcement against
real RDS data, identity-claim join) is sub-branch 4's harness. This
file is the surface-shape pin and the SQL-portability sniff test.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_PATH = REPO_ROOT / "app" / "services" / "dashboard_service.py"


# ---------------------------------------------------------------------
# 1. Module surface -- class + result dataclasses
# ---------------------------------------------------------------------

class TestModuleSurface:
    def test_dashboard_service_class_exists(self):
        from app.services.dashboard_service import DashboardService
        assert inspect.isclass(DashboardService)

    def test_constructor_takes_db_session(self):
        from app.services.dashboard_service import DashboardService
        sig = inspect.signature(DashboardService.__init__)
        assert "db" in sig.parameters

    def test_all_result_dataclasses_present_and_frozen(self):
        from app.services.dashboard_service import (
            AgentDashboard,
            ChildRollup,
            DomainDashboard,
            ScopeAggregates,
            TenantDashboard,
            TrendBucket,
        )
        for cls in (
            ScopeAggregates,
            TrendBucket,
            ChildRollup,
            TenantDashboard,
            DomainDashboard,
            AgentDashboard,
        ):
            assert dataclasses.is_dataclass(cls), f"{cls.__name__} must be a dataclass"
            assert cls.__dataclass_params__.frozen, (
                f"{cls.__name__} must be frozen=True"
            )

    def test_scope_aggregates_field_shape(self):
        from app.services.dashboard_service import ScopeAggregates
        names = {f.name for f in dataclasses.fields(ScopeAggregates)}
        assert names == {
            "turn_count",
            "unique_user_count",
            "escalation_count",
            "tool_call_count",
            "moderation_block_count",
            "latency_p50_ms",
            "latency_p95_ms",
        }

    def test_trend_bucket_field_shape(self):
        from app.services.dashboard_service import TrendBucket
        names = {f.name for f in dataclasses.fields(TrendBucket)}
        assert names == {"day", "turn_count"}

    def test_child_rollup_field_shape(self):
        from app.services.dashboard_service import ChildRollup
        names = {f.name for f in dataclasses.fields(ChildRollup)}
        assert names == {
            "child_id",
            "child_display_name",
            "turn_count",
            "unique_user_count",
        }

    def test_tenant_dashboard_field_shape(self):
        from app.services.dashboard_service import TenantDashboard
        names = {f.name for f in dataclasses.fields(TenantDashboard)}
        assert names == {
            "tenant_id",
            "window_days",
            "aggregates",
            "trend",
            "top_domains",
            "top_luciel_instances",
        }

    def test_domain_dashboard_field_shape(self):
        from app.services.dashboard_service import DomainDashboard
        names = {f.name for f in dataclasses.fields(DomainDashboard)}
        assert names == {
            "tenant_id",
            "domain_id",
            "window_days",
            "aggregates",
            "trend",
            "top_agents",
        }

    def test_agent_dashboard_field_shape(self):
        from app.services.dashboard_service import AgentDashboard
        names = {f.name for f in dataclasses.fields(AgentDashboard)}
        assert names == {
            "tenant_id",
            "domain_id",
            "agent_id",
            "window_days",
            "aggregates",
            "trend",
            "top_luciel_instances",
        }


# ---------------------------------------------------------------------
# 2. Public method signatures
# ---------------------------------------------------------------------

class TestMethodSignatures:
    def test_get_tenant_dashboard_signature(self):
        from app.services.dashboard_service import DashboardService
        sig = inspect.signature(DashboardService.get_tenant_dashboard)
        # Positional tenant_id, kw-only window_days/top_n/since.
        assert "tenant_id" in sig.parameters
        for kw in ("window_days", "top_n", "since"):
            assert kw in sig.parameters
            assert sig.parameters[kw].kind == inspect.Parameter.KEYWORD_ONLY

    def test_get_domain_dashboard_signature(self):
        from app.services.dashboard_service import DashboardService
        sig = inspect.signature(DashboardService.get_domain_dashboard)
        for required in ("tenant_id", "domain_id"):
            assert required in sig.parameters

    def test_get_agent_dashboard_signature(self):
        from app.services.dashboard_service import DashboardService
        sig = inspect.signature(DashboardService.get_agent_dashboard)
        for required in ("tenant_id", "domain_id", "agent_id"):
            assert required in sig.parameters

    def test_required_ids_rejected_when_blank(self):
        # Empty scope ids must raise ValueError -- never let a
        # service-layer caller hit the SQL with an empty WHERE clause.
        from app.services.dashboard_service import DashboardService
        # We construct with a None db; the validators run BEFORE the
        # db is touched.
        svc = DashboardService(db=None)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            svc.get_tenant_dashboard("")
        with pytest.raises(ValueError):
            svc.get_domain_dashboard("t1", "")
        with pytest.raises(ValueError):
            svc.get_domain_dashboard("", "d1")
        with pytest.raises(ValueError):
            svc.get_agent_dashboard("t1", "d1", "")


# ---------------------------------------------------------------------
# 3. No-new-writes discipline (AST grep)
# ---------------------------------------------------------------------

class TestNoNewDbWrites:
    """§3.2.12: 'No new DB writes.' Any .add()/.commit()/.flush() in
    this module would be a contract violation. AST grep catches it
    before review.
    """

    def test_no_add_commit_flush_calls(self):
        src = SERVICE_PATH.read_text()
        tree = ast.parse(src)
        forbidden_methods = {"add", "commit", "flush", "delete", "merge"}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr in forbidden_methods:
                # Filter out false positives -- list.append etc. We
                # only care about calls where the receiver is `self.db`
                # or `db` (the Session handle).
                receiver_src = ast.unparse(func.value)
                if receiver_src in {"self.db", "db"}:
                    pytest.fail(
                        f"dashboard_service.py contains a write call: "
                        f"{ast.unparse(node)} -- §3.2.12 forbids new "
                        f"DB writes from this service"
                    )


# ---------------------------------------------------------------------
# 4. Defense-in-depth scope re-check
# ---------------------------------------------------------------------

class _StubDb:
    """Tiny SQLAlchemy-Session shim that returns scripted rows."""

    def __init__(self, scripted_results: list):
        self._scripted = list(scripted_results)
        self.executed_stmts: list = []

    def execute(self, stmt):
        self.executed_stmts.append(stmt)
        if not self._scripted:
            raise AssertionError("ran out of scripted results")
        result = self._scripted.pop(0)

        class _R:
            def __init__(_self, payload):
                _self._payload = payload

            def all(_self):
                return _self._payload

            def one(_self):
                return _self._payload[0]

        return _R(result)


class _FakeRow:
    """A row-like object with .child_id / .turn_count / .unique_user_count."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class TestDefenseInDepthScopeCheck:
    def test_top_children_drops_row_when_scope_check_false(self, caplog):
        # Build a service over a stub db. _top_children calls
        # db.execute(stmt).all() and then iterates.
        from app.services.dashboard_service import DashboardService
        # First row passes scope_check; second row fails. Only first
        # must appear in the output, and the second must trigger an
        # ERROR-level dashboard_service_scope_violation log.
        rows = [
            _FakeRow(child_id="d-good", turn_count=10, unique_user_count=3),
            _FakeRow(child_id="d-leaked", turn_count=5, unique_user_count=2),
        ]
        db = _StubDb(scripted_results=[rows])
        svc = DashboardService(db=db)  # type: ignore[arg-type]

        # We need a real Column-ish object only so the AST doesn't
        # complain inside the SQL builder -- but we patched .execute
        # to ignore the statement entirely, so any sentinel works.
        from app.models.trace import Trace
        with caplog.at_level(logging.ERROR):
            result = svc._top_children(  # type: ignore[attr-defined]
                base_filter=[Trace.tenant_id == "t1"],
                group_col=Trace.domain_id,
                display_lookup={"d-good": "Good Domain"},
                top_n=5,
                scope_tag=("test", "t1"),
                scope_check=lambda cid: cid == "d-good",
            )
        assert [r.child_id for r in result] == ["d-good"]
        assert result[0].child_display_name == "Good Domain"

        scope_logs = [
            r for r in caplog.records
            if r.message == "dashboard_service_scope_violation"
            or "scope_violation" in r.message
        ]
        assert scope_logs, (
            "dropping a row must emit a dashboard_service_scope_violation "
            "ERROR log"
        )

    def test_top_children_logs_at_error_level(self, caplog):
        # Belt-and-suspenders on the log severity. A WARNING is not
        # enough -- a scope violation is an alarmable condition.
        from app.services.dashboard_service import DashboardService
        from app.models.trace import Trace
        rows = [
            _FakeRow(child_id="d-bad", turn_count=5, unique_user_count=2),
        ]
        db = _StubDb(scripted_results=[rows])
        svc = DashboardService(db=db)  # type: ignore[arg-type]
        with caplog.at_level(logging.WARNING):
            svc._top_children(  # type: ignore[attr-defined]
                base_filter=[Trace.tenant_id == "t1"],
                group_col=Trace.domain_id,
                display_lookup={},
                top_n=5,
                scope_tag=("test", "t1"),
                scope_check=lambda cid: False,  # always reject
            )
        scope_logs = [
            r for r in caplog.records
            if r.levelno >= logging.ERROR
            and "scope_violation" in r.message
        ]
        assert scope_logs, "scope-violation log must be >= ERROR severity"


# ---------------------------------------------------------------------
# 5. Trend bucket math (zero-fill)
# ---------------------------------------------------------------------

class TestTrendBucketZeroFill:
    def test_trend_returns_exactly_window_days_buckets(self):
        # The trend MUST return exactly window_days buckets,
        # zero-filling days with no traces, oldest first, ending on
        # today (UTC). Renders a continuous chart line in the
        # dashboard UI.
        from app.services.dashboard_service import DashboardService
        # Scripted SQL: one row, 3 turns landing on today's UTC date.
        # The bucket walk anchors on today, so this row MUST land in
        # the final (rightmost) bucket -- regardless of when this
        # test runs.
        window_start = datetime.now(timezone.utc) - timedelta(days=7)
        today = datetime.now(timezone.utc).date()
        rows = [
            _FakeRow(day=today, turn_count=3),
        ]
        db = _StubDb(scripted_results=[rows])
        svc = DashboardService(db=db)  # type: ignore[arg-type]
        buckets = svc._trend(  # type: ignore[attr-defined]
            base_filter=[],
            window_days=7,
            window_start=window_start,
        )
        assert len(buckets) == 7
        # Today is the only bucket with traffic, and it's the last.
        assert buckets[-1].turn_count == 3
        assert buckets[-1].day == today.isoformat()
        assert sum(b.turn_count for b in buckets[:-1]) == 0
        # Oldest first (ISO order is ascending).
        days = [b.day for b in buckets]
        assert days == sorted(days)

    def test_trend_handles_caller_supplied_future_since(self):
        # Pathological: a harness calls with `since` pointing to the
        # future. The trend must still return window_days buckets
        # anchored on the requested interval.
        from app.services.dashboard_service import DashboardService
        future_start = datetime.now(timezone.utc) + timedelta(days=30)
        db = _StubDb(scripted_results=[[]])  # no rows
        svc = DashboardService(db=db)  # type: ignore[arg-type]
        buckets = svc._trend(  # type: ignore[attr-defined]
            base_filter=[],
            window_days=7,
            window_start=future_start,
        )
        assert len(buckets) == 7
        assert all(b.turn_count == 0 for b in buckets)


# ---------------------------------------------------------------------
# 6. End-to-end on an in-memory SQLite DB (no Postgres)
# ---------------------------------------------------------------------
# We deliberately keep this test backend-free by using SQLite. SQLAlchemy
# is dialect-portable for the surface this service uses (func.count,
# func.date, func.cast, group_by). The §3.2.12 sub-branch 4 harness is
# where the real RDS-bound correctness is asserted; this is the
# portability sniff test.

@pytest.fixture
def sqlite_session():
    from sqlalchemy import create_engine
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.orm import sessionmaker

    # JSONB is Postgres-only; render it as TEXT under SQLite so the
    # contract test runs without a Postgres. The dashboard service
    # itself does not query any JSONB column -- this is purely so
    # Base.metadata can emit DDL for the tables whose models live
    # alongside the ones we read.
    @compiles(JSONB, "sqlite")
    def _compile_jsonb_as_text(type_, compiler, **kw):  # noqa: ARG001
        return "TEXT"

    # Importing the models registers them on Base.metadata. We only
    # create the four tables the service queries directly.
    from app.models.trace import Trace
    from app.models.domain_config import DomainConfig
    from app.models.agent import Agent
    from app.models.luciel_instance import LucielInstance

    engine = create_engine("sqlite:///:memory:")
    for model in (LucielInstance, DomainConfig, Agent, Trace):
        model.__table__.create(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    yield Session()


def _make_trace(
    db,
    *,
    tenant_id: str,
    domain_id: str | None = None,
    agent_id: str | None = None,
    user_id: str | None = None,
    escalated: bool = False,
    tool_called: bool = False,
    luciel_instance_id: int | None = None,
    created_at: datetime | None = None,
):
    """Insert a single trace row and return it."""
    import uuid
    from app.models.trace import Trace
    t = Trace(
        trace_id=str(uuid.uuid4()),
        session_id=str(uuid.uuid4()),
        user_id=user_id,
        tenant_id=tenant_id,
        domain_id=domain_id,
        agent_id=agent_id,
        user_message="hi",
        assistant_reply="hello",
        escalated=escalated,
        tool_called=tool_called,
        luciel_instance_id=luciel_instance_id,
    )
    db.add(t)
    db.flush()
    if created_at is not None:
        # TimestampMixin set created_at to now() at insert; we override
        # for the test deterministic window.
        t.created_at = created_at
        db.flush()
    return t


class TestEndToEndOnSqlite:
    def test_get_tenant_dashboard_returns_zero_for_empty(self, sqlite_session):
        from app.services.dashboard_service import DashboardService
        svc = DashboardService(db=sqlite_session)
        d = svc.get_tenant_dashboard("tenant-empty")
        assert d.tenant_id == "tenant-empty"
        assert d.window_days == 7
        assert d.aggregates.turn_count == 0
        assert d.aggregates.unique_user_count == 0
        assert d.aggregates.escalation_count == 0
        assert d.aggregates.tool_call_count == 0
        assert d.aggregates.moderation_block_count == 0  # v1
        assert d.aggregates.latency_p50_ms is None
        assert d.aggregates.latency_p95_ms is None
        assert len(d.trend) == 7
        assert all(b.turn_count == 0 for b in d.trend)
        assert d.top_domains == []
        assert d.top_luciel_instances == []

    def test_tenant_isolation_in_aggregates(self, sqlite_session):
        # Two tenants seeded with overlapping shapes. The dashboard
        # for tenant A must see zero leakage from tenant B.
        from app.services.dashboard_service import DashboardService
        now = datetime.now(timezone.utc) - timedelta(hours=1)
        for _ in range(3):
            _make_trace(
                sqlite_session,
                tenant_id="tenant-A",
                domain_id="dom-1",
                user_id="user-A1",
                created_at=now,
            )
        for _ in range(7):
            _make_trace(
                sqlite_session,
                tenant_id="tenant-B",  # leakage source
                domain_id="dom-1",
                user_id="user-B1",
                created_at=now,
            )
        sqlite_session.commit()

        svc = DashboardService(db=sqlite_session)
        d_a = svc.get_tenant_dashboard("tenant-A")
        d_b = svc.get_tenant_dashboard("tenant-B")
        # The headline for A is the 3 turns it owns; the headline for
        # B is the 7 turns it owns. Crucially, neither sees the
        # other's traffic.
        assert d_a.aggregates.turn_count == 3
        assert d_b.aggregates.turn_count == 7

    def test_headline_counts(self, sqlite_session):
        from app.services.dashboard_service import DashboardService
        now = datetime.now(timezone.utc) - timedelta(hours=1)
        # 5 turns; 3 unique users; 2 escalations; 1 tool call.
        for u in ["u1", "u1", "u2", "u3", None]:
            _make_trace(
                sqlite_session,
                tenant_id="tenant-X",
                domain_id="dom-1",
                user_id=u,
                created_at=now,
            )
        # Add 2 escalations + 1 tool call as separate turns.
        _make_trace(
            sqlite_session, tenant_id="tenant-X", domain_id="dom-1",
            user_id="u1", escalated=True, created_at=now,
        )
        _make_trace(
            sqlite_session, tenant_id="tenant-X", domain_id="dom-1",
            user_id="u2", escalated=True, created_at=now,
        )
        _make_trace(
            sqlite_session, tenant_id="tenant-X", domain_id="dom-1",
            user_id="u1", tool_called=True, created_at=now,
        )
        sqlite_session.commit()

        svc = DashboardService(db=sqlite_session)
        d = svc.get_tenant_dashboard("tenant-X")
        assert d.aggregates.turn_count == 8
        # distinct(user_id) excludes NULL on most dialects; 3 unique
        # (u1, u2, u3).
        assert d.aggregates.unique_user_count == 3
        assert d.aggregates.escalation_count == 2
        assert d.aggregates.tool_call_count == 1

    def test_top_domains_ordered_by_turn_count(self, sqlite_session):
        from app.services.dashboard_service import DashboardService
        now = datetime.now(timezone.utc) - timedelta(hours=1)
        # dom-1: 5 turns; dom-2: 8 turns; dom-3: 3 turns.
        for _ in range(5):
            _make_trace(
                sqlite_session, tenant_id="tenant-Z", domain_id="dom-1",
                user_id="u1", created_at=now,
            )
        for _ in range(8):
            _make_trace(
                sqlite_session, tenant_id="tenant-Z", domain_id="dom-2",
                user_id="u2", created_at=now,
            )
        for _ in range(3):
            _make_trace(
                sqlite_session, tenant_id="tenant-Z", domain_id="dom-3",
                user_id="u3", created_at=now,
            )
        sqlite_session.commit()

        svc = DashboardService(db=sqlite_session)
        d = svc.get_tenant_dashboard("tenant-Z")
        ids = [r.child_id for r in d.top_domains]
        assert ids == ["dom-2", "dom-1", "dom-3"]
        assert [r.turn_count for r in d.top_domains] == [8, 5, 3]

    def test_domain_dashboard_filters_to_domain(self, sqlite_session):
        from app.services.dashboard_service import DashboardService
        now = datetime.now(timezone.utc) - timedelta(hours=1)
        for _ in range(4):
            _make_trace(
                sqlite_session, tenant_id="tenant-Q",
                domain_id="dom-1", agent_id="agent-1",
                user_id="u1", created_at=now,
            )
        for _ in range(9):
            _make_trace(
                sqlite_session, tenant_id="tenant-Q",
                domain_id="dom-2", agent_id="agent-99",
                user_id="u2", created_at=now,
            )
        sqlite_session.commit()

        svc = DashboardService(db=sqlite_session)
        d = svc.get_domain_dashboard("tenant-Q", "dom-1")
        assert d.aggregates.turn_count == 4
        # The dom-2 turns must NOT appear in this domain's top_agents.
        agent_ids = [r.child_id for r in d.top_agents]
        assert agent_ids == ["agent-1"]

    def test_agent_dashboard_filters_to_agent(self, sqlite_session):
        from app.services.dashboard_service import DashboardService
        now = datetime.now(timezone.utc) - timedelta(hours=1)
        for _ in range(2):
            _make_trace(
                sqlite_session, tenant_id="tenant-W",
                domain_id="dom-1", agent_id="agent-target",
                user_id="u1", created_at=now,
            )
        for _ in range(7):
            _make_trace(
                sqlite_session, tenant_id="tenant-W",
                domain_id="dom-1", agent_id="agent-other",
                user_id="u2", created_at=now,
            )
        sqlite_session.commit()

        svc = DashboardService(db=sqlite_session)
        d = svc.get_agent_dashboard("tenant-W", "dom-1", "agent-target")
        assert d.aggregates.turn_count == 2
        assert d.agent_id == "agent-target"

    def test_window_filter_excludes_old_traces(self, sqlite_session):
        from app.services.dashboard_service import DashboardService
        now = datetime.now(timezone.utc)
        # 3 inside window, 5 outside (30 days old).
        for _ in range(3):
            _make_trace(
                sqlite_session, tenant_id="tenant-T",
                domain_id="dom-1", user_id="u1",
                created_at=now - timedelta(hours=1),
            )
        for _ in range(5):
            _make_trace(
                sqlite_session, tenant_id="tenant-T",
                domain_id="dom-1", user_id="u1",
                created_at=now - timedelta(days=30),
            )
        sqlite_session.commit()

        svc = DashboardService(db=sqlite_session)
        d = svc.get_tenant_dashboard("tenant-T", window_days=7)
        assert d.aggregates.turn_count == 3

    def test_trend_sums_to_headline(self, sqlite_session):
        # Invariant: the operator sees a consistent picture. The
        # 7-day trend buckets must sum to the headline turn_count.
        from app.services.dashboard_service import DashboardService
        now = datetime.now(timezone.utc)
        # 2 turns today, 3 turns 2 days ago, 1 turn 5 days ago.
        for _ in range(2):
            _make_trace(
                sqlite_session, tenant_id="tenant-S",
                domain_id="dom-1", user_id="u1",
                created_at=now - timedelta(hours=1),
            )
        for _ in range(3):
            _make_trace(
                sqlite_session, tenant_id="tenant-S",
                domain_id="dom-1", user_id="u2",
                created_at=now - timedelta(days=2),
            )
        for _ in range(1):
            _make_trace(
                sqlite_session, tenant_id="tenant-S",
                domain_id="dom-1", user_id="u3",
                created_at=now - timedelta(days=5),
            )
        sqlite_session.commit()

        svc = DashboardService(db=sqlite_session)
        d = svc.get_tenant_dashboard("tenant-S", window_days=7)
        assert sum(b.turn_count for b in d.trend) == d.aggregates.turn_count
        assert d.aggregates.turn_count == 6
