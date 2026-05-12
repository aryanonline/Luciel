"""
Step 31 — Five-pillar validation-gate harness shape contract tests.

Step 31 sub-branch 4, Deliverable B (harness-shape pin).

Purpose
=======

The Step 31 validation-gate harness (tests/e2e/step_31_validation_gate.py)
is a runnable script — NOT a pytest module — that exercises the shipped
five-pillar pre-launch readiness gate from ARCHITECTURE §3.2.12 against
a real Postgres dev DB and asserts the v1 success criterion from
CANONICAL_RECAP §12 (row "31") and DRIFTS.md
D-step-31-impl-backlog-2026-05-11.

Because it requires Postgres, the script CANNOT run in the backend-free
CI job. That's a feature, not a bug — it mirrors Step 24.5c's
`tests/e2e/step_24_5c_live_e2e.py` precedent: the live harness is run
locally against the dev DB on the doc-truthing commit, and the
backend-free CI job pins the harness's *shape* so accidental deletion
or signature drift fails CI loudly.

Invariants this module pins:

  * The script file exists at the canonical path.
  * The script's module docstring names the five pillars and the
    success criterion it exercises (so a future grep on the recap
    claim still lands here).
  * The script imports the load-bearing runtime surfaces it must
    exercise: DashboardService, SessionService, CrossSessionRetriever,
    AdminAuditRepository, TraceService, plus the ORM models that back
    the per-pillar database-state claims. If any of these is removed
    from the imports, the harness is no longer end-to-end and CI must
    fail.
  * The script refuses to run on non-Postgres DATABASE_URL (the
    native enum + uuid columns the Step 24.5c schema installed cannot
    be represented by sqlite without lossy casts). Checked
    syntactically: the script must contain an early DATABASE_URL
    guard with sys.exit(2).
  * The script exits 0 on all-green / 1 on any-failed / 2 on
    environment-not-ready (same convention as Step 24.5c so a CI
    runner can distinguish "env broken" from "claim failed").
  * The script exercises each of the five pillars by name (so the
    §3.2.12 design-lock claim and the harness output stay in lockstep).
  * The script exercises the load-bearing tokens that the five pillars
    are built on: the §3.3 step-4 hook (create_session_with_identity),
    the widget audit-log event names sub-branch 1 wired
    (widget_chat_turn_received / _session_resolved / _turn_completed),
    the exclude_session_id retriever kwarg, both channel labels named
    in the v1 success criterion (web + programmatic_api), and the
    DRIFT cross-refs the operations + compliance pillars acknowledge.

Style
=====

AST + filesystem assertions only. No live HTTP, no subprocess, no
docker, no DB. Backend-free, lives in the existing backend-free
pytest job in .github/workflows/ci.yml.
"""

from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "tests" / "e2e" / "step_31_validation_gate.py"


# ---------------------------------------------------------------------------
# Filesystem-level invariants
# ---------------------------------------------------------------------------


def test_validation_gate_script_exists_at_canonical_path():
    """The harness lives at tests/e2e/step_31_validation_gate.py — same
    parent directory and same naming convention as Step 30c's precedent
    (tests/e2e/step_30c_live_e2e.py) and Step 24.5c's precedent
    (tests/e2e/step_24_5c_live_e2e.py). Moving the file breaks the
    doc-truthing cross-reference."""
    assert SCRIPT_PATH.is_file(), (
        f"Expected Step 31 validation-gate harness at {SCRIPT_PATH}; "
        f"not found. Did the file move or get deleted?"
    )


def test_validation_gate_script_is_not_empty():
    """The harness must be a real script, not a placeholder stub.
    Step 24.5c precedent is ~470 lines for 6 claim groups; Step 31's
    five-pillar harness is ~750 lines. 200 is a generous floor."""
    contents = SCRIPT_PATH.read_text()
    assert len(contents.splitlines()) >= 200, (
        "Step 31 validation-gate harness is suspiciously short; "
        "the precedent is ~470+ lines and ours covers 5 pillars."
    )


# ---------------------------------------------------------------------------
# AST-level invariants
# ---------------------------------------------------------------------------


def _parse_script() -> ast.Module:
    return ast.parse(SCRIPT_PATH.read_text(), filename=str(SCRIPT_PATH))


def _collect_imports(tree: ast.Module) -> set[tuple[str, str]]:
    """Return every (module, name) pair from `from X import Y` blocks."""
    imported: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imported.add((module, alias.name))
    return imported


