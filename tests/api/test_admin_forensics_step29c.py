"""
Contract test for Step 29 Commits C.1 and C.2:
    GET /api/v1/admin/forensics/api_keys_step29c
    GET /api/v1/admin/forensics/memory_items_step29c
    GET /api/v1/admin/forensics/admin_audit_logs_step29c
    GET /api/v1/admin/forensics/luciel_instances_step29c/{instance_id}

C.2 EXTENDS:
    memory_items_step29c gains two query params (actor_user_id,
    agent_id) and one projection field (actor_user_id) so P12 can
    perform identity-stability assertions over HTTP. Tests 8 and 9
    pin those additions in place.

CONTRACT GUARDED:
    1. The four route functions exist in app/api/v1/admin_forensics.py
       under the canonical names get_api_key_forensic_step29c,
       list_memory_items_forensic_step29c,
       list_admin_audit_logs_forensic_step29c, and
       get_luciel_instance_forensic_step29c. A future maintainer who
       renames any of these breaks all P11 forensic reads (and after
       C.2-C.4, P12/P13/P14 forensic reads as well).

    2. Every route function calls _require_platform_admin_step29c
       before issuing any DB read. Skipping the gate would expose
       arbitrary api_keys / memory_items / audit_logs rows to any
       authenticated caller with a regular tenant key.

    3. The ApiKeyForensic response model does NOT include `key_hash`.
       This is the single highest-leverage projection invariant in the
       module: api_keys.key_hash is the bcrypt'd secret. If a future
       maintainer adds it to the projection (e.g. via the temptation
       to "match the ORM model" or by switching to model_config
       extra='allow'), the bcrypt'd secret leaks to anyone holding a
       platform_admin key. This test fails loud the moment that
       happens.

    4. The MemoryItemForensic response model does NOT include
       `content`. The harness only reads metadata for idempotency and
       leak probes; surfacing memory text content over HTTP would
       defeat the whole reason this module is read-only-projection
       rather than "return the ORM row".

    5. The AdminAuditLogForensic response model DOES include
       `after_json`. P11 F5 / F6 / F8 hygiene assertions require
       reading after_json to verify production already guarantees no
       PII there. If a maintainer drops after_json from the
       projection, F5 / F6 / F8 silently degrade because the harness
       reads `row.get("after_json") or {}` and would see {} forever.
       This test is the inverse of #3 and #4.

    6. The router include happens in app/api/router.py with the
       admin_forensics module imported and admin_forensics.router
       passed to api_router.include_router. If the include is missed,
       all four endpoints 404 and every full-mode P11 sub-assertion
       fails.

WHY AST INSTEAD OF DB:
    Following the convention of
    tests/api/test_luciel_instance_delete_uses_scope_owner_tenant_id.py
    and tests/middleware/test_actor_user_id_binding.py: source-level
    AST proof catches the regression first and fastest, runs without
    any app dependencies installed (no sqlalchemy / pgvector / etc),
    and survives in CI sandboxes that cannot stand up Postgres or the
    full FastAPI app.

    Live HTTP coverage of these endpoints comes for free from Pillar
    11 in MODE=full (worker + broker reachable), which exercises all
    four endpoints end-to-end against a real backend with real
    platform_admin auth.

RUN:
    python -m pytest tests/api/test_admin_forensics_step29c.py -v
    OR (no pytest needed):
    python tests/api/test_admin_forensics_step29c.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

# Allow running via
#   python tests/api/test_admin_forensics_step29c.py
# from any cwd by inserting the project root on sys.path before imports.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


_ADMIN_FORENSICS_PATH = (
    _PROJECT_ROOT / "app" / "api" / "v1" / "admin_forensics.py"
)
_ROUTER_PATH = _PROJECT_ROOT / "app" / "api" / "router.py"

_ROUTE_FUNCTION_NAMES = (
    "get_api_key_forensic_step29c",
    "list_memory_items_forensic_step29c",
    "list_admin_audit_logs_forensic_step29c",
    "get_luciel_instance_forensic_step29c",
)

_RESPONSE_MODEL_NAMES = (
    "ApiKeyForensic",
    "MemoryItemForensic",
    "MemoryItemsForensic",
    "AdminAuditLogForensic",
    "AdminAuditLogsForensic",
    "LucielInstanceForensic",
)


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


def _find_classdef(tree: ast.Module, name: str) -> ast.ClassDef | None:
    """Return the first ClassDef with matching name, or None."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _annotated_field_names(cls: ast.ClassDef) -> set[str]:
    """Names declared via AnnAssign at the class top level (Pydantic fields)."""
    names: set[str] = set()
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            names.add(stmt.target.id)
    return names


# ---------------------------------------------------------------------------
# Test 1: All four route function definitions exist.
# ---------------------------------------------------------------------------

