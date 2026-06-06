"""Arc 11 Closeout PR-A — instance lifecycle route contract tests.

Protects the four-route lifecycle surface mandated by Customer Journey
§4.5 Phase 8 (Pause / Resume / Delete / Restore) and Architecture
§3.6.1 (30-day grace window measured from soft_deleted_at).

Same AST + text assertion convention as
tests/api/test_arc10_close_account_route.py — protects the route
shape and audit-action wiring without standing up a full TestClient
or live DB session. The behavioural integration tests
(state transitions, audit-row emission, grace-window math) live in
the repository-level test file ``test_instance_lifecycle_repo.py``.
"""
from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_ROUTES_PATH = REPO_ROOT / "app" / "api" / "v1" / "admin" / "__init__.py"
SERVICE_PATH = REPO_ROOT / "app" / "services" / "instance_service.py"
REPO_PATH = REPO_ROOT / "app" / "repositories" / "instance_repository.py"
SCHEMA_PATH = REPO_ROOT / "app" / "schemas" / "instance.py"
AUDIT_PATH = REPO_ROOT / "app" / "models" / "admin_audit_log.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _parse(p: Path) -> ast.Module:
    return ast.parse(_read(p))


# ---------------------------------------------------------------------
# Route registration — four distinct affordances.
# ---------------------------------------------------------------------


def test_pause_route_is_registered():
    src = _read(ADMIN_ROUTES_PATH)
    assert '"/instances/{pk}/pause"' in src, (
        "POST /admin/instances/{pk}/pause must be registered."
    )


def test_resume_route_is_registered():
    src = _read(ADMIN_ROUTES_PATH)
    assert '"/instances/{pk}/resume"' in src, (
        "POST /admin/instances/{pk}/resume must be registered."
    )


def test_restore_route_is_registered():
    src = _read(ADMIN_ROUTES_PATH)
    assert '"/instances/{pk}/restore"' in src, (
        "POST /admin/instances/{pk}/restore must be registered."
    )


def test_delete_route_has_soft_delete_handler():
    """The DELETE handler must be the new soft-delete-with-grace handler,
    not the legacy ``deactivate_luciel_instance``.

    Customer Journey §4.5 Phase 8 makes Pause and Delete two distinct
    affordances; the DELETE verb must carry the destructive-intent
    semantics (stamps soft_deleted_at), and the operational quiet path
    must be the POST /pause route.
    """
    src = _read(ADMIN_ROUTES_PATH)
    assert "def delete_luciel_instance" in src, (
        "DELETE /admin/instances/{pk} must be wired to delete_luciel_instance."
    )
    assert "delete_instance_with_grace" in src, (
        "DELETE handler must call service.delete_instance_with_grace."
    )


# ---------------------------------------------------------------------
# Handler signatures — every lifecycle route enforces ScopePolicy.
# ---------------------------------------------------------------------


_LIFECYCLE_HANDLERS = (
    "pause_luciel_instance",
    "resume_luciel_instance",
    "delete_luciel_instance",
    "restore_luciel_instance",
)


def _function_node(name: str) -> ast.FunctionDef:
    tree = _parse(ADMIN_ROUTES_PATH)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found in admin.py")


def test_pause_handler_enforces_scope_policy():
    node = _function_node("pause_luciel_instance")
    src = ast.unparse(node)
    assert "ScopePolicy.enforce_luciel_instance_scope" in src, (
        "pause handler must call ScopePolicy.enforce_luciel_instance_scope"
    )


def test_resume_handler_enforces_scope_policy():
    node = _function_node("resume_luciel_instance")
    src = ast.unparse(node)
    assert "ScopePolicy.enforce_luciel_instance_scope" in src


def test_delete_handler_enforces_scope_policy():
    node = _function_node("delete_luciel_instance")
    src = ast.unparse(node)
    assert "ScopePolicy.enforce_luciel_instance_scope" in src


def test_restore_handler_enforces_scope_policy():
    node = _function_node("restore_luciel_instance")
    src = ast.unparse(node)
    assert "ScopePolicy.enforce_luciel_instance_scope" in src


# ---------------------------------------------------------------------
# Rate limiting — every lifecycle route is tier-aware.
# ---------------------------------------------------------------------


