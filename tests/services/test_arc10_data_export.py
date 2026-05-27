"""Arc 10 re-open Gap 5 -- DataExportService contract regression.

Locks the service-layer surface that the original arc shipped without
test coverage:

  C1  Service class + four well-known exception classes exist on
      app.services.data_export_service (DataExportError base +
      ExportAlreadyInFlight + ExportNotReady + ExportNotFound +
      ExportGenerationError).

  C2  Public methods that the route layer depends on:
        * enqueue(admin_id, tier, triggered_by) -> DataExportJob
        * generate_bundle(job_id) -> None
        * get_signed_url(job_id, admin_id) -> (url, expires_at)

  C3  Architecture 3.6.3 bundle shape: the generate_bundle method
      assembles a bundle whose contents include conversations,
      knowledge (chunks-only per Arc 10 Option 2; originals are
      Arc 11), audit log, and instance configs. Asserted via grep
      against the shipped source.

  C4  One-active-per-admin lock: the data_export_jobs table has a
      partial unique index on (admin_id) WHERE status IN
      ('pending','generating'). This is the database-level guard
      that backs ExportAlreadyInFlightError.

  C5  RLS on data_export_jobs is enabled with both SELECT/USING and
      INSERT/WITH-CHECK policies keyed on app.admin_id (fail-closed
      tenant isolation, matching every other Arc 9 customer-data
      table).

  C6  Celery beat schedule: app/worker/tasks/data_export.py is
      registered on the beat schedule via app/worker/celery_app.py
      so the bundle generator actually runs.

  C7  Worker reads the bucket name from settings.data_export_bucket
      with the literal default 'luciel-data-exports'. This matches
      the bucket name created in Arc 10 infra (Gap 0c) and the IAM
      grant on luciel-ecs-worker-role.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_PATH = REPO_ROOT / "app" / "services" / "data_export_service.py"
TASK_PATH = REPO_ROOT / "app" / "worker" / "tasks" / "data_export.py"
CELERY_PATH = REPO_ROOT / "app" / "worker" / "celery_app.py"
MIGRATION_PATH = (
    REPO_ROOT / "alembic" / "versions" / "arc10_lifecycle_subsystem.py"
)
CONFIG_PATH = REPO_ROOT / "app" / "core" / "config.py"


def _parse(p: Path) -> ast.Module:
    return ast.parse(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------
# C1: service + four exception classes.
# ---------------------------------------------------------------------

def test_data_export_service_class_exists():
    tree = _parse(SERVICE_PATH)
    classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    assert "DataExportService" in classes


@pytest.mark.parametrize(
    "name",
    [
        "DataExportError",
        "ExportAlreadyInFlightError",
        "ExportNotReadyError",
        "ExportNotFoundError",
        "ExportGenerationError",
    ],
)
def test_data_export_exceptions_exist(name: str):
    tree = _parse(SERVICE_PATH)
    classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    assert name in classes, (
        f"app.services.data_export_service must expose {name!r} so the "
        "route layer can map service errors to HTTP statuses."
    )


def test_export_already_in_flight_extends_data_export_error():
    tree = _parse(SERVICE_PATH)
    cls = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "ExportAlreadyInFlightError"
    )
    bases = [b.id for b in cls.bases if isinstance(b, ast.Name)]
    assert "DataExportError" in bases, (
        "ExportAlreadyInFlightError must extend DataExportError so the "
        "route layer can catch the whole family with one except clause."
    )


# ---------------------------------------------------------------------
# C2: public methods.
# ---------------------------------------------------------------------

def _service_method_names() -> set[str]:
    tree = _parse(SERVICE_PATH)
    cls = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "DataExportService"
    )
    return {
        n.name for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


@pytest.mark.parametrize("method", ["enqueue", "generate_bundle", "get_signed_url"])
def test_data_export_service_method_exists(method: str):
    assert method in _service_method_names(), (
        f"DataExportService.{method} is part of the route-layer contract; "
        "removing it would break /api/v1/admin/account/export[*]."
    )


def test_enqueue_signature_required_args():
    tree = _parse(SERVICE_PATH)
    method = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "enqueue"
    )
    # First arg is self; following positional/keyword args must include
    # admin_id, tier (or tier_at_request), triggered_by.
    arg_names = [a.arg for a in method.args.args] + [a.arg for a in method.args.kwonlyargs]
    for required in ("admin_id", "triggered_by"):
        assert required in arg_names, (
            f"enqueue must accept {required}; got args {arg_names}"
        )
    # Tier is either 'tier' or 'tier_at_request' depending on naming.
    assert "tier" in arg_names or "tier_at_request" in arg_names, (
        f"enqueue must accept tier or tier_at_request; got {arg_names}"
    )


# ---------------------------------------------------------------------
# C3: Architecture 3.6.3 bundle contents.
# ---------------------------------------------------------------------

def test_bundle_includes_conversations_knowledge_audit_instances():
    """Architecture 3.6.3 mandates the bundle composition. We grep the
    source for the canonical artifact prefixes -- a contract pin, not a
    runtime assertion. If anyone removes one of these, the test fails
    so the doctrine drift surfaces at PR time."""
    src = SERVICE_PATH.read_text(encoding="utf-8")
    expected_prefixes = [
        "conversations",
        "knowledge",
        "audit",
        "instances",
    ]
    for prefix in expected_prefixes:
        # Match either a bundle path ('conversations.jsonl',
        # 'knowledge_sources/...') or a method/section comment.
        assert prefix in src.lower(), (
            f"Architecture 3.6.3 bundle must include {prefix}; "
            f"reference not found in {SERVICE_PATH.name}"
        )


def test_bundle_documents_knowledge_originals_deferred_to_arc11():
    """Option 2 from the Arc 10 design: knowledge is reconstructed
    from chunks in the v1 bundle; originals come in Arc 11. The
    service docstring must document this so customers reading the
    bundle understand why their PDFs aren't there."""
    src = SERVICE_PATH.read_text(encoding="utf-8").lower()
    assert "arc 11" in src or "arc11" in src, (
        "DataExportService docstring must reference Arc 11 as the home "
        "for original knowledge file persistence (Option 2 deferral)."
    )


