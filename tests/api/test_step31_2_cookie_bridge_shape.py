"""Backend-free contract tests for Step 31.2 commit A -- session-cookie
auth middleware.

Step 31.2 commit A lands `SessionCookieAuthMiddleware` which bridges the
Step 30a magic-link session cookie into the same `request.state` shape
that `ApiKeyAuthMiddleware` populates for admin API keys. This file pins
the *shape* of that bridge so we catch unintentional contract drift
between the cookie path and the API-key path before runtime.

Coverage (AST + import only -- no Postgres, no FastAPI runtime, no
network):

  * Module + class -- the middleware class exists with the documented
    name and is importable.
  * Path filter -- COOKIE_AUTH_PATHS includes only /admin and /dashboard
    at v1 (chat, billing, health all excluded by absence).
  * Permission tuple -- COOKIE_PERMISSIONS matches the admin-key
    permission set ("admin","chat","sessions"), so cookied callers can
    do anything the tenant's admin key can do.
  * `request.state` fields set -- every field downstream code reads off
    state (tenant_id, permissions, key_prefix, actor_label,
    actor_user_id, auth_method, plus the widget-related fields the
    middleware sets to None) is populated by the cookie path.
  * Audit attribution -- `actor_label` is set to "cookie:<email>" so
    auditors can trace a cookied action to its user without an
    `actor_user_id` FK column.
  * Middleware mount order -- app/main.py mounts SessionCookieAuth
    AFTER ApiKey so the cookie middleware runs OUTSIDE the API-key
    middleware (Starlette: last-added is outermost).
  * Short-circuit handshake -- ApiKeyAuthMiddleware.dispatch checks
    `request.state.auth_method == "cookie"` and skips the
    Authorization-header gate.

End-to-end correctness (cookie -> /admin/luciel-instances POST ->
audit row landed -> /dashboard/tenant GET) is covered by the
companion live e2e harness at tests/e2e/step_31_2_live_e2e.py.
"""
from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest


# --------------------------------------------------------------------------- #
# Module + class
# --------------------------------------------------------------------------- #


def test_session_cookie_auth_module_importable() -> None:
    """The new middleware module is importable without side effects."""
    mod = importlib.import_module("app.middleware.session_cookie_auth")
    assert mod is not None


def test_session_cookie_auth_middleware_class_exists() -> None:
    """The middleware class is named SessionCookieAuthMiddleware and
    subclasses Starlette's BaseHTTPMiddleware."""
    from starlette.middleware.base import BaseHTTPMiddleware

    from app.middleware.session_cookie_auth import SessionCookieAuthMiddleware

    assert inspect.isclass(SessionCookieAuthMiddleware)
    assert issubclass(SessionCookieAuthMiddleware, BaseHTTPMiddleware)


def test_dispatch_is_async() -> None:
    """`dispatch` is an async coroutine function per Starlette contract."""
    from app.middleware.session_cookie_auth import SessionCookieAuthMiddleware

    assert inspect.iscoroutinefunction(SessionCookieAuthMiddleware.dispatch)


# --------------------------------------------------------------------------- #
# Path filter -- COOKIE_AUTH_PATHS
# --------------------------------------------------------------------------- #


def test_cookie_auth_paths_includes_admin_and_dashboard() -> None:
    """At v1 the cookie authenticates /api/v1/admin and /api/v1/dashboard
    only. Adding more paths is a deliberate scope expansion that should
    be reviewed at the same time as this test."""
    from app.middleware.session_cookie_auth import COOKIE_AUTH_PATHS

    assert set(COOKIE_AUTH_PATHS) == {"/api/v1/admin", "/api/v1/dashboard"}


def test_cookie_auth_paths_excludes_chat() -> None:
    """Widget chat MUST stay embed-key-only -- a logged-in customer's
    cookie cannot drive widget traffic outside the embed-key scope
    envelope. This test pins that exclusion explicitly."""
    from app.middleware.session_cookie_auth import COOKIE_AUTH_PATHS

    assert not any(p.startswith("/api/v1/chat") for p in COOKIE_AUTH_PATHS)