def test_pause_handler_is_rate_limited():
    node = _function_node("pause_luciel_instance")
    decorators = [ast.unparse(d) for d in node.decorator_list]
    assert any(
        "limiter.limit" in d and "get_tier_rate_limit_for_key" in d
        for d in decorators
    ), "pause route must be tier-rate-limited"


def test_resume_handler_is_rate_limited():
    node = _function_node("resume_luciel_instance")
    decorators = [ast.unparse(d) for d in node.decorator_list]
    assert any("limiter.limit" in d for d in decorators)


def test_delete_handler_is_rate_limited():
    node = _function_node("delete_luciel_instance")
    decorators = [ast.unparse(d) for d in node.decorator_list]
    assert any("limiter.limit" in d for d in decorators)


def test_restore_handler_is_rate_limited():
    node = _function_node("restore_luciel_instance")
    decorators = [ast.unparse(d) for d in node.decorator_list]
    assert any("limiter.limit" in d for d in decorators)


# ---------------------------------------------------------------------
# Audit context threading — every lifecycle route writes an audit row.
# ---------------------------------------------------------------------


def test_pause_handler_takes_audit_ctx_dependency():
    src = _read(ADMIN_ROUTES_PATH)
    assert (
        "def pause_luciel_instance" in src
        and "audit_ctx: Annotated[AuditContext, Depends(get_audit_context)]" in src
    ), "pause handler must thread AuditContext via Depends(get_audit_context)"


def test_resume_handler_takes_audit_ctx_dependency():
    node = _function_node("resume_luciel_instance")
    arg_names = [a.arg for a in node.args.args]
    assert "audit_ctx" in arg_names


def test_delete_handler_takes_audit_ctx_dependency():
    node = _function_node("delete_luciel_instance")
    arg_names = [a.arg for a in node.args.args]
    assert "audit_ctx" in arg_names


def test_restore_handler_takes_audit_ctx_dependency():
    node = _function_node("restore_luciel_instance")
    arg_names = [a.arg for a in node.args.args]
    assert "audit_ctx" in arg_names


# ---------------------------------------------------------------------
# Status-code semantics — 409 conflict, 410 gone, 404 not found.
# ---------------------------------------------------------------------


def test_pause_handler_maps_lifecycle_conflict_to_409():
    src = ast.unparse(_function_node("pause_luciel_instance"))
    assert "InstanceLifecycleConflictError" in src, (
        "pause handler must catch InstanceLifecycleConflictError"
    )
    assert "status_code=409" in src, (
        "pause handler must surface lifecycle conflict as HTTP 409"
    )


def test_resume_handler_maps_lifecycle_conflict_to_409():
    src = ast.unparse(_function_node("resume_luciel_instance"))
    assert "InstanceLifecycleConflictError" in src
    assert "status_code=409" in src


def test_restore_handler_maps_grace_expired_to_410():
    src = ast.unparse(_function_node("restore_luciel_instance"))
    assert "InstanceRestoreGraceExpiredError" in src, (
        "restore handler must catch InstanceRestoreGraceExpiredError"
    )
    assert "status_code=410" in src, (
        "Customer Journey §4.5 Phase 8 + Architecture §3.6.1: "
        "restore past the 30-day window must surface as HTTP 410 Gone."
    )


def test_restore_handler_surfaces_new_embed_key():
    """Vision §6.4 Reactivation — restore must re-mint embed keys; the
    new key is surfaced on the response under ``new_embed_key`` for
    a one-time read by the admin."""
    src = ast.unparse(_function_node("restore_luciel_instance"))
    assert "new_embed_key" in src, (
        "restore handler must surface the re-minted embed key on the response."
    )


# ---------------------------------------------------------------------
# Schema — InstanceRead must carry instance_status + soft_deleted_at.
# ---------------------------------------------------------------------


def test_instance_read_schema_carries_instance_status():
    src = _read(SCHEMA_PATH)
    assert "instance_status" in src, (
        "InstanceRead must include instance_status."
    )


def test_instance_read_schema_carries_soft_deleted_at():
    src = _read(SCHEMA_PATH)
    assert "soft_deleted_at" in src