# ---------------------------------------------------------------------
# C4: one-active-per-admin partial unique index.
# ---------------------------------------------------------------------

def test_one_active_export_per_admin_unique_index_exists():
    src = MIGRATION_PATH.read_text(encoding="utf-8")
    # Match the index by name + the partial WHERE clause.
    assert "ux_data_export_jobs_one_active_per_admin" in src, (
        "Migration must declare ux_data_export_jobs_one_active_per_admin"
    )
    # And it must be a UNIQUE index filtered on pending/generating only.
    pat = re.search(
        r"CREATE UNIQUE INDEX ux_data_export_jobs_one_active_per_admin"
        r".*?WHERE status IN \('pending', 'generating'\)",
        src, re.DOTALL,
    )
    assert pat, (
        "ux_data_export_jobs_one_active_per_admin must be a partial UNIQUE "
        "index WHERE status IN ('pending', 'generating'). This is the DB-"
        "level lock that backs ExportAlreadyInFlightError."
    )


# ---------------------------------------------------------------------
# C5: RLS on data_export_jobs.
# ---------------------------------------------------------------------

def test_data_export_jobs_rls_enabled():
    src = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "ALTER TABLE data_export_jobs ENABLE ROW LEVEL SECURITY" in src


def test_data_export_jobs_select_policy_exists():
    src = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "data_export_jobs_admin_isolation" in src
    # USING clause keys on app.admin_id GUC.
    assert (
        "USING (admin_id = current_setting('app.admin_id', true)::text)"
        in src
    ), "RLS SELECT policy must fail-closed on missing app.admin_id GUC."


def test_data_export_jobs_insert_policy_exists():
    src = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "data_export_jobs_admin_isolation_write" in src
    assert "FOR INSERT" in src
    assert (
        "WITH CHECK (admin_id = current_setting('app.admin_id', true)::text)"
        in src
    ), "RLS INSERT policy must enforce admin_id matches GUC."


# ---------------------------------------------------------------------
# C6: Celery beat registration.
# ---------------------------------------------------------------------

def test_data_export_task_registered_on_beat_schedule():
    src = CELERY_PATH.read_text(encoding="utf-8")
    # The task lives at app.worker.tasks.data_export -- the beat schedule
    # must reference it by module path or task name.
    assert (
        "data_export" in src
    ), "Celery beat schedule must register the data_export task."


def test_data_export_task_uses_ops_session_for_bypassrls():
    """The data export worker has to read across all admins (otherwise
    the per-admin RLS policies block it). Same pattern as the retention
    worker (Arc 10 + Arc 9 C6.3)."""
    src = TASK_PATH.read_text(encoding="utf-8")
    # Either OpsSessionLocal import or explicit GUC-set pattern.
    assert (
        "OpsSessionLocal" in src
        or "luciel_ops" in src
        or "BYPASSRLS" in src
    ), (
        "data_export worker must use OpsSessionLocal (or an explicit "
        "BYPASSRLS path) -- otherwise per-admin RLS blocks cross-admin "
        "reads needed for the bundle generation."
    )


# ---------------------------------------------------------------------
# C7: bucket name source-of-truth.
# ---------------------------------------------------------------------

def test_settings_default_data_export_bucket():
    src = CONFIG_PATH.read_text(encoding="utf-8")
    pat = re.search(
        r'data_export_bucket\s*:\s*str\s*=\s*"luciel-data-exports"',
        src,
    )
    assert pat, (
        "settings.data_export_bucket default must be 'luciel-data-exports' "
        "-- this is the bucket created in Arc 10 infra and the IAM grant "
        "on luciel-ecs-worker-role is scoped to that ARN. Changing the "
        "default without updating IAM produces a silent 403 at runtime."
    )


def test_worker_reads_bucket_via_settings():
    src = TASK_PATH.read_text(encoding="utf-8")
    assert "data_export_bucket" in src, (
        "Worker task must read settings.data_export_bucket (not hardcode)."
    )
