"""Arc 10 regression tests -- 30-day retention window + tombstone.

Protects three founder locks from this arc:

  L1  Single 30-day clock supersedes the prior 90-day lock.
      RETENTION_WINDOW_DAYS = 30 (not 90).

  L10 Scan predicate keys off admins.closure_initiated_at, not
      tenant_configs.deactivated_at. The tenant_configs fallback in
      hard_delete_tenant_after_retention is removed.

  L13 Step 11 of the cascade is a tombstone UPDATE, not a row DELETE.
      Vision 6.5 minimal-compliance-record reading. PII columns
      are redacted; the row persists; the audit chain's
      resource_natural_id references stay walkable.

Test strategy mirrors the Arc 8 WU-6 doctrine: AST / text assertions
against the shipped source. The retention worker, the cascade
service, and the migration are static-text fixtures; we pin the
exact public surface and wiring rather than executing a live DB
path (which would need a Postgres fixture with the Arc 10 migration
applied).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
RETENTION_PATH = REPO_ROOT / "app" / "worker" / "tasks" / "retention.py"
ADMIN_SERVICE_PATH = REPO_ROOT / "app" / "services" / "admin_service.py"
MIGRATION_PATH = (
    REPO_ROOT / "alembic" / "versions" / "arc10_lifecycle_subsystem.py"
)


# ---------------------------------------------------------------------
# L1: RETENTION_WINDOW_DAYS is 30, not 90.
# ---------------------------------------------------------------------

def test_retention_window_days_is_30():
    """Founder lock L1: 30-day clock per Vision 6.5."""
    src = RETENTION_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "RETENTION_WINDOW_DAYS"
        ):
            target = node
            break
    assert target is not None, (
        "RETENTION_WINDOW_DAYS constant not found in retention.py"
    )
    assert isinstance(target.value, ast.Constant)
    assert target.value.value == 30, (
        f"RETENTION_WINDOW_DAYS must be 30 (Arc 10 L1 lock per "
        f"Vision 6.5). Found {target.value.value}. If the value was "
        f"intentionally changed, this test must be amended in the "
        f"same commit with a documented founder approval."
    )


def test_retention_window_supersession_comment_present():
    """The 30-day lock must carry the supersession comment.

    The comment trail is part of the doctrine -- if the constant is
    ever bumped back, the supersession reasoning must be re-stated
    in the same commit.
    """
    src = RETENTION_PATH.read_text(encoding="utf-8")
    # The comment block is the source-of-truth narrative for why we
    # collapsed 90 -> 30. It must reference Vision 6.5 by name.
    assert "Vision" in src and "6.5" in src, (
        "Retention worker source must reference Vision 6.5 in its "
        "RETENTION_WINDOW_DAYS lock comment."
    )
    assert "Supersedes" in src or "supersedes" in src, (
        "Retention worker source must explicitly state that the "
        "30-day lock supersedes the prior 90-day lock."
    )


# ---------------------------------------------------------------------
# L10: scan predicate uses closure_initiated_at, not deactivated_at.
# ---------------------------------------------------------------------

def test_scan_predicate_keys_off_closure_initiated_at():
    """Founder lock L10: closure is the only hard-delete trigger.

    The _scan_eligible_tenants SQL must filter on
    admins.closure_initiated_at, and must NOT filter on
    tenant_configs.deactivated_at (the legacy predicate).
    """
    src = RETENTION_PATH.read_text(encoding="utf-8")

    # Locate the _scan_eligible_tenants function.
    tree = ast.parse(src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_scan_eligible_tenants":
            fn = node
            break
    assert fn is not None, "_scan_eligible_tenants not found"

    fn_src = ast.get_source_segment(src, fn)
    assert fn_src is not None

    assert "closure_initiated_at" in fn_src, (
        "Scan predicate must filter on admins.closure_initiated_at "
        "per Arc 10 L10."
    )
    assert "FROM admins" in fn_src, (
        "Scan must SELECT FROM admins, not tenant_configs."
    )
    assert "hard_deleted_at IS NULL" in fn_src, (
        "Scan must exclude already-tombstoned rows via "
        "hard_deleted_at IS NULL."
    )
    # Negative assertions: the legacy table / column must NOT appear
    # in the live scan SQL.
    assert "FROM tenant_configs" not in fn_src, (
        "Arc 10 L10: tenant_configs fallback removed; scan must "
        "not query the legacy table."
    )


# ---------------------------------------------------------------------
# L10: cascade no longer falls back to tenant_configs DELETE.
# ---------------------------------------------------------------------

def test_cascade_does_not_fall_back_to_tenant_configs_delete():
    """Founder lock L10: tenant_configs fallback removed from cascade.

    The hard_delete_tenant_after_retention method must not contain
    a 'DELETE FROM tenant_configs' SQL clause. The Arc 5 rename ->
    admins is complete; the drift-reconciliation backfill ran in the
    Arc 10 migration.
    """
    src = ADMIN_SERVICE_PATH.read_text(encoding="utf-8")

    # Pull the method body via AST so we don't false-positive on
    # comments elsewhere in the file.
    tree = ast.parse(src)
    method = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "hard_delete_tenant_after_retention"
        ):
            method = node
            break
    assert method is not None, (
        "hard_delete_tenant_after_retention not found in admin_service.py"
    )

    method_src = ast.get_source_segment(src, method)
    assert method_src is not None

    # The cascade body should not contain a literal SQL DELETE
    # against tenant_configs. (Comments are allowed to reference the
    # historical fallback; we filter to non-comment lines.)
    non_comment_lines = [
        line for line in method_src.splitlines()
        if not line.lstrip().startswith("#")
    ]
    non_comment_src = "\n".join(non_comment_lines)
    assert "DELETE FROM tenant_configs" not in non_comment_src, (
        "Arc 10 L10: cascade must not DELETE FROM tenant_configs."
    )


# ---------------------------------------------------------------------
# L13: step 11 is a tombstone UPDATE, not a row DELETE.
# ---------------------------------------------------------------------

def test_cascade_step_11_is_tombstone_update():
    """Founder lock L13: tombstone admins row at hard-delete.

    The cascade must:
      * UPDATE admins ... SET hard_deleted_at = now()
      * NOT DELETE FROM admins
      * Redact name and stripe_customer_id (PII)
    """
    src = ADMIN_SERVICE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    method = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "hard_delete_tenant_after_retention"
        ):
            method = node
            break
    assert method is not None
    method_src = ast.get_source_segment(src, method)
    assert method_src is not None
    non_comment_lines = [
        line for line in method_src.splitlines()
        if not line.lstrip().startswith("#")
    ]
    non_comment_src = "\n".join(non_comment_lines)

    # The tombstone UPDATE shape must be present.
    assert re.search(r"UPDATE\s+admins", non_comment_src), (
        "Step 11 must UPDATE admins (tombstone)."
    )
    assert "hard_deleted_at" in non_comment_src and "now()" in non_comment_src, (
        "Step 11 tombstone must stamp hard_deleted_at = now()."
    )
    # PII redaction must be in the SQL.
    assert "[REDACTED]" in non_comment_src, (
        "Step 11 must redact the admin's name to '[REDACTED]'."
    )
    assert "stripe_customer_id = NULL" in non_comment_src, (
        "Step 11 must NULL the admin's stripe_customer_id."
    )
    # The destructive DELETE must NOT be in the method body.
    assert "DELETE FROM admins" not in non_comment_src, (
        "Arc 10 L13: step 11 must NOT DELETE FROM admins (tombstone, "
        "not delete)."
    )


def test_cascade_default_retention_window_is_30():
    """The method's default retention_window_days parameter is 30."""
    src = ADMIN_SERVICE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    method = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "hard_delete_tenant_after_retention"
        ):
            method = node
            break
    assert method is not None
    # The defaults list is positional-only after the * keyword marker.
    # Pull the kw-only defaults from the signature.
    found_default = None
    for kwarg, default in zip(method.args.kwonlyargs, method.args.kw_defaults):
        if kwarg.arg == "retention_window_days":
            assert isinstance(default, ast.Constant)
            found_default = default.value
            break
    assert found_default == 30, (
        f"hard_delete_tenant_after_retention default retention_window_days "
        f"must be 30 (Arc 10 L1). Found {found_default}."
    )


