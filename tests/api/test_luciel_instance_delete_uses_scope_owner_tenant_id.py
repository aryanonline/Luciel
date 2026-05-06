"""
Regression test for Step 28 C10 (P3-Q):
    DELETE /api/v1/admin/luciel-instances/{pk} must read
    instance.scope_owner_tenant_id, NOT instance.tenant_id.

CONTRACT GUARDED:
    1. The deactivate_luciel_instance route in app/api/v1/admin.py must
       reference instance.scope_owner_tenant_id when constructing the
       memory cascade call. The LucielInstance ORM model exposes the
       tenant column under that name (see app/models/luciel_instance.py)
       because LucielInstance is a multi-scope resource (tenant /
       domain / agent) and uses scope_owner_* prefixes consistently.
    2. The route must NOT reference instance.tenant_id anywhere -- that
       attribute does not exist on LucielInstance and any access raises
       AttributeError, which propagates as HTTP 500.

THE BUG THIS GUARDS AGAINST:
    P3-Q (Discovered 2026-05-04, root-caused 2026-05-06): every
    DELETE /api/v1/admin/luciel-instances/{pk} returned HTTP 500 in
    production because line 904 of admin.py read instance.tenant_id.
    The error chain was:
        deactivate_luciel_instance reads instance.tenant_id
        -> AttributeError: 'LucielInstance' object has no attribute
                           'tenant_id'
        -> 500 Internal Server Error
        -> bulk_soft_deactivate_memory_items_for_luciel_instance
           was NEVER called for any DELETE in prod
    Pillar 10 zero-residue still passed because the verify teardown
    PATCHes the tenant active=false at the end, which fires the
    tenant-level cascade that catches everything the failed DELETEs
    left behind. The teardown anomaly was tracked in three consecutive
    verify runs (luciel 42 / 69 / 73) before root-cause was confirmed
    via CloudWatch traceback on 2026-05-06.

WHY AST INSTEAD OF DB:
    Following the convention of
    tests/memory/test_extractor_save_fail_observability.py and
    tests/middleware/test_actor_user_id_binding.py: source-level AST
    proof catches the regression first and fastest, runs without any
    app dependencies installed (no sqlalchemy / pgvector / etc), and
    survives in CI sandboxes that cannot stand up Postgres or the
    full FastAPI app.

    A live HTTP test would catch the regression too but would require
    the entire admin auth stack + DB fixtures, and a regression here
    fails earlier (at static analysis) than at any runtime layer.

RUN:
    python -m pytest tests/api/test_luciel_instance_delete_uses_scope_owner_tenant_id.py -v
    OR (no pytest needed):
    python tests/api/test_luciel_instance_delete_uses_scope_owner_tenant_id.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

# Allow running via
#   python tests/api/test_luciel_instance_delete_uses_scope_owner_tenant_id.py
# from any cwd by inserting the project root on sys.path before imports.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


_ADMIN_API_PATH = _PROJECT_ROOT / "app" / "api" / "v1" / "admin.py"
_LUCIEL_INSTANCE_MODEL_PATH = _PROJECT_ROOT / "app" / "models" / "luciel_instance.py"
_ROUTE_FUNCTION_NAME = "deactivate_luciel_instance"


def _parse(path: Path) -> ast.Module:
    """Parse the source file as an AST module."""
    if not path.exists():
        raise FileNotFoundError(f"Expected source file not found: {path}")
    src = path.read_text(encoding="utf-8")
    return ast.parse(src, filename=str(path))


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    """Return the first FunctionDef with matching name, or None."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


# ---------------------------------------------------------------------------
# Test 1: LucielInstance ORM model exposes scope_owner_tenant_id.
# This anchors the contract: if someone renames the column on the model,
# this test catches it before the route test fires for the wrong reason.
# ---------------------------------------------------------------------------

def test_luciel_instance_model_has_scope_owner_tenant_id_attribute() -> None:
    tree = _parse(_LUCIEL_INSTANCE_MODEL_PATH)
    found = False
    for node in ast.walk(tree):
        # mapped_column / Column assignments via AnnAssign
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "scope_owner_tenant_id":
                found = True
                break
    assert found, (
        "LucielInstance ORM model must expose scope_owner_tenant_id as "
        "an annotated mapped column; this is the canonical tenant "
        "attribute for the multi-scope LucielInstance resource."
    )