def test_instance_read_schema_carries_new_embed_key():
    src = _read(SCHEMA_PATH)
    assert "new_embed_key" in src, (
        "InstanceRead must carry new_embed_key for the restore response."
    )


# ---------------------------------------------------------------------
# Service layer — Pause / Resume / Delete / Restore methods exist.
# ---------------------------------------------------------------------


_SERVICE_METHODS = (
    "pause_instance",
    "resume_instance",
    "delete_instance_with_grace",
    "restore_instance",
)


def test_instance_service_has_pause_resume_delete_restore():
    tree = _parse(SERVICE_PATH)
    methods = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in _SERVICE_METHODS:
        assert name in methods, (
            f"InstanceService must expose {name} per Arc 11 Closeout PR-A spec."
        )


# ---------------------------------------------------------------------
# Repository layer — pause/resume/delete/restore by PK.
# ---------------------------------------------------------------------


_REPO_METHODS = (
    "pause_by_pk",
    "resume_by_pk",
    "delete_by_pk",
    "restore_by_pk",
)


def test_instance_repository_has_lifecycle_methods():
    tree = _parse(REPO_PATH)
    methods = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
    }
    for name in _REPO_METHODS:
        assert name in methods, (
            f"InstanceRepository must expose {name} per Arc 11 Closeout PR-A spec."
        )


def test_deactivate_by_pk_is_deprecated_alias():
    """Spec: deactivate_by_pk survives as a deprecated alias for
    pause_by_pk. New code must call pause_by_pk; existing internal
    callsites stay compiling through Arc 11."""
    src = _read(REPO_PATH)
    assert "DeprecationWarning" in src, (
        "deactivate_by_pk must emit DeprecationWarning per spec."
    )


# ---------------------------------------------------------------------
# Audit constants — five new lifecycle verbs.
# ---------------------------------------------------------------------


def test_audit_actions_contain_lifecycle_verbs():
    src = _read(AUDIT_PATH)
    for verb in (
        "ACTION_INSTANCE_PAUSED",
        "ACTION_INSTANCE_RESUMED",
        "ACTION_INSTANCE_DELETED",
        "ACTION_INSTANCE_RESTORED",
        "ACTION_INSTANCE_HARD_PURGED",
    ):
        assert verb in src, (
            f"admin_audit_log.py must define {verb} per Arc 11 Closeout PR-A spec."
        )


def test_lifecycle_verbs_in_allowed_actions_tuple():
    """The five new verbs must be wired into ALLOWED_ACTIONS so
    AdminAuditRepository.record() accepts them. Arc 10 Gap 6 closed a
    similar membership omission for ACTION_AUDIT_LOG_TIER_ARCHIVED;
    we belt-and-suspenders here."""
    src = _read(AUDIT_PATH)
    # Find the ALLOWED_ACTIONS tuple block and verify each constant
    # appears within it. The block ends at the next top-level "\n)\n"
    # (every entry is comma-suffixed inside the tuple, so the first
    # right-paren appearing alone on a line is the tuple terminator).
    start = src.index("ALLOWED_ACTIONS = (")
    end = src.index("\n)", start)
    allowed_block = src[start:end]
    for verb in (
        "ACTION_INSTANCE_PAUSED",
        "ACTION_INSTANCE_RESUMED",
        "ACTION_INSTANCE_DELETED",
        "ACTION_INSTANCE_RESTORED",
        "ACTION_INSTANCE_HARD_PURGED",
    ):
        assert verb in allowed_block, (
            f"{verb} must be a member of ALLOWED_ACTIONS."
        )


# ---------------------------------------------------------------------
# Memory-items cascade — Pause and Delete both run the cascade.
# ---------------------------------------------------------------------


def test_pause_handler_runs_memory_cascade():
    """Pause must run the memory_items soft-deactivate cascade so the
    widget surface goes fully quiet; otherwise memory writes keep
    landing while the chat surface is silent (a half-state the founder
    explicitly rejected)."""
    src = ast.unparse(_function_node("pause_luciel_instance"))
    assert "_lifecycle_cascade_memory_items" in src


def test_delete_handler_runs_memory_cascade():
    src = ast.unparse(_function_node("delete_luciel_instance"))
    assert "_lifecycle_cascade_memory_items" in src
