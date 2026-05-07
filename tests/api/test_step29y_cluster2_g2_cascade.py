"""Step 29.y Cluster 2 -- G-2 forensic-toggle memory cascade.

Static-analysis (AST + bytes) tests for findings_phase1g.md G-2:

The route POST /admin/forensics/luciel_instances_step29c/{instance_id}
/toggle_active must, when transitioning a luciel_instance from
active=True to active=False, cascade-deactivate every memory_items
row scoped to that instance under the same tenant. Pre-29.y the
route flipped the active flag without any cascade, leaving D-1
memory rows behind that a future occupant of the same agent slot
would inherit -- the exact PIPEDA P5 hole G-2 documents.

These tests do NOT spin up the FastAPI app. They are AST/regex
checks against the source so they run in any environment, including
ones without a live DB.

Invariants pinned here:

  T1. ``app.services.admin_service.AdminService`` is imported in
      app/api/v1/admin_forensics.py. Without the import the cascade
      call below cannot resolve; this test fails fast with a clearer
      message than ``NameError`` at request time.

  T2. The toggle route function body contains a call to
      ``AdminService(...).bulk_soft_deactivate_memory_items_for_luciel_instance(...)``.
      The exact method name is the canonical operational cascade
      already used by the operational DELETE route at
      app/api/v1/admin.py:911 -- pinning the name keeps both
      mutate-active surfaces on the same cascade path.

  T3. The cascade call passes ``autocommit=False``. autocommit=True
      would commit the cascade UPDATE separately from the toggle
      route's own audit row + active-flag flip, breaking the
      audit-row-before-mutation atomicity contract documented in
      tests/api/test_admin_forensics_step29c.py test 17.

  T4. The cascade call appears at a line number BETWEEN the existing
      audit_repo.record(...) call and the ``inst.active = ...``
      assignment. This preserves the C.5 ordering invariant
      (record_lineno < mutation_lineno) AND ensures the cascade
      itself runs before the active-flag flip -- so a cascade
      failure aborts the flip, not the other way around.

  T5. The cascade is gated on ``requested_active is False`` (or an
      equivalent shape that only fires on a True->False transition).
      A cascade that fired on every toggle including False->True
      would corrupt P11 F10's restore leg, where the harness
      reactivates the instance after the F10 assertion.
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
    raise AssertionError(f"function {name!r} not found in module")


# ---------------------------------------------------------------------
# T1: AdminService imported.
# ---------------------------------------------------------------------

def test_admin_service_imported_in_admin_forensics() -> None:
    src = _read("app/api/v1/admin_forensics.py")
    assert (
        "from app.services.admin_service import AdminService" in src
    ), (
        "G-2: app/api/v1/admin_forensics.py must import "
        "AdminService so the toggle route can call "
        "bulk_soft_deactivate_memory_items_for_luciel_instance "
        "before flipping luciel_instances.active. Without this "
        "import the Cluster 2 fix cannot resolve."
    )


# ---------------------------------------------------------------------
# T2: cascade call present in toggle route.
# ---------------------------------------------------------------------

def test_toggle_route_calls_bulk_soft_deactivate_cascade() -> None:
    tree = _parse("app/api/v1/admin_forensics.py")
    func = _find_function(tree, "toggle_luciel_instance_active_step29c")
    seen = False
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (
                node.func.attr
                == "bulk_soft_deactivate_memory_items_for_luciel_instance"
            ):
                seen = True
                break
    assert seen, (
        "G-2: toggle_luciel_instance_active_step29c must call "
        "AdminService(db).bulk_soft_deactivate_memory_items_for_luciel_instance(...) "
        "before flipping inst.active when deactivating. Without "
        "the cascade, memory_items rows survive luciel_instance "
        "deactivation and a future occupant of the agent slot "
        "inherits them (PIPEDA P5 hole, findings_phase1g.md G-2)."
    )


# ---------------------------------------------------------------------
# T3: cascade call uses autocommit=False.
# ---------------------------------------------------------------------

def test_cascade_call_passes_autocommit_false() -> None:
    tree = _parse("app/api/v1/admin_forensics.py")
    func = _find_function(tree, "toggle_luciel_instance_active_step29c")
    found = None
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (
                node.func.attr
                == "bulk_soft_deactivate_memory_items_for_luciel_instance"
            ):
                found = node
                break
    assert found is not None, "cascade call missing (covered by T2 too)"
    autocommit_kw = None
    for kw in found.keywords:
        if kw.arg == "autocommit":
            autocommit_kw = kw
            break
    assert autocommit_kw is not None, (
        "G-2: cascade call must pass autocommit=False explicitly. "
        "The default (True) would commit the cascade UPDATE in a "
        "separate transaction from the toggle route's audit row + "
        "active-flag flip, breaking the single-commit atomicity "
        "the C.5 audit-before-mutate contract relies on."
    )
    val = autocommit_kw.value
    assert isinstance(val, ast.Constant) and val.value is False, (
        f"G-2: cascade call passes autocommit={ast.dump(val)} but "
        f"must pass autocommit=False. autocommit=True would commit "
        f"the cascade UPDATE separately and the active-flag flip "
        f"would no longer be atomic with the cascade."
    )


# ---------------------------------------------------------------------
# T4: cascade call appears between audit .record() and inst.active = ...
# ---------------------------------------------------------------------

def test_cascade_between_audit_record_and_active_mutation() -> None:
    tree = _parse("app/api/v1/admin_forensics.py")
    func = _find_function(tree, "toggle_luciel_instance_active_step29c")

    record_lineno: int | None = None
    cascade_lineno: int | None = None
    mutation_lineno: int | None = None

    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "record":
                # First .record() call in the function is the audit write.
                if record_lineno is None or node.lineno < record_lineno:
                    record_lineno = node.lineno
            if (
                node.func.attr
                == "bulk_soft_deactivate_memory_items_for_luciel_instance"
            ):
                if cascade_lineno is None or node.lineno < cascade_lineno:
                    cascade_lineno = node.lineno
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (
                    isinstance(tgt, ast.Attribute)
                    and tgt.attr == "active"
                    and isinstance(tgt.value, ast.Name)
                ):
                    if mutation_lineno is None or node.lineno < mutation_lineno:
                        mutation_lineno = node.lineno

    assert record_lineno is not None, "audit .record() call missing"
    assert cascade_lineno is not None, "cascade call missing (covered by T2)"
    assert mutation_lineno is not None, "inst.active = ... assignment missing"
    assert record_lineno < cascade_lineno, (
        f"G-2: audit .record() must precede the cascade call so "
        f"audit-row-before-mutation still holds (the cascade "
        f"itself is a mutation). Got record at line "
        f"{record_lineno}, cascade at line {cascade_lineno}."
    )
    assert cascade_lineno < mutation_lineno, (
        f"G-2: cascade must precede the active-flag flip so the "
        f"memory rows are deactivated BEFORE the parent instance "
        f"is marked inactive. Reverse order would briefly leave "
        f"memory rows orphaned to an active=False parent (and on "
        f"transaction failure could leave them orphaned "
        f"permanently). Got cascade at line {cascade_lineno}, "
        f"flip at line {mutation_lineno}."
    )


# ---------------------------------------------------------------------
# T5: cascade only fires on True -> False transition.
#
# We do this with a regex-on-source check. The cascade must be
# guarded by a condition that mentions both ``requested_active``
# and ``False`` (or ``previous_active`` and ``True``) so it cannot
# fire on a False -> True restore (P11 F10 teardown).
# ---------------------------------------------------------------------

def test_cascade_gated_on_real_deactivation_only() -> None:
    src = _read("app/api/v1/admin_forensics.py")
    # Find the cascade call site and look at the ~15 preceding
    # lines for an ``if ... requested_active ... False ...`` shape.
    lines = src.splitlines()
    cascade_line = None
    for i, line in enumerate(lines):
        if "bulk_soft_deactivate_memory_items_for_luciel_instance" in line:
            cascade_line = i
            break
    assert cascade_line is not None, "cascade call line not located"
    window = "\n".join(lines[max(0, cascade_line - 15):cascade_line + 1])
    # Accept either:
    #   if requested_active is False and previous_active is True:
    #   if requested_active == False ...
    #   if not requested_active and previous_active:
    pattern = re.compile(
        r"if\s+("
        r"requested_active\s+is\s+False"
        r"|requested_active\s*==\s*False"
        r"|not\s+requested_active"
        r")"
    )
    assert pattern.search(window), (
        "G-2: the memory cascade must be gated so it only fires "
        "on a real True->False deactivation transition. Without "
        "this guard, P11 F10's restore leg (False->True) would "
        "trip the cascade and corrupt the harness teardown. "
        f"Window inspected:\n{window}"
    )


# ---------------------------------------------------------------------
# T6: module imports cleanly (catches accidental syntax breakage).
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "module",
    [
        "app.api.v1.admin_forensics",
        "app.services.admin_service",
    ],
)
def test_cluster2_modules_import(module: str) -> None:
    import importlib

    importlib.import_module(module)
