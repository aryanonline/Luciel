"""Arc 11 Closeout PR-A — repository-level contract for lifecycle methods.

Covers the shape + audit-action wiring of:

  * InstanceRepository.pause_by_pk
  * InstanceRepository.resume_by_pk
  * InstanceRepository.delete_by_pk
  * InstanceRepository.restore_by_pk

Behavioural integration tests (state transitions against a live DB)
land in tests/db/test_instance_retention_worker.py and the live
integration suite. The tests below are pure AST/source assertions
that protect:

  - Each method signature.
  - The correct audit verb per method.
  - The 30-day grace constant used by restore_by_pk and the worker.
  - The deprecated alias relationship deactivate_by_pk -> pause_by_pk.
"""
from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_PATH = REPO_ROOT / "app" / "repositories" / "instance_repository.py"


def _read() -> str:
    return REPO_PATH.read_text(encoding="utf-8")


def _parse() -> ast.Module:
    return ast.parse(_read())


def _method(name: str) -> ast.FunctionDef:
    tree = _parse()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"method {name!r} not found in instance_repository.py")


# ---------------------------------------------------------------------
# Grace-window constant — single source of truth.
# ---------------------------------------------------------------------


def test_grace_window_constant_is_thirty_days():
    """Architecture §3.6.1 locks the soft-delete window at 30 days.
    The constant lives in instance_repository so both ends of the
    clock (repo restore-by-pk + worker retention sweep) agree."""
    src = _read()
    assert "INSTANCE_RESTORE_GRACE_DAYS = 30" in src, (
        "INSTANCE_RESTORE_GRACE_DAYS must equal 30 per Architecture §3.6.1."
    )


# ---------------------------------------------------------------------
# pause_by_pk
# ---------------------------------------------------------------------


def test_pause_by_pk_signature():
    node = _method("pause_by_pk")
    args = [a.arg for a in node.args.args]
    assert args[0] == "self"
    assert "pk" in args
    kwonly = [a.arg for a in node.args.kwonlyargs]
    assert "audit_ctx" in kwonly, "pause_by_pk must take audit_ctx kw-only."


def test_pause_by_pk_emits_paused_audit_verb():
    src = ast.unparse(_method("pause_by_pk"))
    assert "ACTION_INSTANCE_PAUSED" in src, (
        "pause_by_pk must record ACTION_INSTANCE_PAUSED."
    )


def test_pause_by_pk_sets_status_to_paused():
    src = ast.unparse(_method("pause_by_pk"))
    assert "InstanceStatus.PAUSED" in src


def test_pause_by_pk_refuses_deleted_rows():
    """Lifecycle invariant: Pause is not a valid transition out of the
    'deleted' state -- the right verb for that case is Restore. The
    repo signals refusal by returning the row unchanged with no audit
    emission (route layer maps that to 409)."""
    src = ast.unparse(_method("pause_by_pk"))
    assert "InstanceStatus.DELETED" in src


# ---------------------------------------------------------------------
# resume_by_pk
# ---------------------------------------------------------------------


def test_resume_by_pk_emits_resumed_audit_verb():
    src = ast.unparse(_method("resume_by_pk"))
    assert "ACTION_INSTANCE_RESUMED" in src


def test_resume_by_pk_sets_status_to_active():
    src = ast.unparse(_method("resume_by_pk"))
    assert "InstanceStatus.ACTIVE" in src


def test_resume_by_pk_refuses_deleted_rows():
    src = ast.unparse(_method("resume_by_pk"))
    assert "InstanceStatus.DELETED" in src


# ---------------------------------------------------------------------
# delete_by_pk
# ---------------------------------------------------------------------


def test_delete_by_pk_emits_deleted_audit_verb():
    src = ast.unparse(_method("delete_by_pk"))
    assert "ACTION_INSTANCE_DELETED" in src


def test_delete_by_pk_stamps_soft_deleted_at():
    """Architecture §3.6.1 -- the grace clock is measured from
    soft_deleted_at. delete_by_pk must stamp it on the row."""
    src = ast.unparse(_method("delete_by_pk"))
    assert "soft_deleted_at" in src
    assert "datetime.now(timezone.utc)" in src or "datetime.now(tz=timezone.utc)" in src


def test_delete_by_pk_is_idempotent_on_already_deleted():
    """Spec: idempotent on already-deleted row -- preserve the original
    soft_deleted_at clock, do not emit a second audit row."""
    src = ast.unparse(_method("delete_by_pk"))
    assert "InstanceStatus.DELETED" in src


def test_delete_by_pk_has_no_sibling_grant_cascade():
    """Unit 1 (audit-and-alignment): the sibling-call-grant cascade was
    REMOVED. ``call_sibling_luciel`` and the ``sibling_call_grants``
    table are deferred-feature surfaces (multi-Luciel, Open Decision
    #7) excised in this unit. The single-Luciel model has no sibling
    grants, so ``delete_by_pk`` must NOT reference the deleted
    SiblingCallGrantService. This test pins the removal so the cascade
    is not accidentally reintroduced against a dropped table."""
    src = ast.unparse(_method("delete_by_pk"))
    assert "SiblingCallGrantService" not in src, (
        "Unit 1: delete_by_pk must NOT reference the deleted "
        "SiblingCallGrantService (multi-Luciel sibling grants deferred)."
    )
    assert "revoke_all_touching_instance" not in src, (
        "Unit 1: the sibling-grant cascade call must be removed from "
        "delete_by_pk (sibling_call_grants table dropped)."
    )


# ---------------------------------------------------------------------
# restore_by_pk
# ---------------------------------------------------------------------


def test_restore_by_pk_emits_restored_audit_verb():
    src = ast.unparse(_method("restore_by_pk"))
    assert "ACTION_INSTANCE_RESTORED" in src


def test_restore_by_pk_enforces_grace_window():
    src = ast.unparse(_method("restore_by_pk"))
    assert "INSTANCE_RESTORE_GRACE_DAYS" in src, (
        "restore_by_pk must consult INSTANCE_RESTORE_GRACE_DAYS for the "
        "30-day grace window per Architecture §3.6.1."
    )
    assert "timedelta" in src


def test_restore_by_pk_clears_soft_deleted_at():
    src = ast.unparse(_method("restore_by_pk"))
    # Either a literal None assignment or a `.soft_deleted_at = None`.
    assert ".soft_deleted_at = None" in src or "soft_deleted_at=None" in src


# ---------------------------------------------------------------------
# Deprecated alias: deactivate_by_pk -> pause_by_pk.
# ---------------------------------------------------------------------


def test_deactivate_by_pk_calls_pause_by_pk():
    src = ast.unparse(_method("deactivate_by_pk"))
    assert "self.pause_by_pk" in src, (
        "deactivate_by_pk must delegate to pause_by_pk per Arc 11 "
        "Closeout PR-A deprecation shim."
    )


def test_deactivate_by_pk_emits_deprecation_warning():
    src = ast.unparse(_method("deactivate_by_pk"))
    assert "warnings.warn" in src
    assert "DeprecationWarning" in src