def test_cookie_auth_paths_excludes_billing() -> None:
    """Billing routes do their own per-route cookie validation in
    app/api/v1/billing.py; they MUST NOT be in COOKIE_AUTH_PATHS or we
    would double-resolve the cookie."""
    from app.middleware.session_cookie_auth import COOKIE_AUTH_PATHS

    assert not any(p.startswith("/api/v1/billing") for p in COOKIE_AUTH_PATHS)


# --------------------------------------------------------------------------- #
# Permission tuple -- COOKIE_PERMISSIONS
# --------------------------------------------------------------------------- #


def test_cookie_permissions_matches_admin_key_set() -> None:
    """A cookied caller is granted the same permission set the tenant's
    admin API key carries (chat + sessions + admin). Drift between
    these two would mean cookied users can't do what API-key callers
    can or vice versa."""
    from app.middleware.session_cookie_auth import COOKIE_PERMISSIONS

    assert set(COOKIE_PERMISSIONS) == {"admin", "chat", "sessions"}


def test_cookie_permissions_is_tuple() -> None:
    """COOKIE_PERMISSIONS is an immutable tuple -- runtime code that
    sets request.state.permissions converts to list, but the source of
    truth is server-set and immutable."""
    from app.middleware.session_cookie_auth import COOKIE_PERMISSIONS

    assert isinstance(COOKIE_PERMISSIONS, tuple)


# --------------------------------------------------------------------------- #
# request.state fields set (AST scan)
# --------------------------------------------------------------------------- #


REQUIRED_STATE_FIELDS = {
    # Auth-vector fields ApiKeyAuthMiddleware sets:
    "tenant_id",
    "domain_id",
    "agent_id",
    "api_key_id",
    "permissions",
    "key_prefix",
    "actor_label",
    "luciel_instance_id",
    "actor_user_id",
    # Widget-related fields (Step 30b commit c) -- must be set even
    # though cookied requests never reach the widget endpoint, because
    # downstream code may defensively read these:
    "key_kind",
    "allowed_origins",
    "rate_limit_per_minute",
    "widget_config",
    # Step 31.2 marker:
    "auth_method",
}


def _state_attrs_set_in_module(module_path: Path) -> set[str]:
    """Walk the AST of `module_path` and return the set of attribute
    names assigned to `request.state.<attr>` anywhere in the module."""
    tree = ast.parse(module_path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        # Look for `request.state.X = ...`
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Attribute)
                    and isinstance(target.value.value, ast.Name)
                    and target.value.value.id == "request"
                    and target.value.attr == "state"
                ):
                    found.add(target.attr)
    return found


def test_cookie_middleware_sets_all_required_state_fields() -> None:
    """The cookie middleware populates every field on request.state
    that ApiKeyAuthMiddleware populates, plus the auth_method marker.
    If a field is missing, downstream code (AuditContext, ScopePolicy,
    chat dependencies) will silently see None and behave wrong."""
    path = (
        Path(__file__).parent.parent.parent
        / "app"
        / "middleware"
        / "session_cookie_auth.py"
    )
    assert path.exists(), f"middleware file missing: {path}"
    fields = _state_attrs_set_in_module(path)
    missing = REQUIRED_STATE_FIELDS - fields
    assert not missing, (
        f"SessionCookieAuthMiddleware fails to set request.state fields: "
        f"{sorted(missing)}. Downstream code reads these off state and "
        f"would silently see None."
    )


# --------------------------------------------------------------------------- #
# Audit attribution
# --------------------------------------------------------------------------- #


def test_cookie_actor_label_uses_email_prefix() -> None:
    """Cookied admin actions write audit rows with
    actor_label='cookie:<email>'. AST-grep for the format string so we
    pin the provenance prefix."""
    path = (
        Path(__file__).parent.parent.parent
        / "app"
        / "middleware"
        / "session_cookie_auth.py"
    )
    src = path.read_text()
    assert 'f"cookie:{user.email}"' in src, (
        "actor_label format string drifted from f'cookie:{user.email}' -- "
        "auditors rely on this prefix to filter cookied actions."
    )