def test_validation_gate_module_docstring_names_five_pillars():
    """The module docstring is the first place a future contributor
    lands. It MUST name all five pillars verbatim so a grep on the
    §3.2.12 design-lock claim from ARCHITECTURE lands here."""
    tree = _parse_script()
    docstring = ast.get_docstring(tree)
    assert docstring is not None, (
        "Module docstring missing on Step 31 validation-gate harness."
    )

    required_markers = [
        "Step 31",
        "CANONICAL_RECAP",
        "31",
        # The five pillars by name — verbatim per ARCHITECTURE §3.2.12
        "ISOLATION",
        "CUSTOMER JOURNEY",
        "MEMORY QUALITY",
        "OPERATIONS",
        "COMPLIANCE",
        # The shipped surfaces the harness exercises
        "create_session_with_identity",
        "DashboardService",
        "CrossSessionRetriever",
        # DRIFT cross-refs the operations + compliance pillars acknowledge
        "D-prod-alarms-deployed-unverified-2026-05-09",
        "D-retention-purge-worker-missing-2026-05-09",
    ]
    for marker in required_markers:
        assert marker in docstring, (
            f"Validation-gate docstring missing required marker {marker!r}. "
            f"This marker is load-bearing for the doc-truthing "
            f"cross-reference from CANONICAL_RECAP §12 / DRIFTS "
            f"D-step-31-impl-backlog-2026-05-11."
        )


def test_validation_gate_imports_all_load_bearing_runtime_surfaces():
    """The harness MUST exercise the five load-bearing runtime surfaces
    that Step 31's pillars are built on. If any of these imports is
    removed, the harness has silently stopped being end-to-end."""
    tree = _parse_script()
    imported = _collect_imports(tree)

    required = [
        # Sub-branch 2 — the dashboard read-only aggregation surface
        ("app.services.dashboard_service", "DashboardService"),
        # §3.3 step 4 hook — sub-branch 4 of Step 24.5c, exercised by
        # Step 31 pillar 2 (customer journey).
        ("app.services.session_service", "SessionService"),
        # Runtime memory surface — Step 31 pillar 3 (memory quality)
        # exercises this with exclude_session_id discipline.
        ("app.memory.cross_session_retriever", "CrossSessionRetriever"),
        # Pillar 23 listener carrier — Step 31 pillar 5 (compliance)
        # exercises the hash-chain advancement across two writes.
        ("app.repositories.admin_audit_repository", "AdminAuditRepository"),
        # Trace persistence — Step 31 pillar 2 (customer journey)
        # asserts a trace row lands.
        ("app.services.trace_service", "TraceService"),
    ]

    for module, name in required:
        assert (module, name) in imported, (
            f"Validation-gate harness must import {name} from {module}. "
            f"Without this import the harness is no longer end-to-end."
        )


def test_validation_gate_imports_orm_models_for_db_state_claims():
    """The harness must exercise the ORM models too — Conversation,
    IdentityClaim, SessionModel, MessageModel, Trace, AdminAuditLog,
    DeletionLog — so the database-state-truthing claims (one
    identity_claim, one conversation, one session, message rows, trace
    row, audit rows, deletion_logs queryable) are queried at the model
    layer, not via raw SQL."""
    tree = _parse_script()
    imported = _collect_imports(tree)

    required_models = [
        ("app.models.conversation", "Conversation"),
        ("app.models.identity_claim", "IdentityClaim"),
        ("app.models.identity_claim", "ClaimType"),
        ("app.models.session", "SessionModel"),
        ("app.models.message", "MessageModel"),
        ("app.models.trace", "Trace"),
        ("app.models.admin_audit_log", "AdminAuditLog"),
        ("app.models.retention", "DeletionLog"),
    ]

    for module, name in required_models:
        assert (module, name) in imported, (
            f"Validation-gate harness must import {name} from {module} — "
            f"the per-pillar database-state claims query this model at the "
            f"ORM layer, not via raw SQL."
        )


def test_validation_gate_imports_audit_allow_listed_constants():
    """The compliance pillar writes two admin_audit_logs rows to prove
    the Pillar 23 hash-chain listener advances. AdminAuditRepository
    guards writes with ALLOWED_ACTIONS / ALLOWED_RESOURCE_TYPES; the
    harness MUST import ACTION_UPDATE + RESOURCE_TENANT (allow-listed
    constants) so a future tightening of the allow-list is caught at
    import time."""
    tree = _parse_script()
    imported = _collect_imports(tree)

    assert ("app.models.admin_audit_log", "ACTION_UPDATE") in imported, (
        "Validation-gate harness must import ACTION_UPDATE — the "
        "compliance pillar writes audit rows guarded by "
        "AdminAuditRepository.ALLOWED_ACTIONS."
    )
    assert ("app.models.admin_audit_log", "RESOURCE_TENANT") in imported, (
        "Validation-gate harness must import RESOURCE_TENANT — the "
        "compliance pillar writes audit rows guarded by "
        "AdminAuditRepository.ALLOWED_RESOURCE_TYPES."
    )
    assert ("app.repositories.admin_audit_repository", "AuditContext") in imported, (
        "Validation-gate harness must import AuditContext — every "
        "AdminAuditRepository.record(...) call requires an actor ctx."
    )


