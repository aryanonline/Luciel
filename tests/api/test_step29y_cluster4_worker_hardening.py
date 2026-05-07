"""Step 29.y Cluster 4 (4a) -- worker async-pipeline hardening.

Static-analysis (AST + bytes) + behavioural tests for the E-2,
E-3, and E-5 fixes documented in findings_phase1e.md. The 4b
sub-cluster (E-6 CloudWatch alarms, E-12 luciel_worker DB grants,
E-13 ECS task-def luciel_app role) is prod/IaC work and is
covered by separate prod-side runbooks, not by this file.

Invariants pinned here:

E-2 (rejection-audit idempotency)
  T1. The migration d8e2c4b1a0f3 file exists, declares revision
      d8e2c4b1a0f3 and down_revision c5d8a1e7b3f9, and contains
      a CREATE UNIQUE INDEX statement on
      (action, tenant_id, resource_natural_id) WHERE action LIKE
      'worker_%'.
  T2. AdminAuditRepository.record() accepts skip_on_conflict.
  T3. _reject_with_audit() in memory_extraction.py passes
      skip_on_conflict=True.

E-3 (autoretry tightening)
  T4. memory_extraction's @shared_task decorator does NOT use
      autoretry_for=(Exception,); the tuple is empty (or missing).
  T5. memory_extraction defines a _TRANSIENT_EXC tuple containing
      sqlalchemy.exc.OperationalError and redis.exceptions
      .ConnectionError.
  T6. The task body has explicit handlers for Reject, Retry,
      _TRANSIENT_EXC (calling self.retry), and a final
      ``except Exception`` that routes to _reject_with_audit
      using ACTION_WORKER_PERMANENT_FAILURE.
  T7. ACTION_WORKER_PERMANENT_FAILURE is registered in
      ALLOWED_ACTIONS.

E-5 (apply_async with headers)
  T8. memory_service.enqueue_extraction calls
      extract_memory_from_turn.apply_async(...) (NOT .delay) and
      passes a ``headers={"trace_id": ..., "tenant_id": ...}``
      argument.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

_HERE = pathlib.Path(__file__).resolve()
_PROJECT_ROOT = _HERE.parents[2]


def _read(rel: str) -> str:
    return (_PROJECT_ROOT / rel).read_text()


def _parse(rel: str) -> ast.Module:
    return ast.parse(_read(rel))


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


# =====================================================================
# E-2 tests
# =====================================================================

def test_e2_rejection_idempotency_migration_present_and_wired() -> None:
    mig = (
        _PROJECT_ROOT
        / "alembic"
        / "versions"
        / "d8e2c4b1a0f3_step29y_cluster4_worker_rejection_idempotency.py"
    )
    assert mig.exists(), (
        "E-2: migration "
        "d8e2c4b1a0f3_step29y_cluster4_worker_rejection_idempotency.py "
        f"is missing. Expected at {mig}."
    )
    src = mig.read_text()
    assert re.search(
        r'^revision\s*=\s*[\'"]d8e2c4b1a0f3[\'"]', src, re.MULTILINE
    ), "Cluster 4 migration must declare revision = 'd8e2c4b1a0f3'."
    assert re.search(
        r'^down_revision\s*=\s*[\'"]c5d8a1e7b3f9[\'"]', src, re.MULTILINE
    ), (
        "Cluster 4 migration must chain to c5d8a1e7b3f9 "
        "(the Cluster 3 head)."
    )
    # Partial unique index on the right columns.
    assert "CREATE UNIQUE INDEX" in src.upper(), (
        "Migration must create a UNIQUE INDEX."
    )
    for fragment in (
        "action",
        "tenant_id",
        "resource_natural_id",
        "worker_%",
    ):
        assert fragment in src, (
            f"E-2 migration body missing required fragment {fragment!r}."
        )


def test_e2_record_accepts_skip_on_conflict() -> None:
    tree = _parse("app/repositories/admin_audit_repository.py")
    fn = _find_function(tree, "record")
    kw_names = [a.arg for a in fn.args.kwonlyargs]
    assert "skip_on_conflict" in kw_names, (
        "E-2: AdminAuditRepository.record must accept "
        "skip_on_conflict as a keyword-only argument so worker "
        "rejection paths can opt into idempotent INSERTs against "
        "the d8e2c4b1a0f3 partial unique index."
    )


def test_e2_reject_with_audit_passes_skip_on_conflict() -> None:
    src = _read("app/worker/tasks/memory_extraction.py")
    # The audit.record(...) call inside _reject_with_audit is the
    # only one in this file; locate it and verify the keyword.
    tree = ast.parse(src)
    fn = _find_function(tree, "_reject_with_audit")
    record_call = None
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "record"
        ):
            record_call = node
            break
    assert record_call is not None, (
        "E-2: _reject_with_audit must call audit.record(...)."
    )
    skip = None
    for kw in record_call.keywords:
        if kw.arg == "skip_on_conflict":
            skip = kw
            break
    assert skip is not None, (
        "E-2: _reject_with_audit must pass skip_on_conflict to "
        "AdminAuditRepository.record so the d8e2c4b1a0f3 partial "
        "unique index makes ack-late-race redeliveries idempotent."
    )
    assert isinstance(skip.value, ast.Constant) and skip.value.value is True


# =====================================================================
# E-3 tests
# =====================================================================

def test_e3_autoretry_for_is_empty_tuple() -> None:
    """Pre-29.y the decorator carried autoretry_for=(Exception,)
    which caught Reject in some Celery 5.x versions. Cluster 4
    flips this to an empty tuple (or omits it) so retry is
    explicit via self.retry().

    Use AST so we inspect only the @shared_task(...) decorator call
    on extract_memory_from_turn -- not comments or docstrings that
    legitimately reference the historical buggy form.
    """
    import ast
    src = _read("app/worker/tasks/memory_extraction.py")
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name != "extract_memory_from_turn":
            continue
        for deco in node.decorator_list:
            if not isinstance(deco, ast.Call):
                continue
            for kw in deco.keywords:
                if kw.arg != "autoretry_for":
                    continue
                found = True
                # Must be a Tuple literal that is empty.
                assert isinstance(kw.value, ast.Tuple), (
                    "E-3: autoretry_for must be a tuple literal; "
                    "got %r" % type(kw.value).__name__
                )
                assert len(kw.value.elts) == 0, (
                    "E-3: autoretry_for must be empty -- "
                    "autoretry_for=(Exception,) catches Reject "
                    "and produces duplicate rejection audit rows. "
                    "Dispatch retries explicitly via self.retry()."
                )
    assert found, (
        "E-3: extract_memory_from_turn must declare an explicit "
        "autoretry_for=() so test verifies the empty form."
    )


def test_e3_transient_exception_tuple_defined() -> None:
    src = _read("app/worker/tasks/memory_extraction.py")
    assert "_TRANSIENT_EXC" in src, (
        "E-3: memory_extraction must define a _TRANSIENT_EXC "
        "tuple naming the exception classes that trigger a "
        "retry. Anything outside this tuple is permanent."
    )
    # And the tuple must include OperationalError + redis ConnectionError
    assert "OperationalError" in src, (
        "E-3: _TRANSIENT_EXC must include sqlalchemy "
        "OperationalError (DB transient failure)."
    )
    assert (
        "redis" in src and "ConnectionError" in src
    ), (
        "E-3: _TRANSIENT_EXC must include redis "
        "ConnectionError (broker transient failure)."
    )


def test_e3_task_body_has_explicit_retry_and_permanent_paths() -> None:
    src = _read("app/worker/tasks/memory_extraction.py")
    # except Reject:
    assert re.search(r"except\s+Reject\s*:", src), (
        "E-3: task body must catch Reject explicitly so the "
        "rejection path's pre-written audit row is preserved."
    )
    # except Retry:
    assert re.search(r"except\s+Retry\s*:", src), (
        "E-3: task body must catch Retry explicitly so "
        "self.retry()'s sentinel propagates without being "
        "re-classified as a permanent failure."
    )
    # except _TRANSIENT_EXC: ...
    assert re.search(r"except\s+_TRANSIENT_EXC", src), (
        "E-3: task body must catch _TRANSIENT_EXC explicitly "
        "and call self.retry()."
    )
    # self.retry() called inside body
    assert "self.retry(" in src, (
        "E-3: task body must call self.retry(exc=exc) for "
        "transient classes."
    )
    # Permanent failure path uses _reject_with_audit with the new action.
    assert "ACTION_WORKER_PERMANENT_FAILURE" in src, (
        "E-3: permanent failure branch must route through "
        "_reject_with_audit with ACTION_WORKER_PERMANENT_FAILURE."
    )


def test_e3_action_registered_in_allowed_actions() -> None:
    from app.models.admin_audit_log import (
        ACTION_WORKER_PERMANENT_FAILURE,
        ALLOWED_ACTIONS,
    )

    assert ACTION_WORKER_PERMANENT_FAILURE in ALLOWED_ACTIONS, (
        "E-3: ACTION_WORKER_PERMANENT_FAILURE must be in "
        "ALLOWED_ACTIONS or AdminAuditRepository.record will "
        "raise ValueError for the permanent-failure path."
    )


# =====================================================================
# E-5 tests
# =====================================================================

def test_e5_enqueue_uses_apply_async_with_headers() -> None:
    src = _read("app/memory/service.py")
    assert "extract_memory_from_turn.apply_async" in src, (
        "E-5: enqueue_extraction must call .apply_async(...), "
        "not .delay(), so we control kwargs and headers explicitly."
    )
    # And NOT use .delay anymore
    assert "extract_memory_from_turn.delay" not in src, (
        "E-5: stale .delay(...) call still present. Replace with "
        ".apply_async(kwargs=..., headers=...)."
    )

    # AST-level: confirm headers={trace_id, tenant_id} keys are present.
    tree = _parse("app/memory/service.py")
    fn = _find_function(tree, "enqueue_extraction")
    apply_async_call = None
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "apply_async"
        ):
            apply_async_call = node
            break
    assert apply_async_call is not None
    headers_kw = None
    for kw in apply_async_call.keywords:
        if kw.arg == "headers":
            headers_kw = kw
            break
    assert headers_kw is not None, (
        "E-5: apply_async must pass headers=... so trace_id "
        "and tenant_id flow through SQS MessageAttributes."
    )
    assert isinstance(headers_kw.value, ast.Dict)
    keys = [
        k.value
        for k in headers_kw.value.keys
        if isinstance(k, ast.Constant)
    ]
    for required in ("trace_id", "tenant_id"):
        assert required in keys, (
            f"E-5: headers dict must contain {required!r} "
            f"(saw keys={keys})."
        )


# =====================================================================
# Module imports.
# =====================================================================

@pytest.mark.parametrize(
    "module",
    [
        "app.repositories.admin_audit_repository",
        "app.worker.tasks.memory_extraction",
        "app.memory.service",
        "app.models.admin_audit_log",
    ],
)
def test_cluster4_modules_import(module: str) -> None:
    import importlib

    importlib.import_module(module)
