"""
Contract test for Step 29 Commits C.1, C.2, C.3, C.4, and C.5:
    GET /api/v1/admin/forensics/api_keys_step29c
    GET /api/v1/admin/forensics/memory_items_step29c
    GET /api/v1/admin/forensics/admin_audit_logs_step29c
    GET /api/v1/admin/forensics/luciel_instances_step29c/{instance_id}
    GET /api/v1/admin/forensics/messages_step29c               (C.3)
    GET /api/v1/admin/forensics/users_step29c/{user_id}        (C.4)
    POST /api/v1/admin/forensics/luciel_instances_step29c
         /{instance_id}/toggle_active                          (C.5)

C.2 EXTENDS:
    memory_items_step29c gains two query params (actor_user_id,
    agent_id) and one projection field (actor_user_id) so P12 can
    perform identity-stability assertions over HTTP. Tests 8 and 9
    pin those additions in place.

C.3 EXTENDS:
    1. Adds a fifth route, list_messages_forensic_step29c, with a
       strict projection that EXCLUDES `content` (chat content is the
       most sensitive field after memory content). Tests 10 and 11
       pin the route's existence and content-exclusion contract.
    2. memory_items_step29c gains two more query params (message_id,
       content_contains) so P13 can poll for the spoof memory row by
       message_id and probe for cross-tenant content leak via a
       server-side substring filter (the projection still excludes
       content; caller learns only "row matches?", never the text).
       Test 12 pins those filters.
    3. admin_audit_logs_step29c gains one query param
       (actor_key_prefix) so P13 can scope the SPOOF_REJECT audit-row
       poll to the exact tenant-A platform_admin key without scanning
       the global audit log. Test 13 pins that filter.

C.4 EXTENDS:
    Adds a sixth route, get_user_forensic_step29c, with a strict
    UserForensic projection that EXCLUDES `email` and `display_name`
    (both PII -- the forensic surface has no business returning
    them). Tests 14 and 15 pin the route's existence and the
    PII-exclusion contract; test 16 pins that `synthetic` IS in
    the projection (the boolean Option-B-onboarding-auto-created
    flag is metadata, not PII, and is useful for forensic
    cross-correlation). The two ApiKey reads in P14 (A1/A2) and
    the two MemoryItem reads (A5/A7) reuse C.1+C.2 endpoints with
    no new params; only A6 needs the new endpoint.

C.5 EXTENDS:
    Adds a seventh route, toggle_luciel_instance_active_step29c,
    which is a POST (the first and only mutation in the C-series).
    It backs the P11 F10 ORM-write migration: deactivate (setup)
    and restore (teardown) of the instance-liveness Gate-4
    assertion. C.5 ships TWO EXTRA AST tests beyond the C.x
    pattern (per the bisect-surface compensation rule locked at
    87e4068, which suspended the verify-after-every-commit
    doctrine for C.4 -> D):
      - Test 18 (audit-row-before-mutation invariant): the route's
        AST shows the AdminAuditRepository.record() call appears at
        a lower line number than the `inst.active = ...` assignment.
        A future maintainer who refactors this route into a
        "mutate then audit" shape silently breaks the compliance
        contract -- this test fails loud the moment that happens.
      - Test 19 (ALLOWED_ACTIONS membership invariant):
        ACTION_LUCIEL_INSTANCE_FORENSIC_TOGGLE is in
        ALLOWED_ACTIONS in app/models/admin_audit_log.py. If a
        maintainer adds the constant but forgets to extend the
        tuple, AdminAuditRepository.record() raises ValueError
        on every C.5 POST -- the route 500s in production. This
        test catches the half-applied refactor at AST time.

C.6 EXTENDS:
    Adds three AST tests covering the verification-harness consolidation
    that landed in C.6 (no new admin_forensics routes, no new projections;
    pure cleanup commit on the harness side). The tests live in this file
    rather than a new tests/verification/ file because the doctrine flip
    at 87e4068 explicitly anticipated each of C.4/C.5/C.6/D shipping AST
    tests in this same file (D is the only one that gets a separate
    harness-equivalent file).
      - Test 19 (infra-probes module exists with both helpers):
        app/verification/_infra_probes.py defines _broker_reachable AND
        _worker_reachable as module-level FunctionDefs. P11 and P13
        both import them by name, so a deleted module or renamed
        helper breaks both pillars at import time. Surface that
        regression at the source level rather than at verify time.
      - Test 20 (no re-inlined helpers in P11/P13): the consolidation
        invariant -- neither pillar may define _broker_reachable or
        _worker_reachable as a module-level FunctionDef. The original
        B.1 mode-gate honesty bug came from the inline copies drifting
        apart; this test prevents the recurrence at AST time.
      - Test 21 (forensics_get allowlist contract): the wrapper added
        in C.6 invokes call() with expect=(200, 404) literally. A
        maintainer who drops 404 defeats the wrapper (callers branch
        on status 404); a maintainer who broadens the allowlist masks
        forensic-plane failures. Pin the exact two-element tuple.

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

# ---- Step 29 Commit C.6 paths (verification harness consolidation) -------
# C.6 pulled `_broker_reachable` / `_worker_reachable` out of P11 + P13 into
# `_infra_probes.py`, and added `forensics_get` to `http_client.py`. The C.6
# tests below pin those moves at the source level so a future refactor that
# re-inlines the helpers, deletes the shared module, or broadens the
# `forensics_get` status-code allowlist surfaces here rather than at the
# next verify run.
_INFRA_PROBES_PATH = (
    _PROJECT_ROOT / "app" / "verification" / "_infra_probes.py"
)
_HTTP_CLIENT_PATH = (
    _PROJECT_ROOT / "app" / "verification" / "http_client.py"
)
_PILLAR_11_PATH = (
    _PROJECT_ROOT / "app" / "verification" / "tests" / "pillar_11_async_memory.py"
)
_PILLAR_13_PATH = (
    _PROJECT_ROOT / "app" / "verification" / "tests" / "pillar_13_cross_tenant_identity.py"
)

# ---- Step 29 Commit D paths (pillar registry as single source of truth) --
# D extracted the pre-teardown pillar list from app/verification/__main__.py
# into a new app/verification/registry.py module so the CLI entry point and
# the pytest harness in tests/verification/test_pillars.py reference the
# SAME ordered list. The three D tests below pin: registry exposes the two
# expected functions; __main__.py no longer carries the literal; the pytest
# harness imports from registry, not from __main__.
_REGISTRY_PATH = _PROJECT_ROOT / "app" / "verification" / "registry.py"
_VERIFICATION_MAIN_PATH = _PROJECT_ROOT / "app" / "verification" / "__main__.py"
_TEST_PILLARS_PATH = _PROJECT_ROOT / "tests" / "verification" / "test_pillars.py"

_ROUTE_FUNCTION_NAMES = (
    "get_api_key_forensic_step29c",
    "list_memory_items_forensic_step29c",
    "list_admin_audit_logs_forensic_step29c",
    "get_luciel_instance_forensic_step29c",
    "list_messages_forensic_step29c",
    "get_user_forensic_step29c",
    "toggle_luciel_instance_active_step29c",  # C.5
)

_RESPONSE_MODEL_NAMES = (
    "ApiKeyForensic",
    "MemoryItemForensic",
    "MemoryItemsForensic",
    "AdminAuditLogForensic",
    "AdminAuditLogsForensic",
    "LucielInstanceForensic",
    "MessageForensic",
    "MessagesForensic",
    "UserForensic",
    "LucielInstanceToggleRequest",  # C.5 request body schema
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
# Test 10 (C.3): MessageForensic projection MUST NOT include content.
# This is the parallel of test 4 for the new chat-message projection.
# A future maintainer who adds `content` to the response model leaks
# every chat turn ever stored to anyone holding a platform_admin key.
# Chat content is the single most sensitive field on `messages` after
# attachments; this test fails loud the moment the projection grows.
# ---------------------------------------------------------------------------

def test_message_forensic_projection_excludes_content() -> None:
    tree = _parse(_ADMIN_FORENSICS_PATH)
    cls = _find_classdef(tree, "MessageForensic")
    assert cls is not None, "MessageForensic class missing"
    fields = _annotated_field_names(cls)
    assert "content" not in fields, (
        "MessageForensic must NOT expose chat-message content. P13 "
        "only reads metadata (id, session_id, role, trace_id, "
        "created_at) for spoof-rejection and cross-tenant leak "
        "probes; surfacing chat text over the platform_admin HTTP "
        "boundary defeats the strict-projection rationale."
    )
    # Positive guard: the metadata fields P13 actually needs are present.
    for required in ("id", "session_id", "role"):
        assert required in fields, (
            f"MessageForensic must expose {required!r} (Step 29 "
            f"Commit C.3). P13 reads it to correlate the spoofed "
            f"message turn against the spoof-rejection audit row."
        )


# ---------------------------------------------------------------------------
# Test 11 (C.3): list_messages_forensic_step29c exists and is registered
# under that canonical name. The function-existence assertion is already
# covered by test 1 via _ROUTE_FUNCTION_NAMES; this test pins the
# additional contract that the route accepts session_id as a required
# query parameter (P13 always scopes its message reads by session).
# ---------------------------------------------------------------------------

def test_list_messages_route_accepts_session_id() -> None:
    tree = _parse(_ADMIN_FORENSICS_PATH)
    func = _find_function(tree, "list_messages_forensic_step29c")
    assert func is not None, (
        "list_messages_forensic_step29c missing (Step 29 Commit C.3)"
    )
    arg_names = {a.arg for a in func.args.args}
    arg_names.update({a.arg for a in func.args.kwonlyargs})
    assert "session_id" in arg_names, (
        "list_messages_forensic_step29c must accept session_id as a "
        "query parameter. P13 always scopes its message reads by "
        "session; an unscoped messages endpoint would let a "
        "platform_admin caller dump the global messages table."
    )


# ---------------------------------------------------------------------------
# Test 12 (C.3): list_memory_items_forensic_step29c accepts the two
# additional filter parameters added in C.3 (message_id and
# content_contains). Without message_id, P13's six-attempt poll for
# the spoof memory row would have to scan the global memory_items
# table client-side; without content_contains, P13's cross-tenant
# content-leak probe loses its server-side WHERE clause and falls
# back to fetching every row in tenant A's memory.
# ---------------------------------------------------------------------------

def test_list_memory_items_route_accepts_c3_filters() -> None:
    tree = _parse(_ADMIN_FORENSICS_PATH)
    func = _find_function(tree, "list_memory_items_forensic_step29c")
    assert func is not None, (
        "list_memory_items_forensic_step29c missing"
    )
    arg_names = {a.arg for a in func.args.args}
    arg_names.update({a.arg for a in func.args.kwonlyargs})
    for required in ("message_id", "content_contains"):
        assert required in arg_names, (
            f"list_memory_items_forensic_step29c must accept "
            f"{required!r} as a query parameter (added in Step 29 "
            f"Commit C.3 for P13 cross-tenant identity reads). "
            f"Removing it forces P13 to scan global memory "
            f"client-side, breaking the strict-projection contract."
        )


# ---------------------------------------------------------------------------
# Test 13 (C.3): list_admin_audit_logs_forensic_step29c accepts the
# actor_key_prefix filter added in C.3. P13's SPOOF_REJECT audit-row
# poll must scope to the exact tenant-A platform_admin key prefix;
# without this filter the poll matches any spoof-rejection audit row
# in the global audit log, which racy CI runs would falsely satisfy.
# ---------------------------------------------------------------------------

def test_list_admin_audit_logs_route_accepts_actor_key_prefix() -> None:
    tree = _parse(_ADMIN_FORENSICS_PATH)
    func = _find_function(tree, "list_admin_audit_logs_forensic_step29c")
    assert func is not None, (
        "list_admin_audit_logs_forensic_step29c missing"
    )
    arg_names = {a.arg for a in func.args.args}
    arg_names.update({a.arg for a in func.args.kwonlyargs})
    assert "actor_key_prefix" in arg_names, (
        "list_admin_audit_logs_forensic_step29c must accept "
        "actor_key_prefix as a query parameter (added in Step 29 "
        "Commit C.3 for P13 spoof-rejection audit poll). Without "
        "this filter the poll cannot scope to a single tenant's "
        "platform_admin key and any racy SPOOF_REJECT audit row in "
        "the global log can falsely satisfy A2."
    )


# ---------------------------------------------------------------------------
# Test 14 (C.4): UserForensic projection MUST NOT include `email` or
# `display_name`. Both are PII; the forensic surface is the worst
# possible place for them to leak. P14 A6 only needs `active` (and
# transitively `id`) to assert User identity persistence across
# tenant departure, so the projection has no legitimate reason to
# carry PII.
# ---------------------------------------------------------------------------

def test_user_forensic_projection_excludes_pii() -> None:
    tree = _parse(_ADMIN_FORENSICS_PATH)
    cls = _find_classdef(tree, "UserForensic")
    assert cls is not None, "UserForensic class missing"
    fields = _annotated_field_names(cls)
    for forbidden in ("email", "display_name"):
        assert forbidden not in fields, (
            f"UserForensic must NOT expose {forbidden!r}. Both "
            f"`email` and `display_name` are PII; surfacing them "
            f"on a forensic endpoint defeats the strict-projection "
            f"rationale and creates a regulatory disclosure surface "
            f"that this module deliberately does not have."
        )
    # Positive guard: A6's actual assertion needs `active` (and `id`
    # for caller-side correlation).
    for required in ("id", "active"):
        assert required in fields, (
            f"UserForensic must expose {required!r} (Step 29 "
            f"Commit C.4). P14 A6 reads `active` to assert User "
            f"identity persistence across departure; `id` is the "
            f"caller-side correlation handle."
        )


# ---------------------------------------------------------------------------
# Test 15 (C.4): UserForensic projection DOES include `synthetic`.
# Inverse-shape contract -- `synthetic` is a non-PII boolean flag
# distinguishing Option-B-onboarding-auto-created users from real
# users. P14 does not currently read it but future forensic flows
# (PIPEDA access/erasure paths filter on it) need it surfaced. If
# a future maintainer mistakenly classifies it as "too sensitive"
# and drops it from the projection, this test catches that --
# `synthetic` is metadata, NOT PII (it carries no information
# about the underlying person).
# ---------------------------------------------------------------------------

def test_user_forensic_projection_includes_synthetic() -> None:
    tree = _parse(_ADMIN_FORENSICS_PATH)
    cls = _find_classdef(tree, "UserForensic")
    assert cls is not None, "UserForensic class missing"
    fields = _annotated_field_names(cls)
    assert "synthetic" in fields, (
        "UserForensic must include `synthetic` (Step 29 Commit "
        "C.4). It is a non-PII boolean distinguishing "
        "Option-B-onboarding-auto-created users from real users; "
        "PIPEDA access/erasure flows filter on it. Dropping it "
        "from the projection silently degrades any future "
        "forensic path that relies on the distinction."
    )


# ---------------------------------------------------------------------------
# Test 16 (C.4): get_user_forensic_step29c exists and accepts
# user_id as a path parameter. The function-existence assertion is
# already covered by test 1 via _ROUTE_FUNCTION_NAMES; this test
# pins the path-parameter signature so a future refactor cannot
# accidentally turn it into an unscoped list endpoint.
# ---------------------------------------------------------------------------

def test_get_user_route_accepts_user_id_path_param() -> None:
    tree = _parse(_ADMIN_FORENSICS_PATH)
    func = _find_function(tree, "get_user_forensic_step29c")
    assert func is not None, (
        "get_user_forensic_step29c missing (Step 29 Commit C.4)"
    )
    arg_names = {a.arg for a in func.args.args}
    arg_names.update({a.arg for a in func.args.kwonlyargs})
    assert "user_id" in arg_names, (
        "get_user_forensic_step29c must accept user_id as a "
        "parameter. The route is pinned to "
        "/users_step29c/{user_id} -- a path parameter, not a "
        "list endpoint -- because there is no legitimate "
        "forensic use case for enumerating all users; that "
        "would be a PII-disclosure surface even with the "
        "strict projection."
    )


# ---------------------------------------------------------------------------
# Test 17 (C.5, EXTRA #1): audit-row-BEFORE-mutation invariant.
#
# The C.5 POST `toggle_luciel_instance_active_step29c` writes an
# admin_audit_log row BEFORE mutating `luciel_instances.active`. If
# a future maintainer flips the order (mutate then audit), an
# audit-write failure would leave a mutation persisted with no
# trace -- silently breaking the compliance contract that audit
# rows must accompany every state change. This test pins the
# ordering at AST time: the AdminAuditRepository(...).record(...)
# call must appear at a lower line number than the
# `inst.active = ...` assignment in the function body.
#
# This is one of the TWO EXTRA AST tests C.5 ships beyond the C.x
# pattern, per the bisect-surface compensation rule locked at
# 87e4068 (verify-after-every-commit doctrine suspended for
# C.4 -> D).
# ---------------------------------------------------------------------------

def test_toggle_route_audits_before_mutating() -> None:
    tree = _parse(_ADMIN_FORENSICS_PATH)
    func = _find_function(tree, "toggle_luciel_instance_active_step29c")
    assert func is not None, (
        "toggle_luciel_instance_active_step29c missing (Step 29 "
        "Commit C.5)"
    )

    record_lineno: int | None = None
    mutation_lineno: int | None = None

    for node in ast.walk(func):
        # AdminAuditRepository(...).record(...) -- a Call whose func
        # is an Attribute with attr == "record". We do not pin the
        # exact receiver chain (could be `audit_repo.record(...)` or
        # `AdminAuditRepository(db).record(...)`); attribute name is
        # the contract.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "record":
                if record_lineno is None or node.lineno < record_lineno:
                    record_lineno = node.lineno
        # `inst.active = ...` Assign with target Attribute.attr == "active"
        # on a Name receiver. We pin Name receiver (not arbitrary
        # expression) because the conventional pattern is
        # `inst = db.get(...); inst.active = <new>`.
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (
                    isinstance(tgt, ast.Attribute)
                    and tgt.attr == "active"
                    and isinstance(tgt.value, ast.Name)
                ):
                    if mutation_lineno is None or node.lineno < mutation_lineno:
                        mutation_lineno = node.lineno

    assert record_lineno is not None, (
        "toggle_luciel_instance_active_step29c must call "
        ".record(...) (AdminAuditRepository's audit-row writer). "
        "Without an audit row, every state mutation through this "
        "route is invisible to compliance -- the precise outcome "
        "the C.5 design rejects."
    )
    assert mutation_lineno is not None, (
        "toggle_luciel_instance_active_step29c must contain an "
        "`<name>.active = ...` assignment to actually flip the "
        "flag. If the route returns the row unmodified, P11 F10's "
        "deactivated-instance Gate-4 assertion can never fire."
    )
    assert record_lineno < mutation_lineno, (
        f"toggle_luciel_instance_active_step29c records audit at "
        f"line {record_lineno} but mutates active at line "
        f"{mutation_lineno}. The audit row MUST be written before "
        f"the mutation so an audit-insert failure aborts the "
        f"mutation (atomic in a single commit). Reversing this "
        f"order means a mutation can persist with no audit trail "
        f"if the audit insert later raises -- the compliance "
        f"contract this route exists to satisfy."
    )


# ---------------------------------------------------------------------------
# Test 18 (C.5, EXTRA #2): ALLOWED_ACTIONS membership invariant.
#
# AdminAuditRepository.record() validates `action` against
# ALLOWED_ACTIONS in app/models/admin_audit_log.py and raises
# ValueError on unknown actions. If a maintainer defines
# ACTION_LUCIEL_INSTANCE_FORENSIC_TOGGLE but forgets to extend
# the ALLOWED_ACTIONS tuple, every C.5 POST raises ValueError at
# the audit-write step -- which means the route 500s in
# production while local sandboxes that never hit the audit path
# stay green. This AST test catches the half-applied refactor.
#
# This is the second of the TWO EXTRA AST tests C.5 ships
# beyond the C.x pattern, per the bisect-surface compensation
# rule locked at 87e4068.
# ---------------------------------------------------------------------------

def test_action_luciel_instance_forensic_toggle_in_allowed_actions() -> None:
    audit_log_path = _PROJECT_ROOT / "app" / "models" / "admin_audit_log.py"
    tree = _parse(audit_log_path)

    # 1. The constant exists at module-top-level as a string assignment.
    constant_value: str | None = None
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if (
                    isinstance(tgt, ast.Name)
                    and tgt.id == "ACTION_LUCIEL_INSTANCE_FORENSIC_TOGGLE"
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    constant_value = stmt.value.value
    assert constant_value is not None, (
        "ACTION_LUCIEL_INSTANCE_FORENSIC_TOGGLE must be defined as a "
        "module-level string constant in app/models/admin_audit_log.py "
        "(Step 29 Commit C.5). The C.5 POST route imports it and passes "
        "it as the action= argument to AdminAuditRepository.record(); "
        "removing the constant 500s every forensic-toggle POST."
    )

    # 2. ALLOWED_ACTIONS is a tuple at module-top-level whose elements
    #    include a Name node with id == "ACTION_LUCIEL_INSTANCE_FORENSIC_TOGGLE".
    allowed_actions_elts: list[ast.expr] | None = None
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if (
                    isinstance(tgt, ast.Name)
                    and tgt.id == "ALLOWED_ACTIONS"
                    and isinstance(stmt.value, ast.Tuple)
                ):
                    allowed_actions_elts = list(stmt.value.elts)
    assert allowed_actions_elts is not None, (
        "ALLOWED_ACTIONS must be defined as a module-level tuple in "
        "app/models/admin_audit_log.py."
    )

    member_names = {
        elt.id
        for elt in allowed_actions_elts
        if isinstance(elt, ast.Name)
    }
    assert "ACTION_LUCIEL_INSTANCE_FORENSIC_TOGGLE" in member_names, (
        "ACTION_LUCIEL_INSTANCE_FORENSIC_TOGGLE is defined but NOT "
        "in ALLOWED_ACTIONS. AdminAuditRepository.record() validates "
        "action against ALLOWED_ACTIONS and raises ValueError on "
        "unknown actions, so the C.5 POST would 500 in production "
        "while local sandboxes that never hit the audit path stay "
        "green. Add the constant to the tuple in "
        "app/models/admin_audit_log.py."
    )


# ---------------------------------------------------------------------------
# Step 29 Commit C.6: verification-harness consolidation contracts.
# ---------------------------------------------------------------------------

def test_infra_probes_module_defines_broker_and_worker_reachable() -> None:
    """C.6 invariant: shared infra-probe module exists with both helpers.

    `_broker_reachable` / `_worker_reachable` were originally inlined in
    Pillar 11 and (since B.1) duplicated verbatim in Pillar 13. C.6
    consolidated them into `app/verification/_infra_probes.py`. If a
    future refactor deletes the module or renames either function, both
    P11 and P13 fail to import (and therefore fail to load) -- this test
    surfaces that source-level regression before any verify run.
    """
    if not _INFRA_PROBES_PATH.exists():
        raise AssertionError(
            f"C.6: {_INFRA_PROBES_PATH} does not exist. The shared infra "
            "probe module was deleted; restore it (or relocate the helpers "
            "and update P11/P13/the test below to match)."
        )
    tree = ast.parse(_INFRA_PROBES_PATH.read_text())
    fn_names = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    missing = {"_broker_reachable", "_worker_reachable"} - fn_names
    if missing:
        raise AssertionError(
            f"C.6: {_INFRA_PROBES_PATH.name} missing helpers {sorted(missing)}. "
            f"Found module-level FunctionDefs: {sorted(fn_names)}. Both "
            "_broker_reachable and _worker_reachable must be defined here "
            "(P11 and P13 import them by name)."
        )


def test_pillars_11_and_13_dont_redefine_infra_probes() -> None:
    """C.6 invariant: P11 and P13 must not re-inline the infra probes.

    The whole point of C.6 was to eliminate the duplicate inline copies
    that caused the B.1 mode-gate honesty bug (P13's _broker_reachable
    drifted out of sync with P11's). If a future maintainer re-adds an
    inline definition in either pillar -- even with the same body --
    the duplication risk returns. This test pins the cleanup at the
    source level: zero module-level FunctionDefs named _broker_reachable
    or _worker_reachable in either pillar file.
    """
    forbidden_names = {"_broker_reachable", "_worker_reachable"}
    for path in (_PILLAR_11_PATH, _PILLAR_13_PATH):
        tree = ast.parse(path.read_text())
        offenders = sorted(
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in forbidden_names
        )
        if offenders:
            raise AssertionError(
                f"C.6: {path.name} re-defines {offenders} as module-level "
                "functions. These helpers must be imported from "
                "app.verification._infra_probes (the C.6 consolidation), "
                "not inlined. Re-inlining recreates the B.1 drift risk."
            )


def test_forensics_get_admits_200_and_404_only() -> None:
    """C.6 invariant: `forensics_get` allowlists exactly (200, 404).

    The wrapper exists to name the GET-and-expect-(200,404) pattern. A
    maintainer who drops 404 from the allowlist defeats the wrapper's
    purpose (callers branch on r.status_code == 404 for the
    teardown-race / never-created case); a maintainer who broadens the
    allowlist (e.g. adds 500) silently masks forensic-plane failures
    that should surface as hard AssertionErrors. Either drift breaks
    the contract that callers rely on. This test parses the wrapper
    body, finds the inner call(...) invocation, and asserts the
    expect= kwarg is a Tuple of exactly the two literal ints {200, 404}.
    """
    tree = ast.parse(_HTTP_CLIENT_PATH.read_text())
    fn = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "forensics_get"
        ),
        None,
    )
    if fn is None:
        raise AssertionError(
            f"C.6: {_HTTP_CLIENT_PATH.name} does not define forensics_get. "
            "Restore the wrapper (introduced in C.6) or relocate it and "
            "update this test to match."
        )

    # Walk the function body and find the first Call whose func is a Name
    # 'call' (the inner call() invocation that forensics_get is a thin
    # wrapper around). The wrapper has exactly one such call.
    inner_call = None
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "call"
        ):
            inner_call = node
            break
    if inner_call is None:
        raise AssertionError(
            "C.6: forensics_get does not invoke call(). The wrapper must "
            "delegate to call() to preserve auth + assertion semantics."
        )

    expect_kw = next(
        (kw for kw in inner_call.keywords if kw.arg == "expect"),
        None,
    )
    if expect_kw is None:
        raise AssertionError(
            "C.6: forensics_get's inner call() invocation has no expect= "
            "kwarg. The wrapper must pin expect=(200, 404) explicitly so "
            "the contract is visible at the source level."
        )
    if not isinstance(expect_kw.value, ast.Tuple):
        raise AssertionError(
            f"C.6: forensics_get's expect= kwarg must be a Tuple literal; "
            f"got {type(expect_kw.value).__name__}. A non-tuple value "
            "could be a variable that drifts at runtime."
        )

    constants: list[int] = []
    for elt in expect_kw.value.elts:
        if not isinstance(elt, ast.Constant) or not isinstance(elt.value, int):
            raise AssertionError(
                "C.6: forensics_get's expect= tuple must contain only "
                f"literal int Constants; got element of type "
                f"{type(elt).__name__}."
            )
        constants.append(elt.value)

    if sorted(constants) != [200, 404]:
        raise AssertionError(
            f"C.6: forensics_get expect= allowlist must be exactly "
            f"(200, 404); got {tuple(constants)}. Dropping 404 defeats "
            "the wrapper; broadening the allowlist (e.g. +500) masks "
            "forensic-plane failures."
        )


# ---------------------------------------------------------------------------
# Step 29 Commit D: pillar-registry single source of truth
# ---------------------------------------------------------------------------
#
# These three tests pin the D refactor at the source level. They run without
# importing the registry (which would require DATABASE_URL) -- pure AST + raw
# source-substring inspection. A future contributor who reverts D by:
#   - deleting registry.py,
#   - copying the pillar list back into __main__.py,
#   - or having the pytest harness import pillars from __main__.py,
# will see one of these three tests fail and the cause will be obvious from
# the assertion message.


def test_registry_module_defines_pre_teardown_and_integrity_functions() -> None:
    """app/verification/registry.py defines both registry functions at module scope.

    Both must be top-level FunctionDefs (not nested, not class methods)
    because callers do `from app.verification.registry import
    pre_teardown_pillars, teardown_integrity_pillar`. A regression that
    moves either symbol into a class, renames it, or deletes it breaks
    the import in __main__.py and in tests/verification/test_pillars.py;
    this AST-level pin catches it before runtime.
    """
    tree = _parse(_REGISTRY_PATH)
    top_level_func_names = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    assert "pre_teardown_pillars" in top_level_func_names, (
        f"app/verification/registry.py missing top-level FunctionDef "
        f"`pre_teardown_pillars`; found top-level functions: "
        f"{sorted(top_level_func_names)!r}"
    )
    assert "teardown_integrity_pillar" in top_level_func_names, (
        f"app/verification/registry.py missing top-level FunctionDef "
        f"`teardown_integrity_pillar`; found top-level functions: "
        f"{sorted(top_level_func_names)!r}"
    )


def test_main_no_longer_carries_pre_teardown_pillars_literal() -> None:
    """app/verification/__main__.py no longer contains the inline pillar list.

    Pre-D, __main__.py opened with `from ... import PILLAR as P1` (x23)
    followed by `PRE_TEARDOWN_PILLARS = [P1, P2, ...]`. D moved that
    entirely into registry.py. If a future contributor copies the list
    back into __main__.py (perhaps as a "local override") this test
    fails -- preserving the single-source-of-truth invariant.

    Substring check is intentional: the only legitimate reason for
    `PRE_TEARDOWN_PILLARS = [` to appear in __main__.py is the
    re-introduction of the literal. Comments referencing the symbol
    are allowed (and expected -- the file's commentary explains the
    extraction).
    """
    src = _VERIFICATION_MAIN_PATH.read_text(encoding="utf-8")
    assert "PRE_TEARDOWN_PILLARS = [" not in src, (
        "app/verification/__main__.py contains `PRE_TEARDOWN_PILLARS = [` "
        "-- the inline pillar literal that D extracted into "
        "app/verification/registry.py. Restore the registry-only "
        "source-of-truth pattern."
    )


def test_test_pillars_imports_from_registry_not_main() -> None:
    """tests/verification/test_pillars.py imports pillars from the registry.

    Pins the architectural intent of D: the pytest harness MUST share
    the registry with __main__.py. If the harness is rewritten to import
    pillars directly from app.verification.__main__ or to inline its own
    P1..P23 import list, the two entry points can drift -- which is
    exactly the failure mode the registry was created to prevent.

    The harness wraps its registry import in a module-level try/except
    so collection succeeds in CI without DATABASE_URL; that try/except
    still contains the `from app.verification.registry import` line, so
    an AST walk over the whole module body picks it up.
    """
    tree = _parse(_TEST_PILLARS_PATH)
    has_registry_import = False
    has_main_pillar_import = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "app.verification.registry":
                has_registry_import = True
            if node.module == "app.verification.__main__":
                # Importing anything from __main__ would be a smell; if
                # someone reaches in there for the pillar list, we want
                # a loud failure here.
                has_main_pillar_import = True
    assert has_registry_import, (
        "tests/verification/test_pillars.py is missing "
        "`from app.verification.registry import ...`. The harness must "
        "share the pillar registry with __main__.py."
    )
    assert not has_main_pillar_import, (
        "tests/verification/test_pillars.py imports from "
        "app.verification.__main__. The harness must NOT reach into "
        "the CLI entry-point module for the pillar list -- that would "
        "reintroduce the drift risk D was built to prevent. Import "
        "from app.verification.registry instead."
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
        test_message_forensic_projection_excludes_content,
        test_list_messages_route_accepts_session_id,
        test_list_memory_items_route_accepts_c3_filters,
        test_list_admin_audit_logs_route_accepts_actor_key_prefix,
        test_user_forensic_projection_excludes_pii,
        test_user_forensic_projection_includes_synthetic,
        test_get_user_route_accepts_user_id_path_param,
        test_toggle_route_audits_before_mutating,
        test_action_luciel_instance_forensic_toggle_in_allowed_actions,
        test_infra_probes_module_defines_broker_and_worker_reachable,
        test_pillars_11_and_13_dont_redefine_infra_probes,
        test_forensics_get_admits_200_and_404_only,
        test_registry_module_defines_pre_teardown_and_integrity_functions,
        test_main_no_longer_carries_pre_teardown_pillars_literal,
        test_test_pillars_imports_from_registry_not_main,
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