def test_admin_forensics_route_functions_exist() -> None:
    tree = _parse(_ADMIN_FORENSICS_PATH)
    for name in _ROUTE_FUNCTION_NAMES:
        assert _find_function(tree, name) is not None, (
            f"Expected route function {name!r} in admin_forensics.py. "
            f"All four Step 29 Commit C.1 forensic-read endpoints depend "
            f"on these canonical function names; renaming any of them "
            f"breaks every P11 (and later P12/P13/P14) forensic read."
        )


# ---------------------------------------------------------------------------
# Test 2: Every route calls _require_platform_admin_step29c before any
# DB access. We assert the helper-call appears textually before the
# first occurrence of `db.scalars` or `db.get` inside each function body.
# ---------------------------------------------------------------------------

def test_admin_forensics_routes_check_platform_admin_first() -> None:
    tree = _parse(_ADMIN_FORENSICS_PATH)
    helper = "_require_platform_admin_step29c"

    for name in _ROUTE_FUNCTION_NAMES:
        func = _find_function(tree, name)
        assert func is not None, f"missing function {name}"

        helper_lineno: int | None = None
        first_db_lineno: int | None = None

        for node in ast.walk(func):
            # Helper call: an Expr statement with a Call to a Name.
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == helper and helper_lineno is None:
                    helper_lineno = node.lineno
            # First DB read: db.scalars(...) or db.get(...)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "db":
                    if node.func.attr in ("scalars", "get"):
                        if first_db_lineno is None or node.lineno < first_db_lineno:
                            first_db_lineno = node.lineno

        assert helper_lineno is not None, (
            f"{name} must call {helper}(request) before issuing any DB "
            f"read; the platform_admin gate is the only access control "
            f"on this surface."
        )
        if first_db_lineno is not None:
            assert helper_lineno < first_db_lineno, (
                f"{name} calls DB at line {first_db_lineno} but only "
                f"checks platform_admin at line {helper_lineno}; the "
                f"gate must run first or unauthorized callers can race "
                f"the DB read."
            )


# ---------------------------------------------------------------------------
# Test 3: ApiKeyForensic projection MUST NOT include key_hash.
# ---------------------------------------------------------------------------

def test_api_key_forensic_projection_excludes_key_hash() -> None:
    tree = _parse(_ADMIN_FORENSICS_PATH)
    cls = _find_classdef(tree, "ApiKeyForensic")
    assert cls is not None, "ApiKeyForensic class missing"
    fields = _annotated_field_names(cls)
    assert "key_hash" not in fields, (
        "ApiKeyForensic must NOT expose key_hash. That field stores the "
        "bcrypt'd secret; including it in the response model would leak "
        "the hashed credential to any caller holding a platform_admin "
        "key. The 12-char key_prefix is the public correlation handle "
        "and is sufficient for forensics."
    )
    # Positive guard: the safe correlation handle is present.
    assert "key_prefix" in fields, (
        "ApiKeyForensic must expose key_prefix so the harness can "
        "correlate api_keys rows with audit-row actor_key_prefix values."
    )


# ---------------------------------------------------------------------------
# Test 4: MemoryItemForensic projection MUST NOT include content.
# ---------------------------------------------------------------------------

def test_memory_item_forensic_projection_excludes_content() -> None:
    tree = _parse(_ADMIN_FORENSICS_PATH)
    cls = _find_classdef(tree, "MemoryItemForensic")
    assert cls is not None, "MemoryItemForensic class missing"
    fields = _annotated_field_names(cls)
    assert "content" not in fields, (
        "MemoryItemForensic must NOT expose memory content. The harness "
        "only reads metadata (id, message_id, tenant_id) for idempotency "
        "and cross-tenant leak probes; surfacing content over the "
        "platform_admin HTTP boundary would defeat the strict-projection "
        "rationale of this module."
    )


# ---------------------------------------------------------------------------
# Test 5: AdminAuditLogForensic projection DOES include after_json.
# Inverse-shape contract: P11 F5 / F6 / F8 require after_json to assert
# content hygiene, actor_key_prefix linkage, and trace_id propagation.
# Dropping it from the projection silently degrades all three.
# ---------------------------------------------------------------------------

def test_admin_audit_log_forensic_projection_includes_after_json() -> None:
    tree = _parse(_ADMIN_FORENSICS_PATH)
    cls = _find_classdef(tree, "AdminAuditLogForensic")
    assert cls is not None, "AdminAuditLogForensic class missing"
    fields = _annotated_field_names(cls)
    assert "after_json" in fields, (
        "AdminAuditLogForensic MUST include after_json. P11 F5 reads it "
        "for content-hygiene assertions, F6 for actor_key_prefix "
        "linkage, and F8 for trace_id propagation. Production code "
        "already guarantees no PII in after_json (F5 verifies this "
        "every run)."
    )


# ---------------------------------------------------------------------------
# Test 6: All response models are defined (catches a typo or a
# half-applied refactor that drops one of the projections silently).
# ---------------------------------------------------------------------------