def test_cookie_key_prefix_is_none() -> None:
    """key_prefix MUST be None for cookied actions -- the
    admin_audit_logs.actor_key_prefix column is nullable specifically
    so cookied rows can land without a fake key prefix."""
    path = (
        Path(__file__).parent.parent.parent
        / "app"
        / "middleware"
        / "session_cookie_auth.py"
    )
    src = path.read_text()
    # The assignment "request.state.key_prefix = key_prefix" plus the
    # earlier `key_prefix: str | None = None` line. We assert on the
    # explicit None typing so refactors don't silently set a stub.
    assert "key_prefix: str | None = None" in src


# --------------------------------------------------------------------------- #
# Middleware mount order in app/main.py
# --------------------------------------------------------------------------- #


def test_main_mounts_cookie_middleware_after_api_key() -> None:
    """app/main.py imports both middlewares and adds SessionCookieAuth
    AFTER ApiKeyAuth. Starlette executes last-added outermost, so this
    order means the cookie middleware runs FIRST on the way in, which
    is what makes the short-circuit work."""
    main_src = (
        Path(__file__).parent.parent.parent / "app" / "main.py"
    ).read_text()

    api_key_idx = main_src.index("app.add_middleware(ApiKeyAuthMiddleware)")
    cookie_idx = main_src.index("app.add_middleware(SessionCookieAuthMiddleware)")
    assert api_key_idx < cookie_idx, (
        "Cookie middleware must be added AFTER ApiKeyAuthMiddleware so "
        "Starlette runs it outermost (= first on the way in)."
    )


def test_main_imports_session_cookie_middleware() -> None:
    """The import line is present so the middleware actually mounts."""
    main_src = (
        Path(__file__).parent.parent.parent / "app" / "main.py"
    ).read_text()
    assert (
        "from app.middleware.session_cookie_auth import SessionCookieAuthMiddleware"
        in main_src
    )


# --------------------------------------------------------------------------- #
# Short-circuit handshake in ApiKeyAuthMiddleware
# --------------------------------------------------------------------------- #


def test_api_key_middleware_short_circuits_on_cookie_auth() -> None:
    """ApiKeyAuthMiddleware.dispatch checks
    `request.state.auth_method == 'cookie'` and returns early, skipping
    the Authorization-header gate. Without this check, cookied requests
    would still be rejected by the api-key middleware's Bearer-header
    requirement."""
    auth_src = (
        Path(__file__).parent.parent.parent
        / "app"
        / "middleware"
        / "auth.py"
    ).read_text()
    assert 'auth_method", None) == "cookie"' in auth_src, (
        "ApiKeyAuthMiddleware no longer short-circuits on the cookie "
        "auth_method tag -- cookied requests will be rejected by the "
        "Bearer header check."
    )


# --------------------------------------------------------------------------- #
# Settings the middleware reads exist
# --------------------------------------------------------------------------- #


def test_settings_session_cookie_name_present() -> None:
    """The middleware reads settings.session_cookie_name; that field
    is declared on app.core.config.Settings (Step 30a)."""
    from app.core.config import Settings

    assert "session_cookie_name" in Settings.model_fields


@pytest.mark.parametrize(
    "import_target",
    [
        "app.services.magic_link_service.validate_session_token",
        "app.services.magic_link_service.MagicLinkError",
        "app.services.billing_service.BillingService",
        "app.models.user.User",
    ],
)
def test_middleware_imports_resolve(import_target: str) -> None:
    """Every symbol the cookie middleware depends on resolves."""
    mod_path, _, attr = import_target.rpartition(".")
    mod = importlib.import_module(mod_path)
    assert hasattr(mod, attr), f"{import_target} missing"
