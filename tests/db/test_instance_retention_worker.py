"""Arc 11 Closeout PR-A — instance retention worker contract tests.

Behavioural integration of the per-instance retention sweep (Customer
Journey §4.5 Phase 8 + Architecture §3.6.1: "30-day grace window,
then permanently deleted") lives in this file. The live-DB cascade
tests are gated on tests/db/conftest.py live fixtures landing in a
follow-up coverage pass; the assertions below are AST + text checks
that protect the worker's shape so a refactor cannot silently break
the retention contract.

Tests fall into three groups:

  1. Beat-schedule wiring (worker fires nightly, on the right queue).
  2. Scan predicate shape (30 days, instance_status='deleted',
     not-null soft_deleted_at).
  3. Per-instance cascade (knowledge_chunks, knowledge_sources,
     traces, sessions, api_keys, instances; audit row emitted
     before the instance row is dropped).
"""
from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_PATH = REPO_ROOT / "app" / "lifecycle" / "retention.py"
CELERY_APP_PATH = REPO_ROOT / "app" / "worker" / "celery_app.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _parse(p: Path) -> ast.Module:
    return ast.parse(_read(p))


# ---------------------------------------------------------------------
# Worker module exists and exports the canonical task name.
# ---------------------------------------------------------------------


def test_worker_module_exists():
    assert WORKER_PATH.exists(), (
        "app/worker/tasks/instance_retention.py must exist per Arc 11 "
        "Closeout PR-A spec."
    )


def test_worker_task_name_matches_celery_registration():
    """The @shared_task name must match the beat-schedule entry, or
    Celery will receive a beat ping for a task it has not registered."""
    src = _read(WORKER_PATH)
    assert (
        "app.lifecycle.retention.run_instance_retention_purge"
        in src
    ), "task name must be app.lifecycle.retention.run_instance_retention_purge."


def test_worker_function_is_decorated_shared_task():
    tree = _parse(WORKER_PATH)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and n.name == "run_instance_retention_purge"
    )
    decorators = [ast.unparse(d) for d in fn.decorator_list]
    assert any("shared_task" in d for d in decorators), (
        "run_instance_retention_purge must be decorated with @shared_task."
    )


# ---------------------------------------------------------------------
# Beat-schedule entry — 08:30 UTC, luciel-memory-tasks queue.
# ---------------------------------------------------------------------


def test_beat_schedule_includes_instance_retention_nightly():
    src = _read(CELERY_APP_PATH)
    assert "instance-retention-purge-nightly" in src, (
        "celery_app.beat_schedule must contain instance-retention-purge-nightly."
    )


def test_beat_schedule_fires_at_0830_utc():
    """Spec: 30 minutes after the tenant retention sweep so the two
    beat tasks do not contend for the worker's prefetch slot."""
    src = _read(CELERY_APP_PATH)
    # The crontab pair is on the line that follows the schedule key.
    idx = src.index("instance-retention-purge-nightly")
    block = src[idx: idx + 600]
    assert "crontab(hour=8, minute=30)" in block, (
        "instance retention beat entry must fire at 08:30 UTC."
    )


def test_beat_schedule_routes_to_memory_tasks_queue():
    src = _read(CELERY_APP_PATH)
    idx = src.index("instance-retention-purge-nightly")
    block = src[idx: idx + 600]
    assert '"luciel-memory-tasks"' in block


def test_celery_include_list_pulls_in_worker_module():
    src = _read(CELERY_APP_PATH)
    assert "app.lifecycle.retention" in src, (
        "celery_app.include must list app.lifecycle.retention "
        "or the worker boots without registering the task."
    )


# ---------------------------------------------------------------------
# Scan predicate — 30 days, instance_status='deleted', not-null soft_deleted_at.
# ---------------------------------------------------------------------


def test_scan_predicate_filters_on_status_deleted():
    src = _read(WORKER_PATH)
    assert "instance_status = 'deleted'" in src, (
        "Worker scan must filter on instance_status = 'deleted'."
    )


def test_scan_predicate_excludes_null_soft_deleted_at():
    src = _read(WORKER_PATH)
    assert "soft_deleted_at IS NOT NULL" in src


def test_scan_predicate_uses_30_day_cutoff():
    src = _read(WORKER_PATH)
    assert "INSTANCE_RESTORE_GRACE_DAYS" in src, (
        "Worker must source its 30-day cutoff from the repo's "
        "INSTANCE_RESTORE_GRACE_DAYS constant so both ends of the clock agree."
    )


def test_scan_predicate_ordered_by_oldest_first():
    src = _read(WORKER_PATH)
    assert "ORDER BY soft_deleted_at ASC" in src, (
        "Oldest deletes purge first so an interrupted nightly run picks "
        "up where it left off in FIFO order."
    )


# ---------------------------------------------------------------------
# Per-instance cascade — every customer-data table.
# ---------------------------------------------------------------------


_REQUIRED_CASCADE_TABLES = (
    "knowledge_chunks",
    "knowledge_sources",
    "traces",
    "sessions",
    "api_keys",
    "instances",
)


def test_cascade_deletes_every_required_table():
    src = _read(WORKER_PATH)
    for table in _REQUIRED_CASCADE_TABLES:
        assert f"FROM {table}" in src or f"DELETE FROM {table}" in src, (
            f"Worker cascade must hard-delete from {table}."
        )


def test_cascade_uses_ops_session_local():
    """OpsSessionLocal binds to the luciel_ops Postgres role (BYPASSRLS)
    so the cross-instance scan + DELETE chain runs without binding to a
    single ``app.admin_id`` GUC. Mirrors the role posture of
    app.worker.tasks.retention."""
    src = _read(WORKER_PATH)
    assert "from app.db.session import OpsSessionLocal" in src
    assert "OpsSessionLocal()" in src


def test_audit_row_emitted_before_instance_deletion():
    """The FK admin_audit_logs.luciel_instance_id -> instances.id is
    RESTRICT; audit must be emitted while the instance row still
    exists. Otherwise the cascade would fail at the audit-write step."""
    src = _read(WORKER_PATH)
    # Find the cascade function body.
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


def test_cascade_emits_hard_purged_audit_verb():
    src = _read(WORKER_PATH)
    assert "ACTION_INSTANCE_HARD_PURGED" in src, (
        "Per-instance hard-purge must emit ACTION_INSTANCE_HARD_PURGED "
        "so a regulator can filter the audit chain by verb."
    )