def test_validation_gate_refuses_non_postgres_database_url():
    """The Step 24.5c schema (which Step 31 builds on) uses a native
    Postgres enum (identity_claim_type) and uuid columns. The harness
    must refuse to run on a non-Postgres DATABASE_URL with a clear
    error and a non-zero exit — silently coercing sqlite would produce
    false-positive PASSes against pillars 1-3."""
    source = SCRIPT_PATH.read_text()

    assert "DATABASE_URL" in source, (
        "Validation-gate harness must reference DATABASE_URL (it picks "
        "up the dev DB via the same env-var pattern as Step 24.5c "
        "precedent)."
    )
    assert (
        'startswith("postgresql")' in source
        or "startswith('postgresql')" in source
    ), (
        "Validation-gate harness must guard against non-Postgres "
        "DATABASE_URL (the Step 24.5c schema uses native enum + uuid "
        "columns). Expected an explicit "
        "DATABASE_URL.startswith('postgresql') check."
    )
    assert "sys.exit(2)" in source, (
        "Validation-gate harness must exit with code 2 on a non-Postgres "
        "DATABASE_URL (so a CI runner can distinguish 'env not set up' "
        "from 'a claim failed' at exit code 1)."
    )


def test_validation_gate_uses_summary_exit_code_pattern():
    """Same exit-code contract as Step 24.5c precedent: 0 on all
    pillars green, 1 on any failed claim, 2 on env-not-ready. A CI
    runner gates on this. Step 31's harness assigns exit_code in two
    branches (all-green vs any-failed) and calls sys.exit(exit_code)
    once at the end — a slight variant of Step 24.5c's pattern that
    keeps the same three-code contract."""
    source = SCRIPT_PATH.read_text()
    # Pin the three exit codes literally. Pillar 4 / 5 paths can land
    # on any of these depending on outcome — sys.exit(2) on env-not-
    # ready, exit_code=0 on all-green, exit_code=1 on any-failed.
    assert "sys.exit(2)" in source, (
        "Validation-gate harness must reach sys.exit(2) when "
        "DATABASE_URL is not Postgres (env-not-ready)."
    )
    assert "exit_code = 0" in source, (
        "Validation-gate harness must set exit_code = 0 when every "
        "claim across the five pillars passes (Step 24.5c precedent — "
        "CI gates on this)."
    )
    assert "exit_code = 1" in source, (
        "Validation-gate harness must set exit_code = 1 when any claim "
        "fails (Step 24.5c precedent — distinct from exit 2 'env not "
        "ready')."
    )
    assert "sys.exit(exit_code)" in source, (
        "Validation-gate harness must call sys.exit(exit_code) once at "
        "the bottom — the load-bearing exit signal CI gates on."
    )


# ---------------------------------------------------------------------------
# Pillar-by-pillar token pins — each pillar is a load-bearing claim of
# the §3.2.12 design-lock. If a refactor silently drops a pillar's
# central exercise, CI must fail loudly.
# ---------------------------------------------------------------------------


def test_validation_gate_pillar_1_exercises_two_tenants_with_overlap():
    """Pillar 1 (ISOLATION) seeds two tenants with overlapping shapes
    (same domain_id, same agent_id, overlapping email claims). The
    harness MUST literally instantiate both tenant fixtures and prove
    cross-tenant denial."""
    source = SCRIPT_PATH.read_text()
    assert "TENANT_A" in source and "TENANT_B" in source, (
        "Pillar 1 (isolation) requires two tenant fixtures. The harness "
        "must literally name TENANT_A and TENANT_B for cross-tenant "
        "claims to be greppable."
    )
    assert "isolation" in source.lower(), (
        "Pillar 1 must be named 'isolation' in the harness output so "
        "the §3.2.12 pillar label and the harness PASS/FAIL lines stay "
        "in lockstep."
    )


