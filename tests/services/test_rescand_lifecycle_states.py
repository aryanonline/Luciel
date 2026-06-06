"""RESCAN TIER-DE — instance lifecycle 5-state machine + complete hard-delete
cascade (Architecture §3.6.1, §3.6.5).

Test scope
----------

1. **Enum shape** — all 5 canonical values + the deprecated ``deleted``
   alias exist and are string-valued; ``INSTANCE_GRACE_STATES`` covers
   both grace aliases.

2. **Transition table** — each permitted transition works; forbidden
   ones are rejected; the ``deleted`` / ``grace_window`` distinction is
   treated identically by the service layer.

3. **Instance-level hard-delete cascade** — the worker now purges all
   tables listed in §3.6.5: leads, escalation_events (session
   summaries), sibling_call_grants (both sides), instance_composition_
   grants, knowledge_share_grants, instance_tool_authorizations,
   byo_webhook_endpoints, channel_routes, tool_execution_log,
   user_role_assignments, knowledge_graph_nodes/edges, plus the
   previously-present tables.  Per-step counts appear in the audit
   manifest; tombstones (audit_log rows) are NOT deleted.

4. **Scan predicate** — worker now includes both 'deleted' and
   'grace_window' status values in the scan predicate.

5. **Migration shape** — rescand_lifecycle_states has the right
   revision/down_revision and contains the expected ADD VALUE calls.

Strategy: AST + source-text assertions (no live DB required).
A separate live-DB opt-in test (test_rescand_instance_cascade_live.py)
seeds a real DB and exercises the full cascade.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_PATH = REPO_ROOT / "app" / "worker" / "tasks" / "instance_retention.py"
MODEL_PATH = REPO_ROOT / "app" / "models" / "instance_status.py"
SERVICE_PATH = REPO_ROOT / "app" / "services" / "instance_service.py"
MIGRATION_PATH = (
    REPO_ROOT / "alembic" / "versions" / "rescand_lifecycle_states.py"
)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _parse(p: Path) -> ast.Module:
    return ast.parse(_read(p))


# ======================================================================
# §1: InstanceStatus enum has all 5 canonical values + deprecated alias.
# ======================================================================


def test_instance_status_has_all_five_canonical_values():
    """All five §3.6.1 canonical values must be present."""
    # Import the live module so we test the actual enum.
    from app.models.instance_status import InstanceStatus

    values = {m.value for m in InstanceStatus}
    for expected in ("active", "paused", "deactivating", "grace_window", "hard_deleted"):
        assert expected in values, (
            f"InstanceStatus must have value '{expected}' per §3.6.1."
        )


def test_instance_status_deleted_alias_present():
    """'deleted' is the deprecated alias for grace_window; it must still
    be a valid member so existing rows / queries are not orphaned."""
    from app.models.instance_status import InstanceStatus

    values = {m.value for m in InstanceStatus}
    assert "deleted" in values, (
        "'deleted' must be retained as a deprecated alias mapping to the "
        "grace_window semantics.  Removing it would orphan existing rows."
    )


def test_instance_status_members_are_string_valued():
    """All InstanceStatus members must be strings so SQLAlchemy and
    Pydantic round-trip cleanly with the PG enum."""
    from app.models.instance_status import InstanceStatus

    for member in InstanceStatus:
        assert isinstance(member.value, str), (
            f"InstanceStatus.{member.name} must be string-valued."
        )


def test_instance_grace_states_covers_both_aliases():
    """INSTANCE_GRACE_STATES must include both 'deleted' (legacy) and
    'grace_window' (new canonical) so transition logic treats them
    identically."""
    from app.models.instance_status import INSTANCE_GRACE_STATES

    assert "deleted" in INSTANCE_GRACE_STATES
    assert "grace_window" in INSTANCE_GRACE_STATES


def test_instance_status_values_tuple_is_complete():
    """INSTANCE_STATUS_VALUES must cover all 6 members (5 canonical + alias)."""
    from app.models.instance_status import INSTANCE_STATUS_VALUES, InstanceStatus

    assert set(INSTANCE_STATUS_VALUES) == {m.value for m in InstanceStatus}


# ======================================================================
# §2: Transition table enforcement in InstanceService.
# ======================================================================


def test_service_has_is_grace_state_helper():
    """_is_grace_state is the canonical grace-window predicate."""
    tree = _parse(SERVICE_PATH)
    names = {
        n.name for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_is_grace_state" in names, (
        "InstanceService must expose _is_grace_state helper."
    )


def test_pause_requires_active_state():
    """pause_instance must reject non-active states with
    InstanceLifecycleConflictError."""
    src = ast.unparse(
        next(
            n for n in ast.walk(_parse(SERVICE_PATH))
            if isinstance(n, ast.FunctionDef) and n.name == "pause_instance"
        )
    )
    assert "InstanceStatus.ACTIVE" in src, (
        "pause_instance must enforce that current state is ACTIVE "
        "before proceeding (§3.6.1: active → paused only)."
    )
    assert "InstanceLifecycleConflictError" in src


def test_resume_requires_paused_state():
    """resume_instance must reject non-paused states."""
    src = ast.unparse(
        next(
            n for n in ast.walk(_parse(SERVICE_PATH))
            if isinstance(n, ast.FunctionDef) and n.name == "resume_instance"
        )
    )
    assert "InstanceStatus.PAUSED" in src, (
        "resume_instance must enforce that current state is PAUSED "
        "(§3.6.1: paused → active only)."
    )
    assert "InstanceLifecycleConflictError" in src


def test_delete_rejects_already_grace_state():
    """delete_instance_with_grace must reject instances already in grace
    or deactivating state."""
    src = ast.unparse(
        next(
            n for n in ast.walk(_parse(SERVICE_PATH))
            if isinstance(n, ast.FunctionDef) and n.name == "delete_instance_with_grace"
        )
    )
    # The service must check for deactivating or grace states.
    assert "InstanceStatus.DEACTIVATING" in src or "DEACTIVATING" in src, (
        "delete_instance_with_grace must reject rows already in DEACTIVATING state."
    )
    assert "_is_grace_state" in src or "INSTANCE_GRACE_STATES" in src or "grace_window" in src, (
        "delete_instance_with_grace must check for grace_window / deleted states."
    )
    assert "InstanceLifecycleConflictError" in src


def test_restore_accepts_grace_state():
    """restore_instance must accept both 'deleted' and 'grace_window'
    via the _is_grace_state helper."""
    src = ast.unparse(
        next(
            n for n in ast.walk(_parse(SERVICE_PATH))
            if isinstance(n, ast.FunctionDef) and n.name == "restore_instance"
        )
    )
    assert "_is_grace_state" in src, (
        "restore_instance must call _is_grace_state() to accept both "
        "'deleted' (legacy) and 'grace_window' (new) states."
    )
    assert "InstanceLifecycleConflictError" in src


def test_instance_transition_role_error_exists():
    """InstanceTransitionRoleError must exist for role-gate violations."""
    src = _read(SERVICE_PATH)
    assert "class InstanceTransitionRoleError" in src, (
        "InstanceTransitionRoleError must be defined in instance_service.py "
        "for the §3.6.1 role-gate violations."
    )


def test_service_has_transition_table_docstring():
    """The module docstring must encode the §3.6.1 transition table."""
    src = _read(SERVICE_PATH)
    # Key transitions must appear in the docstring.
    # NOTE: 'manager' was removed from the required fragments in Unit 1 --
    # the single-login model (Locked Decision #19) collapsed all roles to
    # the single account owner; there is no manager role to document.
    for fragment in (
        "active",
        "paused",
        "deactivating",
        "grace_window",
        "owner",
    ):
        assert fragment in src, (
            f"instance_service.py docstring must document the §3.6.1 "
            f"transition table (missing fragment: '{fragment}')."
        )


# ======================================================================
# §3: Instance-level hard-delete cascade completeness.
# ======================================================================


# All tables the spec requires — both the original set and the newly
# added ones.  The retention worker must explicitly DELETE from each.
_REQUIRED_CASCADE_TABLES = (
    # Original tables (pre-TIER-DE):
    "knowledge_chunks",
    "knowledge_sources",
    "traces",
    "sessions",
    "api_keys",
    "instances",
    # Newly added per §3.6.5:
    "leads",
    "escalation_events",      # session summaries
    # sibling_call_grants / instance_composition_grants /
    # knowledge_share_grants / user_role_assignments REMOVED in Unit 1:
    # those tables were dropped (deferred multi-Luciel / custom-role
    # surfaces -- Open Decisions #7/#8, Locked Decision #19), so the
    # cascade no longer deletes from them.
    "instance_tool_authorizations",
    "byo_webhook_endpoints",
    "channel_routes",
    "tool_execution_log",
    "knowledge_graph_nodes",
    "knowledge_graph_edges",
    "instance_connections",
)


def test_cascade_deletes_every_required_table():
    """Worker cascade must hard-delete from every §3.6.5 table."""
    src = _read(WORKER_PATH)
    for table in _REQUIRED_CASCADE_TABLES:
        assert f"FROM {table}" in src or f"DELETE FROM {table}" in src, (
            f"Worker cascade must include DELETE FROM {table} per §3.6.5."
        )


def test_sibling_call_grants_cascade_removed():
    """Unit 1: sibling_call_grants was dropped (deferred multi-Luciel
    surface, Open Decision #7). The worker cascade must NOT reference
    the table -- a DELETE against it would crash UndefinedTable."""
    src = _read(WORKER_PATH)
    # The removal note in the source legitimately names the table; what
    # must be gone is any ACTIVE DELETE statement against it.
    assert "DELETE FROM sibling_call_grants" not in src, (
        "Worker cascade must not DELETE FROM the dropped "
        "sibling_call_grants table (Unit 1 excision)."
    )


def test_leads_deleted_for_gdpr():
    """leads (SET NULL FK) must be explicitly deleted for GDPR/PIPEDA
    completeness — a SET NULL is not sufficient for a hard-purge."""
    src = _read(WORKER_PATH)
    assert "DELETE FROM leads" in src, (
        "leads must be explicitly deleted per §3.6.5 GDPR/PIPEDA "
        "completeness (SET NULL would orphan the data, not purge it)."
    )


def test_escalation_events_deleted():
    """escalation_events (session summaries) must be explicitly deleted."""
    src = _read(WORKER_PATH)
    assert "DELETE FROM escalation_events" in src, (
        "escalation_events (session summaries) must be purged per §3.6.5."
    )


def test_audit_row_has_data_retention_flag():
    """The hard-purge audit row must carry data_retention_hard_delete=True
    in its after manifest for regulatory traceability."""
    src = _read(WORKER_PATH)
    assert "data_retention_hard_delete" in src, (
        "Audit row manifest must include data_retention_hard_delete for "
        "PIPEDA P5 / GDPR Art.17 traceability."
    )


def test_audit_tombstones_not_deleted():
    """The worker must not DELETE from admin_audit_logs.  Tombstones
    are the compliance record and must be preserved."""
    src = _read(WORKER_PATH)
    assert "DELETE FROM admin_audit_logs" not in src, (
        "Retention worker must never DELETE from admin_audit_logs; "
        "audit tombstones are the PIPEDA / GDPR compliance record."
    )


def test_audit_row_emitted_before_instance_deletion():
    """The FK admin_audit_logs.luciel_instance_id -> instances.id is
    RESTRICT; audit must be emitted while the instance row still
    exists."""
    src = _read(WORKER_PATH)
    tree = _parse(WORKER_PATH)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and n.name == "_hard_delete_instance_cascade"
    )
    body_src = ast.unparse(fn)
    audit_idx = body_src.index("ACTION_INSTANCE_HARD_PURGED")
    instance_delete_idx = body_src.index("DELETE FROM instances")
    assert audit_idx < instance_delete_idx, (
        "Audit emission must precede the instance row DELETE so the "
        "FK admin_audit_logs.luciel_instance_id -> instances.id stays satisfied."
    )


def test_restrict_tables_deleted_before_instance_row():
    """All RESTRICT-FK tables must appear in the cascade source before
    'DELETE FROM instances' to guarantee FK-safe ordering."""
    src = _read(WORKER_PATH)
    tree = _parse(WORKER_PATH)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and n.name == "_hard_delete_instance_cascade"
    )
    body_src = ast.unparse(fn)

    instance_delete_idx = body_src.index("DELETE FROM instances")

    # sibling_call_grants / instance_composition_grants /
    # knowledge_share_grants / user_role_assignments REMOVED in Unit 1
    # (dropped tables -- deferred multi-Luciel / custom-role surfaces).
    for table in (
        "instance_tool_authorizations",
        "byo_webhook_endpoints",
        "channel_routes",
        "tool_execution_log",
        "knowledge_sources",
        "instance_connections",
    ):
        table_idx = body_src.index(f"FROM {table}")
        assert table_idx < instance_delete_idx, (
            f"{table} (RESTRICT FK) must be deleted BEFORE 'instances' "
            f"to avoid FK violation on instance DELETE."
        )


# ======================================================================
# §4: Scan predicate includes both 'deleted' and 'grace_window'.
# ======================================================================


def test_scan_predicate_includes_grace_window():
    """TIER-DE: scan predicate must include 'grace_window' so new-code
    rows (written with status='grace_window') are picked up."""
    src = _read(WORKER_PATH)
    assert "grace_window" in src, (
        "Scan predicate must include grace_window status so rows "
        "written by the new 5-state code path are purged."
    )


def test_scan_predicate_still_includes_deleted():
    """TIER-DE backward-compat: scan must still include 'deleted' so
    legacy rows written before the enum extension are still purged."""
    src = _read(WORKER_PATH)
    assert "instance_status = 'deleted'" in src or "IN ('deleted'" in src, (
        "Scan predicate must still include 'deleted' for backward-compat "
        "with rows written before the TIER-DE migration."
    )


def test_scan_uses_in_predicate_for_both_states():
    """The scan SQL must use IN (...) to cover both states in one query."""
    src = _read(WORKER_PATH)
    assert "IN ('deleted', 'grace_window')" in src or "IN ('grace_window', 'deleted')" in src, (
        "Scan SQL must use IN ('deleted', 'grace_window') to cover both states."
    )


def test_scan_predicate_excludes_null_soft_deleted_at():
    """Retention clock is measured from soft_deleted_at; rows with NULL
    have not started the grace clock and must not be purged."""
    src = _read(WORKER_PATH)
    assert "soft_deleted_at IS NOT NULL" in src


def test_scan_predicate_ordered_by_oldest_first():
    src = _read(WORKER_PATH)
    assert "ORDER BY soft_deleted_at ASC" in src


# ======================================================================
# §5: Migration rescand_lifecycle_states.
# ======================================================================


def test_migration_file_exists():
    assert MIGRATION_PATH.exists(), (
        "alembic/versions/rescand_lifecycle_states.py must exist."
    )


def test_migration_revision_id():
    src = _read(MIGRATION_PATH)
    assert 'revision = "rescand_lifecycle_states"' in src


def test_migration_down_revision():
    src = _read(MIGRATION_PATH)
    assert 'down_revision = "rescanc_graph_kb"' in src


def test_migration_adds_deactivating():
    src = _read(MIGRATION_PATH)
    assert "deactivating" in src, (
        "Migration must ADD VALUE 'deactivating' to instance_status."
    )


def test_migration_adds_grace_window():
    src = _read(MIGRATION_PATH)
    assert "grace_window" in src, (
        "Migration must ADD VALUE 'grace_window' to instance_status."
    )


def test_migration_adds_hard_deleted():
    src = _read(MIGRATION_PATH)
    assert "hard_deleted" in src, (
        "Migration must ADD VALUE 'hard_deleted' to instance_status."
    )


def test_migration_uses_if_not_exists():
    """ADD VALUE must use IF NOT EXISTS for idempotency under replay."""
    src = _read(MIGRATION_PATH)
    assert "IF NOT EXISTS" in src, (
        "Migration must use ADD VALUE IF NOT EXISTS for idempotency."
    )


def test_migration_updates_partial_index():
    """Migration must drop and recreate ix_instances_soft_deleted_sweep
    to cover both 'deleted' and 'grace_window'."""
    src = _read(MIGRATION_PATH)
    assert "ix_instances_soft_deleted_sweep" in src
    assert "grace_window" in src


def test_migration_downgrade_documents_enum_no_op():
    """Downgrade docstring must document that ENUM values are not
    removed (PG does not support DROP VALUE)."""
    src = _read(MIGRATION_PATH)
    assert "LEFT IN PLACE" in src or "cannot" in src.lower() or "no-op" in src.lower() or "no op" in src.lower(), (
        "Migration downgrade must document that ENUM values remain "
        "(PG does not support ALTER TYPE ... DROP VALUE)."
    )


def test_migration_downgrade_restores_original_index():
    """Downgrade must recreate the original partial index shape."""
    src = _read(MIGRATION_PATH)
    tree = _parse(MIGRATION_PATH)
    downgrade_fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "downgrade"
    )
    downgrade_src = ast.unparse(downgrade_fn)
    assert "ix_instances_soft_deleted_sweep" in downgrade_src, (
        "downgrade() must recreate ix_instances_soft_deleted_sweep."
    )
    assert "DROP INDEX" in downgrade_src


# ======================================================================
# §6: Existing tests that previously asserted 3-state enum — in-lockstep
#     update check.
# ======================================================================


def test_old_lifecycle_test_does_not_hardcode_three_states_only():
    """The existing arc11_closeout test must not have a bare ``== 3``
    assertion on the enum member count, as we now have 6 members
    (5 canonical + 1 deprecated alias).

    This test guards against a test that would FAIL after the TIER-DE
    enum extension.  If such an assertion exists, it must be updated
    IN LOCKSTEP with this migration (see spec: 'update any test
    asserting only 3 states IN LOCKSTEP').

    We inspect the test file's source rather than running it — the
    intent is to detect a structural mismatch, not to run the old tests.
    """
    arc11_test = REPO_ROOT / "tests" / "db" / "test_instance_retention_worker.py"
    if not arc11_test.exists():
        pytest.skip("arc11 test file not present")
    src = arc11_test.read_text(encoding="utf-8")
    # The old test must not assert _exactly_ 3 enum values without
    # acknowledging the TIER-DE extension.  A bare '== 3' adjacent to
    # 'InstanceStatus' would indicate a stale assertion.
    # We allow the string '3' to appear in unrelated contexts (line
    # numbers, comments, etc.) but not in a direct enum-count assertion.
    if "len(InstanceStatus) == 3" in src:
        pytest.fail(
            "tests/db/test_instance_retention_worker.py contains "
            "'len(InstanceStatus) == 3' — this assertion is stale after "
            "the TIER-DE enum extension to 6 members.  Update it IN "
            "LOCKSTEP per the spec requirement."
        )
