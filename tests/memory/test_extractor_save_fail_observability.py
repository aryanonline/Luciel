"""
Regression test for Step 28 C8 (P3-O):
    Extractor save-time failure observability.

CONTRACT GUARDED:
    1. The except block in MemoryService.extract_and_save MUST log
       repr(exc) (not just type(exc).__name__), so integrity
       violations / FK errors / pgvector dimension mismatches surface
       the literal Postgres message instead of being type-only.
    2. The except block MUST emit an AdminAuditLog row with action
       ACTION_EXTRACTOR_SAVE_FAIL, so compliance has a durable record
       of every save-time failure.
    3. The audit-row emission MUST be guarded by its own try/except so
       a downstream audit-write failure cannot break the chat turn
       (fail-open contract).

THE BUG THIS GUARDS AGAINST:
    Pillar 13 A3 (May 4 2026): a D11 NOT NULL violation on
    actor_user_id was completely invisible because the except block
    logged only type(exc).__name__. Diagnosis took ~2 hours. The
    repr(exc) would have surfaced
        "null value in column actor_user_id violates not-null
         constraint"
    in the first log read.

    A regression where a future maintainer reverts to type-only
    logging or removes the audit emission would re-create the same
    silent-failure surface this commit closes.

WHY AST INSTEAD OF DB:
    Following the convention of
    tests/middleware/test_actor_user_id_binding.py: source-level AST
    proof catches the regression first and fastest, runs without any
    app dependencies installed (no sqlalchemy / pgvector / etc), and
    survives in CI sandboxes that cannot stand up Postgres.

RUN:
    python -m pytest tests/memory/test_extractor_save_fail_observability.py -v
    OR (no pytest needed):
    python tests/memory/test_extractor_save_fail_observability.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

# Allow running via `python tests/memory/test_extractor_save_fail_observability.py`
# from any cwd by inserting the project root on sys.path before imports.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_SERVICE_PATH = _PROJECT_ROOT / "app" / "memory" / "service.py"
_AUDIT_MODEL_PATH = _PROJECT_ROOT / "app" / "models" / "admin_audit_log.py"


def _find_extract_and_save(tree: ast.AST) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "extract_and_save":
            return node
    raise AssertionError(
        "extract_and_save not found in app/memory/service.py"
    )


def _save_loop_excepts(func: ast.FunctionDef) -> list[ast.ExceptHandler]:
    """Return the except handlers for the per-item save try/except.

    The body of extract_and_save contains a `for item in extracted:`
    loop wrapping a try/except. We collect every ExceptHandler reachable
    from the function body so the test is robust to inner re-shape.
    """
    handlers: list[ast.ExceptHandler] = []
    for node in ast.walk(func):
        if isinstance(node, ast.ExceptHandler):
            handlers.append(node)
    return handlers


# ---------------------------------------------------------------- test 1
def test_action_extractor_save_fail_constant_exists() -> None:
    """ACTION_EXTRACTOR_SAVE_FAIL must be defined and in ALLOWED_ACTIONS.

    Without this, AdminAuditRepository.record will raise ValueError
    on every extractor save failure -- which would defeat the whole
    durable-record purpose of the change.
    """
    src = _AUDIT_MODEL_PATH.read_text()
    assert 'ACTION_EXTRACTOR_SAVE_FAIL = "extractor_save_fail"' in src, (
        "ACTION_EXTRACTOR_SAVE_FAIL constant missing from "
        "app/models/admin_audit_log.py"
    )
    # The constant must appear inside the ALLOWED_ACTIONS tuple.
    tree = ast.parse(src)
    allowed_assign = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "ALLOWED_ACTIONS"):
            allowed_assign = node
            break
    assert allowed_assign is not None, "ALLOWED_ACTIONS tuple not found"
    assert isinstance(allowed_assign.value, ast.Tuple), (
        "ALLOWED_ACTIONS must be a tuple literal"
    )
    member_names = [
        elt.id for elt in allowed_assign.value.elts
        if isinstance(elt, ast.Name)
    ]
    assert "ACTION_EXTRACTOR_SAVE_FAIL" in member_names, (
        "ACTION_EXTRACTOR_SAVE_FAIL missing from ALLOWED_ACTIONS"
    )


# ---------------------------------------------------------------- test 2
def test_save_loop_except_logs_repr_of_exception() -> None:
    """The save-loop except handler must log %r against the exception.

    Type-only logging is the regression we are guarding against.
    Catching either repr(exc) or %r in a logger.warning call counts
    as compliance.
    """
    src = _SERVICE_PATH.read_text()
    tree = ast.parse(src)
    func = _find_extract_and_save(tree)
    handlers = _save_loop_excepts(func)
    assert handlers, "extract_and_save has no except handlers"

    found_repr = False
    for h in handlers:
        if h.name is None:
            continue
        bound = h.name  # the 'as <name>' identifier
        for sub in ast.walk(h):
            # Match logger.warning(... "%r" ..., exc, ...) or repr(exc).
            if isinstance(sub, ast.Call):
                # repr(exc) call form
                if (isinstance(sub.func, ast.Name)
                        and sub.func.id == "repr"
                        and any(isinstance(a, ast.Name) and a.id == bound
                                for a in sub.args)):
                    found_repr = True
                    break
                # %r format-arg form: any string-literal arg containing
                # %r AND the exception name appearing as a positional
                # argument to the same call.
                str_args = [
                    a for a in sub.args
                    if isinstance(a, ast.Constant)
                    and isinstance(a.value, str)
                ]
                has_pct_r = any("%r" in s.value for s in str_args)
                has_exc_arg = any(
                    isinstance(a, ast.Name) and a.id == bound
                    for a in sub.args
                )
                if has_pct_r and has_exc_arg:
                    found_repr = True
                    break
        if found_repr:
            break

    assert found_repr, (
        "Save-loop except handler does not surface repr(exc) / %r. "
        "Type-only logging is the P3-O regression we guard against. "
        "See tests/memory/test_extractor_save_fail_observability.py."
    )


# ---------------------------------------------------------------- test 3
def test_save_loop_except_emits_admin_audit_row() -> None:
    """The save-loop except handler must call AdminAuditRepository.record
    with action=ACTION_EXTRACTOR_SAVE_FAIL.

    This guards against silent removal of the durable audit row -- the
    second leg of the P3-O fix. Without this, save-time failures would
    revert to log-only observability.
    """
    src = _SERVICE_PATH.read_text()
    tree = ast.parse(src)
    func = _find_extract_and_save(tree)
    handlers = _save_loop_excepts(func)
    assert handlers, "extract_and_save has no except handlers"

    found_audit = False
    for h in handlers:
        for sub in ast.walk(h):
            if not isinstance(sub, ast.Call):
                continue
            # Look for AdminAuditRepository(...).record(...).
            func_node = sub.func
            if not isinstance(func_node, ast.Attribute):
                continue
            if func_node.attr != "record":
                continue
            # Inspect kwargs for action=ACTION_EXTRACTOR_SAVE_FAIL.
            for kw in sub.keywords:
                if kw.arg != "action":
                    continue
                v = kw.value
                if (isinstance(v, ast.Name)
                        and v.id == "ACTION_EXTRACTOR_SAVE_FAIL"):
                    found_audit = True
                    break
                if (isinstance(v, ast.Constant)
                        and v.value == "extractor_save_fail"):
                    found_audit = True
                    break
            if found_audit:
                break
        if found_audit:
            break

    assert found_audit, (
        "Save-loop except handler does not emit an AdminAuditRepository "
        "record with action=ACTION_EXTRACTOR_SAVE_FAIL. The durable "
        "audit row is the second leg of the P3-O fix; without it, "
        "save-time failures revert to log-only observability."
    )


# ---------------------------------------------------------------- test 4
def test_audit_row_emission_is_wrapped_in_its_own_try() -> None:
    """The audit-row emission must be guarded by its own try/except.

    If the audit-write fails (e.g. session poisoned by an earlier
    IntegrityError, or a transient DB issue), it must NOT propagate
    out of extract_and_save. The chat turn must remain unaffected.

    This test asserts that the AdminAuditRepository.record call lives
    inside a try/except whose handler is itself inside the outer
    save-loop except (i.e. nested try/except), so the audit failure
    is swallowed.
    """
    src = _SERVICE_PATH.read_text()
    tree = ast.parse(src)
    func = _find_extract_and_save(tree)

    # For each outer save-loop ExceptHandler, look for a Try node
    # inside it whose body contains the AdminAuditRepository.record
    # call. The Try node's handlers list must not be empty -- i.e.,
    # the audit-row code is wrapped in its own try/except.
    nested_try_protects_audit = False
    for outer in ast.walk(func):
        if not isinstance(outer, ast.ExceptHandler):
            continue
        for inner in ast.walk(outer):
            if not isinstance(inner, ast.Try):
                continue
            # Does the inner Try contain a record() call?
            calls_record = False
            for sub in ast.walk(inner):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "record"):
                    calls_record = True
                    break
            if calls_record and len(inner.handlers) >= 1:
                nested_try_protects_audit = True
                break
        if nested_try_protects_audit:
            break

    assert nested_try_protects_audit, (
        "AdminAuditRepository.record call must be wrapped in its own "
        "try/except inside the save-loop except handler, so an audit-"
        "write failure cannot break the chat turn (fail-open contract)."
    )


# ----------------------------------------------------------------- main
if __name__ == "__main__":
    test_action_extractor_save_fail_constant_exists()
    print("PASS test_action_extractor_save_fail_constant_exists")
    test_save_loop_except_logs_repr_of_exception()
    print("PASS test_save_loop_except_logs_repr_of_exception")
    test_save_loop_except_emits_admin_audit_row()
    print("PASS test_save_loop_except_emits_admin_audit_row")
    test_audit_row_emission_is_wrapped_in_its_own_try()
    print("PASS test_audit_row_emission_is_wrapped_in_its_own_try")
    print("ALL P3-O REGRESSION TESTS PASSED")