def test_admin_forensics_response_models_defined() -> None:
    tree = _parse(_ADMIN_FORENSICS_PATH)
    for name in _RESPONSE_MODEL_NAMES:
        assert _find_classdef(tree, name) is not None, (
            f"Response model {name!r} missing from admin_forensics.py. "
            f"All six are required: four singular projections + two "
            f"collection wrappers (MemoryItemsForensic, "
            f"AdminAuditLogsForensic)."
        )


# ---------------------------------------------------------------------------
# Test 7: Router wires admin_forensics. Both the import and the
# include_router call must be present in app/api/router.py.
# ---------------------------------------------------------------------------

def test_router_includes_admin_forensics() -> None:
    src = _ROUTER_PATH.read_text(encoding="utf-8")
    assert "admin_forensics" in src, (
        "app/api/router.py must import admin_forensics; without the "
        "import the four endpoints never get registered and every P11 "
        "full-mode sub-assertion 404s."
    )

    tree = _parse(_ROUTER_PATH)
    found_include = False
    for node in ast.walk(tree):
        # api_router.include_router(admin_forensics.router)
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "include_router":
            continue
        # First positional arg should be admin_forensics.router
        if not node.args:
            continue
        arg0 = node.args[0]
        if (isinstance(arg0, ast.Attribute)
                and isinstance(arg0.value, ast.Name)
                and arg0.value.id == "admin_forensics"
                and arg0.attr == "router"):
            found_include = True
            break

    assert found_include, (
        "app/api/router.py must call "
        "api_router.include_router(admin_forensics.router). Importing "
        "the module is not enough; FastAPI only registers routes that "
        "have been explicitly included."
    )


# ---------------------------------------------------------------------------
# Test 8 (C.2): MemoryItemForensic projection MUST include actor_user_id.
# P12 A1 / A3 / A4 / A5 assert identity continuity by comparing each
# memory row's actor_user_id against the platform User UUID held across
# the promotion. If the projection drops actor_user_id, every P12
# memory-row assertion silently degrades to "None != user_id" and the
# pillar fails for the wrong reason. This is the inverse-shape contract
# for C.2 (mirrors test 5 for after_json on AdminAuditLogForensic).
#
# Rationale for surfacing actor_user_id over HTTP: it is a platform
# User UUID FK to users.id (Step 24.5b made it NOT NULL on every
# memory row), not user-supplied content. content / extracted_text
# remain excluded; actor_user_id is metadata, not message body.
# ---------------------------------------------------------------------------

def test_memory_item_forensic_projection_includes_actor_user_id() -> None:
    tree = _parse(_ADMIN_FORENSICS_PATH)
    cls = _find_classdef(tree, "MemoryItemForensic")
    assert cls is not None, "MemoryItemForensic class missing"
    fields = _annotated_field_names(cls)
    assert "actor_user_id" in fields, (
        "MemoryItemForensic MUST include actor_user_id (Step 29 "
        "Commit C.2). P12 A1 / A3 / A4 / A5 assert identity "
        "continuity by comparing each memory row's actor_user_id to "
        "the platform User UUID held across promotion. Without this "
        "field every P12 memory assertion compares None against a "
        "UUID and the pillar fails for the wrong reason."
    )


# ---------------------------------------------------------------------------
# Test 9 (C.2): list_memory_items_forensic_step29c accepts the two
# filter parameters added in C.2 (actor_user_id and agent_id).
# Without these query params the four P12 callsites cannot scope their
# reads server-side, and A5 (cross-agent scope isolation) loses its
# WHERE-clause coverage entirely.
# ---------------------------------------------------------------------------

def test_list_memory_items_route_accepts_c2_filters() -> None:
    tree = _parse(_ADMIN_FORENSICS_PATH)
    func = _find_function(tree, "list_memory_items_forensic_step29c")
    assert func is not None, (
        "list_memory_items_forensic_step29c missing"
    )
    arg_names = {a.arg for a in func.args.args}
    arg_names.update({a.arg for a in func.args.kwonlyargs})
    for required in ("actor_user_id", "agent_id"):
        assert required in arg_names, (
            f"list_memory_items_forensic_step29c must accept "
            f"{required!r} as a query parameter (added in Step 29 "
            f"Commit C.2 for P12 identity-stability reads). Removing "
            f"it forces the harness to filter client-side, which "
            f"defeats A5's server-side scope-isolation contract."
        )


# ---------------------------------------------------------------------------
# Manual runner so the suite works without pytest installed.
# ---------------------------------------------------------------------------

def _run_all() -> int:
    tests = [
        test_admin_forensics_route_functions_exist,
        test_admin_forensics_routes_check_platform_admin_first,
        test_api_key_forensic_projection_excludes_key_hash,
        test_memory_item_forensic_projection_excludes_content,
        test_admin_audit_log_forensic_projection_includes_after_json,
        test_admin_forensics_response_models_defined,
        test_router_includes_admin_forensics,
        test_memory_item_forensic_projection_includes_actor_user_id,
        test_list_memory_items_route_accepts_c2_filters,
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