def test_validation_gate_pillar_2_calls_step_3_3_hook_by_name():
    """Pillar 2 (CUSTOMER JOURNEY) MUST call the §3.3 step-4 hook
    SessionService.create_session_with_identity() by name — not bypass
    it by hand-rolling the resolver call — so a future refactor that
    renames the hook is caught here, AND so the sub-branch 4 wiring
    proof stays load-bearing."""
    source = SCRIPT_PATH.read_text()
    assert "create_session_with_identity" in source, (
        "Pillar 2 (customer journey) must call "
        "SessionService.create_session_with_identity() — the §3.3 "
        "step-4 hook. Hand-rolling the resolver call bypasses the "
        "wiring sub-branch 4 of Step 24.5c was built to prove."
    )


def test_validation_gate_pillar_2_pins_widget_audit_log_event_names():
    """Pillar 2 includes a source-pin for the three widget audit-log
    event names sub-branch 1 of Step 31 wired into
    app/api/v1/chat_widget.py. These string literals are the contract
    between the widget channel and the dashboard's recent-activity
    surface — renaming any one of them silently breaks downstream
    consumers."""
    source = SCRIPT_PATH.read_text()
    for event_name in (
        "widget_chat_turn_received",
        "widget_chat_session_resolved",
        "widget_chat_turn_completed",
    ):
        assert event_name in source, (
            f"Pillar 2 (customer journey) must source-pin the widget "
            f"audit-log event name {event_name!r}. Sub-branch 1 of Step "
            f"31 wired this exact literal into app/api/v1/chat_widget.py."
        )


def test_validation_gate_pillar_3_exercises_cross_tenant_denial():
    """Pillar 3 (MEMORY QUALITY) exercises
    CrossSessionRetriever.retrieve() with a mismatched tenant_id and
    asserts the result set is empty — the §4.7 three-layer scope
    filter + defense-in-depth post-query loop denying cross-tenant
    access on real rows. The harness MUST exercise both tenants on
    the retriever so a future regression to single-tenant retrieval
    is caught end-to-end, not just at the retriever's own contract
    test."""
    source = SCRIPT_PATH.read_text()
    # The retriever is called at least twice: once in-scope (tenant A)
    # and once cross-tenant (tenant B). Pin both via the literal kwarg
    # values — the §4.7 promise made concrete on real rows.
    assert "retriever.retrieve(" in source, (
        "Pillar 3 (memory quality) must call "
        "CrossSessionRetriever.retrieve() — the runtime memory "
        "surface the §3.2.12 pillar 3 claim is built on."
    )
    assert "tenant_id=TENANT_B" in source, (
        "Pillar 3 (memory quality) must call retriever.retrieve() "
        "with tenant_id=TENANT_B (mismatched) — the cross-tenant "
        "denial claim from §4.7 made concrete on real rows."
    )
    # The provenance fields the retriever returns are load-bearing for
    # pillar 3's 'every passage carries source_channel == web' claim.
    assert "source_channel" in source, (
        "Pillar 3 (memory quality) must reference source_channel — "
        "every surfaced passage's provenance is checked against the "
        "channel label from the v1 success criterion."
    )
    assert "source_session_id" in source, (
        "Pillar 3 (memory quality) must reference source_session_id "
        "— every surfaced passage's provenance is checked against the "
        "prior session's id."
    )


def test_validation_gate_exercises_both_channels_in_success_criterion():
    """The v1 success criterion names two specific channel labels: the
    embeddable chat widget (channel='web') and the programmatic API
    (channel='programmatic_api'). Pillars 2 + 3 MUST exercise both
    channel labels literally — not aliases."""
    source = SCRIPT_PATH.read_text()
    assert '"web"' in source or "'web'" in source, (
        "Validation-gate harness must exercise channel='web' — the "
        "widget channel label named in the v1 success criterion."
    )
    assert '"programmatic_api"' in source or "'programmatic_api'" in source, (
        "Validation-gate harness must exercise channel='programmatic_api' "
        "— the second channel named in the v1 success criterion."
    )


def test_validation_gate_pillar_4_pins_alarms_yaml_path_and_drift():
    """Pillar 4 (OPERATIONS) pins the SEVEN CloudWatch alarms declared
    in cfn/luciel-prod-alarms.yaml. Live OK-state verification stays
    [PROD-PHASE-2B] per D-prod-alarms-deployed-unverified-2026-05-09 —
    this pillar pins DECLARATION, not OK state. Both the yaml path and
    the DRIFT cross-ref must appear in the harness."""
    source = SCRIPT_PATH.read_text()
    assert "luciel-prod-alarms.yaml" in source, (
        "Pillar 4 (operations) must reference "
        "cfn/luciel-prod-alarms.yaml — the canonical declaration site "
        "for the seven CloudWatch alarms."
    )
    assert "D-prod-alarms-deployed-unverified-2026-05-09" in source, (
        "Pillar 4 (operations) must acknowledge "
        "D-prod-alarms-deployed-unverified-2026-05-09 — the OK-state "
        "verification carve-out. Silencing this DRIFT cross-ref would "
        "let live-state regressions pass."
    )