# ---------------------------------------------------------------------
# Migration sanity -- the columns the new scan predicate needs exist.
# ---------------------------------------------------------------------

def test_migration_adds_closure_initiated_at_and_hard_deleted_at():
    """The Arc 10 migration must add the columns the new code reads."""
    src = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "closure_initiated_at" in src, (
        "Migration must add admins.closure_initiated_at."
    )
    assert "hard_deleted_at" in src, (
        "Migration must add admins.hard_deleted_at."
    )
    assert "ix_admins_closure_clock_eligible" in src, (
        "Migration must create the partial index backing the new scan."
    )


# ---------------------------------------------------------------------
# Worker session: uses BYPASSRLS role, removed the C6 guard.
# ---------------------------------------------------------------------

def test_retention_worker_uses_ops_session_local():
    """Arc 10 paired code change: worker switched to OpsSessionLocal."""
    src = RETENTION_PATH.read_text(encoding="utf-8")
    assert "OpsSessionLocal" in src, (
        "Retention worker must use OpsSessionLocal (luciel_ops "
        "BYPASSRLS role) per Arc 10 paired change."
    )
    # The bare SessionLocal references should be gone from runtime
    # code paths (comments allowed).
    tree = ast.parse(src)
    # Find any name reference to SessionLocal outside a comment.
    bare_session_local = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "SessionLocal":
            bare_session_local = True
            break
    assert not bare_session_local, (
        "Retention worker must not reference SessionLocal at runtime "
        "(use OpsSessionLocal). Bare SessionLocal re-introduces the "
        "Wall-3 gap C6.1 was created to close."
    )


def test_retention_worker_removed_rls_tenant_context_guard():
    """The rls_tenant_context_enabled guard is removed.

    Under the BYPASSRLS role, the guard is no longer needed; keeping
    it would mask C6 failures in tests where the flag is set.
    """
    src = RETENTION_PATH.read_text(encoding="utf-8")
    # Comments are allowed to reference the historical guard; we
    # filter to runtime code via AST. Look for any If node whose test
    # is `settings.rls_tenant_context_enabled`.
    tree = ast.parse(src)
    found_guard = False
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test_src = ast.dump(node.test)
            if "rls_tenant_context_enabled" in test_src:
                found_guard = True
                break
    assert not found_guard, (
        "Arc 10: the rls_tenant_context_enabled guard must be removed "
        "from retention.py. BYPASSRLS via OpsSessionLocal makes it "
        "unnecessary; keeping it masks regression of the BYPASSRLS "
        "wiring."
    )
