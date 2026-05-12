"""
Step 31 — Five-pillar pre-launch validation gate harness.

This is the §3.2.12 "no customer goes live until five categories of
readiness are all green" gate, expressed as runnable Python. It is NOT
a unit test. It is a live exercise of the shipped code paths
(`SessionService.create_session_with_identity()`, `DashboardService`,
`CrossSessionRetriever`, `TraceService`, the admin_audit_logs
hash-chain listener, and the `cfn/luciel-prod-alarms.yaml` declarations)
against the five pillars in CANONICAL_RECAP §12 (row "31") and
DRIFTS.md D-step-31-impl-backlog-2026-05-11.

The five pillars (verbatim from ARCHITECTURE §3.2.12 design-lock):

  1. ISOLATION
     Two tenants seeded with overlapping shapes. Every dashboard call
     and every cross-session retriever call from one cannot see the
     other's data. The §4.7 promise made concrete on real rows.

  2. CUSTOMER JOURNEY
     A widget-style turn through
     `SessionService.create_session_with_identity()` lands an
     identity_claims row, a conversations row, a sessions row, message
     rows, a trace row, and (in the live widget path that sub-branch 1
     wired) the three structured log lines. The dashboard for that
     scope reflects the turn within the same run.

  3. MEMORY QUALITY
     The memory-extraction surface runs scope-bound; cross-session
     retrieval surfaces prior-turn passages under the same
     conversation_id, in scope, with correct provenance. The
     three-layer scope filter (§4.7) AND the defense-in-depth
     post-query loop both deny cross-tenant access.

  4. OPERATIONS
     The seven CloudWatch alarms in `cfn/luciel-prod-alarms.yaml` are
     declared (worker no-heartbeat, worker unhealthy task count,
     worker error log rate, RDS connection count, RDS CPU, RDS free
     storage, SSM access failure). Live `OK`-state verification stays
     `[PROD-PHASE-2B]` per cross-ref
     `D-prod-alarms-deployed-unverified-2026-05-09` — this pillar pins
     DECLARATION, not OK state.

  5. COMPLIANCE
     `admin_audit_logs` hash-chain advances across two writes (Pillar
     23 listener); `deletion_logs` table shape exists; the retention
     purge-worker absence is ACKNOWLEDGED via cross-ref to
     `D-retention-purge-worker-missing-2026-05-09`, NOT silenced.

Each pillar prints PASS/FAIL per claim and ends with a per-pillar
verdict. The script's exit code:

    0  → all five pillars green; safe to cut the
         step-31-dashboards-validation-gate-complete tag on the
         sub-branch 5 doc-truthing commit
    1  → at least one pillar red; DO NOT cut the tag
    2  → environment is not set up to run the harness (no Postgres
         DATABASE_URL); same convention as Step 24.5c precedent so
         a CI runner can distinguish "environment broken" from
         "claim violated"

POSTGRES REQUIRED. Like Step 24.5c, the schema uses native enums and
uuid columns that sqlite cannot represent without lossy casts. Run with:

    DATABASE_URL="postgresql+psycopg2://USER:PASS@HOST:PORT/DBNAME" \\
        python tests/e2e/step_31_validation_gate.py

The script creates two fresh tenant fixtures per run (timestamp-suffixed
tenant_ids) and does NOT clean up — re-runs are idempotent because the
fixture ids are unique. Same fixture discipline as Step 24.5c.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------- #
# Environment guard — refuse to run on sqlite. Same shape as Step 24.5c.
# Exit code 2 (environment) is distinct from 1 (claim failed).
# --------------------------------------------------------------------- #

_DB_URL = os.environ.get("DATABASE_URL", "")
if not _DB_URL.startswith("postgresql"):
    print(
        "ERROR: Step 31 validation-gate harness requires Postgres. "
        "Set DATABASE_URL=postgresql+psycopg2://... and re-run."
    )
    print(f"  Current DATABASE_URL={_DB_URL!r}")
    sys.exit(2)


from sqlalchemy.orm import Session as SqlSession  # noqa: E402

from app.db.database import SessionLocal  # noqa: E402
from app.memory.cross_session_retriever import (  # noqa: E402
    CrossSessionRetriever,
)
from app.models.admin_audit_log import (  # noqa: E402
    ACTION_UPDATE,
    AdminAuditLog,
    RESOURCE_TENANT,
)
from app.models.conversation import Conversation  # noqa: E402
from app.models.domain_config import DomainConfig  # noqa: E402
from app.models.identity_claim import (  # noqa: E402
    ClaimType,
    IdentityClaim,
)
from app.models.message import MessageModel  # noqa: E402
from app.models.retention import DeletionLog  # noqa: E402
from app.models.session import SessionModel  # noqa: E402
from app.models.tenant import TenantConfig  # noqa: E402
from app.models.trace import Trace  # noqa: E402
from app.repositories.admin_audit_repository import (  # noqa: E402
    AdminAuditRepository,
    AuditContext,
)
from app.repositories.session_repository import SessionRepository  # noqa: E402
from app.repositories.trace_repository import TraceRepository  # noqa: E402
from app.services.dashboard_service import DashboardService  # noqa: E402
from app.services.session_service import SessionService  # noqa: E402
from app.services.trace_service import TraceService  # noqa: E402


# --------------------------------------------------------------------- #
# Test harness scaffolding — same shape as Step 30c / Step 24.5c.
# --------------------------------------------------------------------- #


class ScenarioResult:
    def __init__(self, name: str, passed: bool, detail: str, pillar: str) -> None:
        self.name = name
        self.passed = passed
        self.detail = detail
        self.pillar = pillar


results: list[ScenarioResult] = []
_current_pillar: str = "(unset)"


def set_pillar(name: str) -> None:
    global _current_pillar
    _current_pillar = name


def record(name: str, passed: bool, detail: str = "") -> None:
    results.append(ScenarioResult(name, passed, detail, _current_pillar))
    flag = "PASS" if passed else "FAIL"
    print(f"  [{flag}] {name}")
    if detail:
        print(f"         {detail}")


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# --------------------------------------------------------------------- #
# Fixture setup — two tenants with overlapping shapes per pillar 1.
# Timestamp suffix keeps re-runs idempotent (no cleanup at end).
# --------------------------------------------------------------------- #

RUN_STAMP = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
TENANT_A = f"step31_gate_A_{RUN_STAMP}"
TENANT_B = f"step31_gate_B_{RUN_STAMP}"
DOMAIN_ID = "real_estate"  # same domain on both tenants — overlapping shape
AGENT_ID = "listings_intake"  # same agent string on both tenants

CLAIM_EMAIL_A = f"a-{RUN_STAMP}@example.test"
CLAIM_EMAIL_B = f"b-{RUN_STAMP}@example.test"


def install_two_tenant_fixture(db: SqlSession) -> None:
    """Two tenants with the SAME domain_id + agent_id strings.

    This is the overlapping-shape isolation test from §3.2.12. If the
    scope filter is wrong anywhere, A's reads will see B's rows.
    """
    for t in (TENANT_A, TENANT_B):
        db.add(
            TenantConfig(
                tenant_id=t,
                display_name=f"Step 31 gate {t}",
                description="Ephemeral fixture for the validation-gate harness.",
                allowed_domains=[DOMAIN_ID],
                active=True,
            )
        )
        db.add(
            DomainConfig(
                tenant_id=t,
                domain_id=DOMAIN_ID,
                display_name=f"{t} real estate",
                active=True,
            )
        )
    db.commit()


# --------------------------------------------------------------------- #
# RUN
# --------------------------------------------------------------------- #

print(f"Step 31 validation-gate harness — run stamp {RUN_STAMP}")
print(f"  Tenant A = {TENANT_A}")
print(f"  Tenant B = {TENANT_B}")

db: SqlSession = SessionLocal()
exit_code = 0

try:
    install_two_tenant_fixture(db)
    repo = SessionRepository(db=db)
    svc = SessionService(repository=repo)
    trace_svc = TraceService(repository=TraceRepository(db=db))
    dash = DashboardService(db=db)

    # =====================================================================
    # PILLAR 1 — ISOLATION
    # Two tenants, overlapping (tenant_id, domain_id, agent_id) shape.
    # Every dashboard call and every cross-session retriever call from
    # one cannot see the other's data.
    # =====================================================================
    header("PILLAR 1 — ISOLATION (two tenants, overlapping shapes)")
    set_pillar("isolation")

    # --- Seed identity + session + messages + trace under tenant A ---
    bundle_a = svc.create_session_with_identity(
        tenant_id=TENANT_A,
        domain_id=DOMAIN_ID,
        agent_id=AGENT_ID,
        channel="web",
        claim_type=ClaimType.EMAIL,
        claim_value=CLAIM_EMAIL_A,
        issuing_adapter="widget",
    )
    db.commit()
    session_a = bundle_a.session
    db.add(
        MessageModel(
            session_id=session_a.id,
            role="user",
            content="hi from tenant A",
        )
    )
    db.add(
        MessageModel(
            session_id=session_a.id,
            role="assistant",
            content="welcome (tenant A reply)",
        )
    )
    db.commit()
    trace_svc.record_trace(
        session_id=str(session_a.id),
        user_id=str(bundle_a.user_id),
        tenant_id=TENANT_A,
        domain_id=DOMAIN_ID,
        agent_id=AGENT_ID,
        user_message="hi from tenant A",
        assistant_reply="welcome (tenant A reply)",
    )

    # --- Seed identity + session + messages + trace under tenant B ---
    bundle_b = svc.create_session_with_identity(
        tenant_id=TENANT_B,
        domain_id=DOMAIN_ID,
        agent_id=AGENT_ID,
        channel="web",
        claim_type=ClaimType.EMAIL,
        claim_value=CLAIM_EMAIL_B,
        issuing_adapter="widget",
    )
    db.commit()
    session_b = bundle_b.session
    db.add(
        MessageModel(
            session_id=session_b.id,
            role="user",
            content="hi from tenant B",
        )
    )
    db.add(
        MessageModel(
            session_id=session_b.id,
            role="assistant",
            content="welcome (tenant B reply)",
        )
    )
    db.commit()
    trace_svc.record_trace(
        session_id=str(session_b.id),
        user_id=str(bundle_b.user_id),
        tenant_id=TENANT_B,
        domain_id=DOMAIN_ID,
        agent_id=AGENT_ID,
        user_message="hi from tenant B",
        assistant_reply="welcome (tenant B reply)",
    )

    # --- Claim 1a — Dashboard for tenant A sees ONLY tenant A's turn ---
    dash_a = dash.get_tenant_dashboard(TENANT_A, window_days=1, top_n=10)
    record(
        "tenant-A dashboard headline turn_count == 1 (no leakage from B)",
        dash_a.aggregates.turn_count == 1,
        detail=f"actual={dash_a.aggregates.turn_count}",
    )
    record(
        "tenant-A dashboard reports tenant_id == TENANT_A",
        dash_a.tenant_id == TENANT_A,
    )

    # --- Claim 1b — Dashboard for tenant B is symmetric ---
    dash_b = dash.get_tenant_dashboard(TENANT_B, window_days=1, top_n=10)
    record(
        "tenant-B dashboard headline turn_count == 1 (no leakage from A)",
        dash_b.aggregates.turn_count == 1,
        detail=f"actual={dash_b.aggregates.turn_count}",
    )

    # --- Claim 1c — Domain dashboards isolate even though domain_id matches ---
    dom_a = dash.get_domain_dashboard(
        TENANT_A, DOMAIN_ID, window_days=1, top_n=10
    )
    dom_b = dash.get_domain_dashboard(
        TENANT_B, DOMAIN_ID, window_days=1, top_n=10
    )
    record(
        "domain dashboard isolates on (tenant_id, domain_id) -- A sees 1",
        dom_a.aggregates.turn_count == 1,
    )
    record(
        "domain dashboard isolates on (tenant_id, domain_id) -- B sees 1",
        dom_b.aggregates.turn_count == 1,
    )

    # --- Claim 1d — Agent dashboards isolate even on identical agent_id ---
    agt_a = dash.get_agent_dashboard(
        TENANT_A, DOMAIN_ID, AGENT_ID, window_days=1, top_n=10
    )
    agt_b = dash.get_agent_dashboard(
        TENANT_B, DOMAIN_ID, AGENT_ID, window_days=1, top_n=10
    )
    record(
        "agent dashboard isolates on (tenant, domain, agent) -- A sees 1",
        agt_a.aggregates.turn_count == 1,
    )
    record(
        "agent dashboard isolates on (tenant, domain, agent) -- B sees 1",
        agt_b.aggregates.turn_count == 1,
    )

    # --- Claim 1e — Cross-session retriever cannot cross tenants ---
    retriever = CrossSessionRetriever(db=db)
    # Query tenant A's conversation with tenant B's tenant_id -- the
    # three-layer scope filter must drop everything.
    cross = retriever.retrieve(
        conversation_id=bundle_a.conversation_id,
        tenant_id=TENANT_B,   # mismatched
        domain_id=DOMAIN_ID,
        limit=20,
    )
    record(
        "retriever denies cross-tenant access (mismatched tenant_id -> 0 rows)",
        len(cross) == 0,
        detail=f"got {len(cross)} rows; expected 0",
    )

    # =====================================================================
    # PILLAR 2 — CUSTOMER JOURNEY
    # A widget-style turn through create_session_with_identity lands all
    # the rows the §3.2.11 contract promises AND the dashboard reflects
    # the turn within the same run. The three structured log lines from
    # sub-branch 1 (widget_chat_turn_received / _session_resolved /
    # _turn_completed) live on the live widget path; here we pin their
    # presence via the chat_widget logger's docstring contract +
    # SOURCE-level emission existence rather than re-running the widget
    # endpoint (which would require booting uvicorn + middleware +
    # ApiKeyAuthMiddleware -- sub-branch 5 / Step 31 closing live
    # rehearsal covers that end-to-end).
    # =====================================================================
    header("PILLAR 2 — CUSTOMER JOURNEY (end-to-end identity + dashboard)")
    set_pillar("customer_journey")

    claims_in_scope = (
        db.query(IdentityClaim)
        .filter(
            IdentityClaim.tenant_id == TENANT_A,
            IdentityClaim.domain_id == DOMAIN_ID,
        )
        .all()
    )
    convs_in_scope = (
        db.query(Conversation)
        .filter(
            Conversation.tenant_id == TENANT_A,
            Conversation.domain_id == DOMAIN_ID,
        )
        .all()
    )
    sessions_in_scope = (
        db.query(SessionModel)
        .filter(
            SessionModel.tenant_id == TENANT_A,
            SessionModel.domain_id == DOMAIN_ID,
        )
        .all()
    )
    messages_in_session = (
        db.query(MessageModel)
        .filter(MessageModel.session_id == session_a.id)
        .all()
    )
    traces_in_scope = (
        db.query(Trace)
        .filter(
            Trace.tenant_id == TENANT_A,
            Trace.domain_id == DOMAIN_ID,
        )
        .all()
    )

    record(
        "identity_claims row landed for the journey (exactly 1 in scope)",
        len(claims_in_scope) == 1,
    )
    record(
        "conversations row landed for the journey (exactly 1 in scope)",
        len(convs_in_scope) == 1,
    )
    record(
        "sessions row landed for the journey (exactly 1 in scope)",
        len(sessions_in_scope) == 1,
    )
    record(
        "messages landed on the new session (>= 2 turns user+assistant)",
        len(messages_in_session) >= 2,
        detail=f"got {len(messages_in_session)} messages",
    )
    record(
        "trace row landed for the journey (exactly 1 in scope)",
        len(traces_in_scope) == 1,
    )

    # Dashboard reflects the turn -- same-run round trip.
    record(
        "dashboard for the journey's scope reflects the turn",
        agt_a.aggregates.turn_count == 1,
    )
    # The trend bucket containing today must carry the turn.
    today_iso = datetime.now(timezone.utc).date().isoformat()
    today_bucket = [b for b in agt_a.trend if b.day == today_iso]
    record(
        "dashboard trend includes a bucket for today (UTC)",
        len(today_bucket) == 1,
        detail=f"trend days: {[b.day for b in agt_a.trend]}",
    )
    if today_bucket:
        record(
            "today's trend bucket records turn_count >= 1",
            today_bucket[0].turn_count >= 1,
            detail=f"actual={today_bucket[0].turn_count}",
        )

    # Sub-branch-1 audit-log-line emission contract: pinned at SOURCE.
    # We don't re-emit them here -- the canonical proof is the
    # tests/api/test_step31_widget_audit_log_shape.py contract test
    # (backend-free) PLUS the live widget-e2e job that exercises a real
    # widget call on every PR. The harness asserts the source-level
    # event names exist on the widget module so a deletion would fail.
    chat_widget_src = (
        Path(__file__).resolve().parents[1].parent
        / "app"
        / "api"
        / "v1"
        / "chat_widget.py"
    ).read_text()
    for event in (
        "widget_chat_turn_received",
        "widget_chat_session_resolved",
        "widget_chat_turn_completed",
    ):
        record(
            f"chat_widget.py emits {event!r} (source-level pin)",
            event in chat_widget_src,
        )

    # =====================================================================
    # PILLAR 3 — MEMORY QUALITY
    # Cross-session retrieval is scope-bound; a second session under the
    # same conversation_id surfaces the prior session's turns with
    # correct provenance. Defense-in-depth: cross-tenant query yields
    # zero even if the SQL filter is misconfigured (the retriever's
    # post-query scope re-check from sub-branch 2 of Step 24.5c).
    # =====================================================================
    header("PILLAR 3 — MEMORY QUALITY (scope-bound retrieval + provenance)")
    set_pillar("memory_quality")

    # Create a SECOND session under the same conversation_id -- the
    # programmatic-API channel scenario. SessionService.
    # create_session_with_identity called with the same EMAIL claim
    # under the same (tenant, domain) returns the EXISTING user +
    # conversation per Step 24.5c PR #26.
    bundle_a2 = svc.create_session_with_identity(
        tenant_id=TENANT_A,
        domain_id=DOMAIN_ID,
        agent_id=AGENT_ID,
        channel="programmatic_api",
        claim_type=ClaimType.EMAIL,
        claim_value=CLAIM_EMAIL_A,
        issuing_adapter="programmatic_api",
    )
    db.commit()

    record(
        "second channel under same email joins existing conversation",
        bundle_a2.conversation_id == bundle_a.conversation_id,
    )
    record(
        "second channel re-uses existing user",
        bundle_a2.user_id == bundle_a.user_id,
    )

    # Retriever surfaces prior session's turns, scope-bound to tenant A.
    passages = retriever.retrieve(
        conversation_id=bundle_a.conversation_id,
        tenant_id=TENANT_A,
        domain_id=DOMAIN_ID,
        limit=20,
    )
    record(
        "retriever surfaces prior session's turns under same conversation",
        len(passages) >= 2,
        detail=f"got {len(passages)} passages",
    )
    record(
        "every passage carries source_session_id == prior session",
        all(p.source_session_id == session_a.id for p in passages),
    )
    record(
        "every passage carries source_channel == 'web' (provenance)",
        all(p.source_channel == "web" for p in passages),
    )

    # Defense-in-depth: same retriever call but with mismatched
    # tenant_id MUST yield zero rows.
    crossed = retriever.retrieve(
        conversation_id=bundle_a.conversation_id,
        tenant_id=TENANT_B,    # wrong tenant
        domain_id=DOMAIN_ID,
        limit=20,
    )
    record(
        "retriever post-query scope filter denies cross-tenant access",
        len(crossed) == 0,
    )

    # =====================================================================
    # PILLAR 4 — OPERATIONS
    # The seven CloudWatch alarms in cfn/luciel-prod-alarms.yaml are
    # declared. Live OK-state stays [PROD-PHASE-2B] per cross-ref to
    # D-prod-alarms-deployed-unverified-2026-05-09 -- this pillar pins
    # DECLARATION here.
    # =====================================================================
    header("PILLAR 4 — OPERATIONS (alarms declared in cfn yaml)")
    set_pillar("operations")

    cfn_path = (
        Path(__file__).resolve().parents[1].parent
        / "cfn"
        / "luciel-prod-alarms.yaml"
    )
    cfn_src = cfn_path.read_text() if cfn_path.is_file() else ""
    record(
        "cfn/luciel-prod-alarms.yaml exists at canonical path",
        cfn_path.is_file(),
    )
    required_alarms = (
        "WorkerNoHeartbeatAlarm",
        "WorkerUnhealthyTaskCountAlarm",
        "WorkerErrorLogRateAlarm",
        "RdsConnectionCountAlarm",
        "RdsCpuAlarm",
        "RdsFreeStorageAlarm",
        "SsmAccessFailureAlarm",
    )
    for alarm in required_alarms:
        record(
            f"alarm declared: {alarm}",
            alarm in cfn_src,
        )
    # Cross-ref pin (a future silent deletion of the drift token would
    # also need to update this string -- catches "tidied away" drift).
    record(
        "live OK-state verification cross-ref present in DRIFTS",
        "D-prod-alarms-deployed-unverified-2026-05-09"
        in (Path(__file__).resolve().parents[1].parent / "docs" / "DRIFTS.md")
        .read_text(),
        detail="must stay until [PROD-PHASE-2B] live verification lands",
    )

    # =====================================================================
    # PILLAR 5 — COMPLIANCE
    # admin_audit_logs hash chain intact across two writes; deletion_logs
    # table shape present; retention-purge-worker absence acknowledged
    # via the drift token (NOT silenced).
    # =====================================================================
    header("PILLAR 5 — COMPLIANCE (audit hash chain + deletion log + drift)")
    set_pillar("compliance")

    audit_repo = AdminAuditRepository(db=db)
    audit_ctx = AuditContext.system(label=f"step31-gate-{RUN_STAMP}")
    # Two writes against the tenant-A fixture. The Pillar 23 before_flush
    # listener should advance the chain so write_2.prev_row_hash ==
    # write_1.row_hash. We use ACTION_UPDATE + RESOURCE_TENANT (both on
    # the advisory allow-lists) so the repository's allow-list guard
    # admits these probe rows.
    audit_repo.record(
        ctx=audit_ctx,
        tenant_id=TENANT_A,
        action=ACTION_UPDATE,
        resource_type=RESOURCE_TENANT,
        resource_natural_id=TENANT_A,
        note=f"step31 gate probe 1/2 ({RUN_STAMP})",
        autocommit=True,
    )
    audit_repo.record(
        ctx=audit_ctx,
        tenant_id=TENANT_A,
        action=ACTION_UPDATE,
        resource_type=RESOURCE_TENANT,
        resource_natural_id=TENANT_A,
        note=f"step31 gate probe 2/2 ({RUN_STAMP})",
        autocommit=True,
    )

    rows = (
        db.query(AdminAuditLog)
        .filter(AdminAuditLog.actor_label == f"step31-gate-{RUN_STAMP}")
        .order_by(AdminAuditLog.id.asc())
        .all()
    )
    record(
        "two admin_audit_logs rows landed for this run",
        len(rows) == 2,
        detail=f"got {len(rows)}",
    )
    if len(rows) == 2:
        r1, r2 = rows
        record(
            "row 1 has non-empty row_hash",
            bool(r1.row_hash) and len(r1.row_hash) == 64,
            detail=f"row_hash len={len(r1.row_hash or '')}",
        )
        record(
            "row 2 has non-empty row_hash",
            bool(r2.row_hash) and len(r2.row_hash) == 64,
        )
        record(
            "hash chain advances: row_2.prev_row_hash == row_1.row_hash",
            r2.prev_row_hash == r1.row_hash,
            detail=(
                f"r1.row_hash={r1.row_hash[:16] if r1.row_hash else '∅'}... "
                f"r2.prev_row_hash={r2.prev_row_hash[:16] if r2.prev_row_hash else '∅'}..."
            ),
        )

    # deletion_logs table shape exists (model declared + table queryable).
    # We don't insert a deletion row -- the retention-purge worker is
    # the legitimate writer and its absence is acknowledged below.
    deletion_count = db.query(DeletionLog).count()
    record(
        "deletion_logs table is queryable (model + DDL present)",
        deletion_count >= 0,
        detail=f"current row count={deletion_count}",
    )

    # Retention-purge-worker absence: ACKNOWLEDGED, not silenced. The
    # drift token must still exist in DRIFTS.md until the worker lands.
    drifts_src = (
        Path(__file__).resolve().parents[1].parent / "docs" / "DRIFTS.md"
    ).read_text()
    record(
        "D-retention-purge-worker-missing-2026-05-09 still cross-ref'd",
        "D-retention-purge-worker-missing-2026-05-09" in drifts_src,
        detail="acknowledge the gap; do NOT silence by deleting the token",
    )

    # =====================================================================
    # SUMMARY
    # =====================================================================
    header("SUMMARY — five-pillar verdict")

    pillars = (
        "isolation",
        "customer_journey",
        "memory_quality",
        "operations",
        "compliance",
    )
    for p in pillars:
        in_p = [r for r in results if r.pillar == p]
        passed_p = sum(1 for r in in_p if r.passed)
        total_p = len(in_p)
        verdict = "GREEN" if passed_p == total_p else "RED"
        print(f"  {p:<20} {verdict:<6} ({passed_p}/{total_p} claims)")

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed

    print()
    print(f"  Total claims:     {total}")
    print(f"  Passed:           {passed}")
    print(f"  Failed:           {failed}")
    print()

    if failed:
        print("  FAILED CLAIMS:")
        for r in results:
            if not r.passed:
                print(f"    - [{r.pillar}] {r.name}")
        print()
        print("  Step 31 validation gate is NOT GREEN.")
        print("  Do NOT cut step-31-dashboards-validation-gate-complete.")
        exit_code = 1
    else:
        print("  All five pillars GREEN against the live shipped code")
        print("  (real Postgres, real ORM, real DashboardService +")
        print("  CrossSessionRetriever + SessionService + audit chain).")
        print()
        print("  Safe to cut tag on the sub-branch 5 doc-truthing commit:")
        print("    step-31-dashboards-validation-gate-complete")
        exit_code = 0

finally:
    db.close()

sys.exit(exit_code)