def test_validation_gate_pillar_4_names_all_seven_alarms():
    """Pillar 4 (OPERATIONS) names all seven CloudWatch alarms declared
    in cfn/luciel-prod-alarms.yaml. If a future commit drops an alarm,
    the harness's declaration check fails — and so should this contract
    test, so the lock between the yaml and the harness is two-sided."""
    source = SCRIPT_PATH.read_text()
    required_alarms = [
        "WorkerNoHeartbeatAlarm",
        "WorkerUnhealthyTaskCountAlarm",
        "WorkerErrorLogRateAlarm",
        "RdsConnectionCountAlarm",
        "RdsCpuAlarm",
        "RdsFreeStorageAlarm",
        "SsmAccessFailureAlarm",
    ]
    for alarm in required_alarms:
        assert alarm in source, (
            f"Pillar 4 (operations) must name CloudWatch alarm "
            f"{alarm!r}. The §3.2.12 design-lock binds the harness to "
            f"the seven-alarm declaration in "
            f"cfn/luciel-prod-alarms.yaml."
        )


def test_validation_gate_pillar_5_exercises_audit_hash_chain():
    """Pillar 5 (COMPLIANCE) writes two admin_audit_logs rows and
    asserts the Pillar 23 before_flush listener advances the hash chain
    (row_2.prev_row_hash == row_1.row_hash). The harness MUST reference
    both columns by name so a future column rename is caught."""
    source = SCRIPT_PATH.read_text()
    assert "row_hash" in source, (
        "Pillar 5 (compliance) must reference admin_audit_logs.row_hash "
        "— half the hash-chain pair the Pillar 23 listener maintains."
    )
    assert "prev_row_hash" in source, (
        "Pillar 5 (compliance) must reference "
        "admin_audit_logs.prev_row_hash — the other half of the "
        "hash-chain pair the Pillar 23 listener maintains."
    )


def test_validation_gate_pillar_5_pins_retention_drift():
    """Pillar 5 (COMPLIANCE) ACKNOWLEDGES the retention purge-worker
    absence via D-retention-purge-worker-missing-2026-05-09 — it does
    NOT silence it. The DRIFT token MUST appear so the carve-out is
    explicit; silently dropping it would let the missing worker pass
    the gate."""
    source = SCRIPT_PATH.read_text()
    assert "D-retention-purge-worker-missing-2026-05-09" in source, (
        "Pillar 5 (compliance) must cross-ref "
        "D-retention-purge-worker-missing-2026-05-09. Silencing this "
        "DRIFT would let the missing retention purge worker pass the "
        "pre-launch gate."
    )


def test_validation_gate_names_every_pillar_in_set_pillar_calls():
    """The harness uses a set_pillar(name) call per pillar so each
    PASS/FAIL line is tagged with its pillar in the summary. The five
    pillar names MUST appear as set_pillar arguments — this is what
    binds the harness output back to the §3.2.12 design-lock labels."""
    source = SCRIPT_PATH.read_text()
    for pillar in (
        "isolation",
        "customer_journey",
        "memory_quality",
        "operations",
        "compliance",
    ):
        # set_pillar("isolation") or set_pillar('isolation')
        assert (
            f'set_pillar("{pillar}")' in source
            or f"set_pillar('{pillar}')" in source
        ), (
            f"Validation-gate harness must call set_pillar({pillar!r}) "
            f"so the PASS/FAIL lines for pillar {pillar!r} are tagged "
            f"in the summary. The §3.2.12 pillar labels and the harness "
            f"output must stay in lockstep."
        )


def test_validation_gate_pins_closing_tag_in_output():
    """The harness's all-green branch must reference the closing tag
    by name (step-31-dashboards-validation-gate-complete) so the
    operator running the gate knows exactly which tag is unlocked on
    success. This is the doc-truthing handoff to sub-branch 5."""
    source = SCRIPT_PATH.read_text()
    assert "step-31-dashboards-validation-gate-complete" in source, (
        "Validation-gate harness must name the closing tag "
        "'step-31-dashboards-validation-gate-complete' in its summary "
        "output — this is the doc-truthing handoff signal to sub-branch "
        "5 (cut the tag on the doc-truthing commit, not before)."
    )