# ---------------------------------------------------------------------------
# Test 2: LucielInstance ORM model does NOT expose tenant_id directly.
# If a future maintainer adds a tenant_id column to LucielInstance, this
# regression test will fail loud and force re-evaluation -- mixing
# tenant_id and scope_owner_tenant_id on the same model is exactly the
# kind of ambiguity that produced P3-Q.
# ---------------------------------------------------------------------------

def test_luciel_instance_model_does_not_expose_tenant_id_directly() -> None:
    tree = _parse(_LUCIEL_INSTANCE_MODEL_PATH)
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assert node.target.id != "tenant_id", (
                "LucielInstance must not expose a top-level tenant_id "
                "attribute. The canonical name is scope_owner_tenant_id "
                "(plus scope_owner_domain_id and scope_owner_agent_id) "
                "to disambiguate from tenant-scoped resources that have "
                "a flat tenant_id column. See P3-Q root-cause analysis."
            )


# ---------------------------------------------------------------------------
# Test 3: deactivate_luciel_instance route uses scope_owner_tenant_id.
# This is the actual P3-Q regression guard. Pre-fix, line 904 of admin.py
# read instance.tenant_id; the AttributeError propagated as HTTP 500.
# ---------------------------------------------------------------------------

def test_deactivate_luciel_instance_route_uses_scope_owner_tenant_id() -> None:
    tree = _parse(_ADMIN_API_PATH)
    func = _find_function(tree, _ROUTE_FUNCTION_NAME)
    assert func is not None, (
        f"Could not find route function {_ROUTE_FUNCTION_NAME} in "
        f"app/api/v1/admin.py. Did the route get renamed or moved?"
    )

    # Walk the function body for any Attribute access whose value is a
    # Name 'instance'. Confirm at least one such access reads
    # 'scope_owner_tenant_id'.
    found_scope_owner_tenant_id = False
    for node in ast.walk(func):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "instance" and node.attr == "scope_owner_tenant_id":
                found_scope_owner_tenant_id = True
                break

    assert found_scope_owner_tenant_id, (
        "deactivate_luciel_instance must read instance.scope_owner_tenant_id "
        "(not instance.tenant_id) when constructing the memory cascade "
        "call. P3-Q root-cause: pre-fix used instance.tenant_id which "
        "raised AttributeError and caused every DELETE to return 500."
    )


# ---------------------------------------------------------------------------
# Test 4: deactivate_luciel_instance route MUST NOT read instance.tenant_id.
# Belt-and-braces companion to Test 3: future maintainers might add a
# scope_owner_tenant_id reference while leaving the bad tenant_id one in
# place. This test catches that.
# ---------------------------------------------------------------------------

def test_deactivate_luciel_instance_route_does_not_read_instance_tenant_id() -> None:
    tree = _parse(_ADMIN_API_PATH)
    func = _find_function(tree, _ROUTE_FUNCTION_NAME)
    assert func is not None

    bad_access = False
    for node in ast.walk(func):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "instance" and node.attr == "tenant_id":
                bad_access = True
                break

    assert not bad_access, (
        "deactivate_luciel_instance must NOT read instance.tenant_id -- "
        "that attribute does not exist on the LucielInstance ORM model "
        "and any access raises AttributeError -> HTTP 500. Use "
        "instance.scope_owner_tenant_id."
    )


# ---------------------------------------------------------------------------
# Manual runner so the suite works without pytest installed.
# ---------------------------------------------------------------------------

def _run_all() -> int:
    tests = [
        test_luciel_instance_model_has_scope_owner_tenant_id_attribute,
        test_luciel_instance_model_does_not_expose_tenant_id_directly,
        test_deactivate_luciel_instance_route_uses_scope_owner_tenant_id,
        test_deactivate_luciel_instance_route_does_not_read_instance_tenant_id,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  [FAIL] {t.__name__}")
            print(f"         {exc}")
        except Exception as exc:
            failed += 1
            print(f"  [ERROR] {t.__name__}")
            print(f"          {type(exc).__name__}: {exc}")
    print()
    if failed:
        print(f"  RESULT: {len(tests) - failed}/{len(tests)} passed, {failed} failed")
        return 1
    print(f"  RESULT: {len(tests)}/{len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
