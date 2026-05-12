"""Backend-free contract tests for Step 31 sub-branch 3.

Sub-branch 3 lands `app/api/v1/dashboard.py`, the HTTP surface for the
three hierarchical dashboard reads. Each handler wraps a corresponding
`DashboardService` method (sub-branch 2) through the `ScopePolicy` chain
and converts the frozen dataclass result into a JSON envelope via
`dataclasses.asdict`.

The defense-in-depth contract this sub-branch must NOT regress:

  1. The router mounts under `/dashboard`, and the auth middleware's
     `ADMIN_AUTH_PATHS` includes `/api/v1/dashboard`. Together those
     make embed keys impossible to admit: the middleware sees the
     prefix match, looks for `"admin"` in `permissions`, and the
     embed-key contract (`EMBED_REQUIRED_PERMISSIONS = {"chat"}`)
     guarantees that check fails. We pin BOTH halves of that contract
     here -- the prefix and the permission set -- by AST, not by
     spinning up the middleware, so the test runs in the sandbox.
  2. Each handler invokes the correct `ScopePolicy.enforce_*_scope`
     method. AST-grep enforces the call name + the call's position
     in the function body (it MUST happen before the service call,
     otherwise a buggy refactor could swap the order and call the
     service against an unauthorized scope).
  3. Each handler calls the correct `DashboardService` method.
  4. The router is registered in `app/api/router.py` so the prefix
     actually mounts.
  5. The `_to_envelope` helper produces a `dict` via
     `dataclasses.asdict`, not a custom serializer. This is the
     stable-envelope contract the §3.2.12 design-lock requires for
     Step 32 (UI) to render against.
  6. The router module performs NO database writes (no `.add()`,
     `.commit()`, `.flush()` calls). Pure read-side surface.

Coverage in this file is AST + small live imports of pure-Python
modules (the dashboard router itself, the schema permission set,
the service module). No FastAPI test client, no Postgres, no
slowapi mocking -- those belong to sub-branch 4's e2e harness.
"""
from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path


# Step 30d added DATABASE_URL gating at app.core.config import time. The
# router module pulls in app.middleware.rate_limit -> slowapi ->
# (transitively) app.core.config via the import chain, which then
# validates Settings.database_url is set. Sandbox tests use an in-memory
# SQLite URL; this never reaches the dashboard's query path because the
# router only LOADS in the live-import test, it never receives a request.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


REPO_ROOT = Path(__file__).resolve().parents[2]
ROUTER_PATH = REPO_ROOT / "app" / "api" / "v1" / "dashboard.py"
APP_ROUTER_PATH = REPO_ROOT / "app" / "api" / "router.py"
MIDDLEWARE_PATH = REPO_ROOT / "app" / "middleware" / "auth.py"
SCHEMAS_API_KEY_PATH = REPO_ROOT / "app" / "schemas" / "api_key.py"


# --------------------------------------------------------------------- #
# Module-level: parse once.
# --------------------------------------------------------------------- #


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


ROUTER_TREE = _parse(ROUTER_PATH)
APP_ROUTER_TREE = _parse(APP_ROUTER_PATH)
MIDDLEWARE_TREE = _parse(MIDDLEWARE_PATH)


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {
        n.name: n
        for n in tree.body
        if isinstance(n, ast.FunctionDef)
    }


ROUTER_FNS = _functions(ROUTER_TREE)


# --------------------------------------------------------------------- #
# 1. Module + handler surface
# --------------------------------------------------------------------- #


class TestModuleSurface:
    """The three handler functions, the DI helper, and the router
    object must exist with the documented names. Sub-branch 4 + Step 32
    import from these names; renaming them is a breaking contract change.
    """

    def test_handlers_present(self):
        for name in (
            "get_tenant_dashboard",
            "get_domain_dashboard",
            "get_agent_dashboard",
        ):
            assert name in ROUTER_FNS, (
                f"Handler `{name}` missing from app/api/v1/dashboard.py"
            )

    def test_di_helper_present(self):
        assert "get_dashboard_service" in ROUTER_FNS, (
            "`get_dashboard_service` DI helper missing"
        )

    def test_router_object_declared(self):
        # `router = APIRouter(prefix="/dashboard", tags=["dashboard"])`
        src = ROUTER_PATH.read_text()
        assert 'APIRouter(prefix="/dashboard"' in src, (
            "router must mount under /dashboard"
        )


# --------------------------------------------------------------------- #
# 2. Each endpoint's decorator + scope-policy call
# --------------------------------------------------------------------- #


def _decorator_paths(fn: ast.FunctionDef) -> list[str]:
    """Return the path arg of every @router.get(...) decorator on fn."""
    out: list[str] = []
    for deco in fn.decorator_list:
        if not isinstance(deco, ast.Call):
            continue
        if (
            isinstance(deco.func, ast.Attribute)
            and deco.func.attr == "get"
            and isinstance(deco.func.value, ast.Name)
            and deco.func.value.id == "router"
        ):
            if deco.args and isinstance(deco.args[0], ast.Constant):
                out.append(deco.args[0].value)
    return out


def _calls_in(fn: ast.FunctionDef) -> list[tuple[str, str]]:
    """Yield (object, attr) for every `OBJECT.attr(...)` call inside fn,
    in textual order. Used to pin call ordering (scope before service).
    """
    calls: list[tuple[str, str]] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name):
                calls.append((node.func.value.id, node.func.attr))
    return calls


class TestEndpointWiring:
    def test_tenant_handler_path_and_policy(self):
        fn = ROUTER_FNS["get_tenant_dashboard"]
        assert "/tenant" in _decorator_paths(fn)
        calls = _calls_in(fn)
        # ScopePolicy.enforce_tenant_scope must appear, and must appear
        # BEFORE the DashboardService call. We find the indices.
        names = [f"{o}.{a}" for o, a in calls]
        assert "ScopePolicy.enforce_tenant_scope" in names
        assert "service.get_tenant_dashboard" in names
        assert names.index("ScopePolicy.enforce_tenant_scope") < names.index(
            "service.get_tenant_dashboard"
        ), (
            "Scope enforcement MUST happen before the service call -- "
            "otherwise the service runs against an unauthorized scope"
        )

    def test_domain_handler_path_and_policy(self):
        fn = ROUTER_FNS["get_domain_dashboard"]
        assert "/domain/{domain_id}" in _decorator_paths(fn)
        names = [f"{o}.{a}" for o, a in _calls_in(fn)]
        assert "ScopePolicy.enforce_domain_scope" in names
        assert "service.get_domain_dashboard" in names
        assert names.index("ScopePolicy.enforce_domain_scope") < names.index(
            "service.get_domain_dashboard"
        )

    def test_agent_handler_path_and_policy(self):
        fn = ROUTER_FNS["get_agent_dashboard"]
        assert "/agent/{agent_id}" in _decorator_paths(fn)
        names = [f"{o}.{a}" for o, a in _calls_in(fn)]
        assert "ScopePolicy.enforce_agent_scope" in names
        assert "service.get_agent_dashboard" in names
        assert names.index("ScopePolicy.enforce_agent_scope") < names.index(
            "service.get_agent_dashboard"
        )


# --------------------------------------------------------------------- #
# 3. Rate-limit decorator
# --------------------------------------------------------------------- #


class TestRateLimit:
    """Every handler must carry the @limiter.limit(ADMIN_RATE_LIMIT, ...)
    decorator. Dashboards share the admin rate-limit envelope -- 30/min.
    A missing decorator silently uplifts the per-handler ceiling to no
    limit at all.
    """

    def _has_limiter(self, fn: ast.FunctionDef) -> bool:
        for deco in fn.decorator_list:
            if not isinstance(deco, ast.Call):
                continue
            f = deco.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr == "limit"
                and isinstance(f.value, ast.Name)
                and f.value.id == "limiter"
            ):
                # Must reference ADMIN_RATE_LIMIT, not a hard-coded string.
                if deco.args and isinstance(deco.args[0], ast.Name):
                    if deco.args[0].id == "ADMIN_RATE_LIMIT":
                        return True
        return False

    def test_tenant_handler_rate_limited(self):
        assert self._has_limiter(ROUTER_FNS["get_tenant_dashboard"])

    def test_domain_handler_rate_limited(self):
        assert self._has_limiter(ROUTER_FNS["get_domain_dashboard"])

    def test_agent_handler_rate_limited(self):
        assert self._has_limiter(ROUTER_FNS["get_agent_dashboard"])


# --------------------------------------------------------------------- #
# 4. Envelope shape: dataclasses.asdict
# --------------------------------------------------------------------- #


class TestEnvelopeShape:
    def test_to_envelope_uses_asdict(self):
        """`_to_envelope` must use `dataclasses.asdict` -- not a custom
        serializer -- so the JSON envelope automatically tracks the
        sub-branch 2 dataclass shape.
        """
        fn = ROUTER_FNS["_to_envelope"]
        src = ast.unparse(fn)
        assert "asdict(result)" in src, (
            "_to_envelope must convert via dataclasses.asdict to "
            "preserve the design-lock envelope shape"
        )

    def test_asdict_is_imported_from_dataclasses(self):
        src = ROUTER_PATH.read_text()
        assert "from dataclasses import asdict" in src

    def test_each_handler_returns_envelope(self):
        for name in (
            "get_tenant_dashboard",
            "get_domain_dashboard",
            "get_agent_dashboard",
        ):
            fn = ROUTER_FNS[name]
            src = ast.unparse(fn)
            assert "_to_envelope(result)" in src, (
                f"{name} must return _to_envelope(result) -- got {src!r}"
            )


# --------------------------------------------------------------------- #
# 5. No writes
# --------------------------------------------------------------------- #


class TestNoWrites:
    """Read-side handlers. Any .add()/.commit()/.flush() in this module
    would indicate an accidental write path; fail loudly if one appears.
    The DashboardService itself has the same discipline (sub-branch 2);
    the HTTP layer must not reintroduce writes.
    """

    def test_module_has_no_db_writes(self):
        src = ROUTER_PATH.read_text()
        tree = ast.parse(src)
        forbidden = {"add", "commit", "flush", "delete", "merge"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in forbidden, (
                    f"dashboard router contains a `.{node.func.attr}(...)` "
                    f"call -- read handlers must not write to the DB"
                )


# --------------------------------------------------------------------- #
# 6. Router is registered in app/api/router.py
# --------------------------------------------------------------------- #


class TestRouterRegistration:
    def test_dashboard_imported(self):
        src = APP_ROUTER_PATH.read_text()
        assert "from app.api.v1 import dashboard" in src

    def test_dashboard_included(self):
        src = APP_ROUTER_PATH.read_text()
        assert "api_router.include_router(dashboard.router)" in src


# --------------------------------------------------------------------- #
# 7. Perimeter denial: middleware ADMIN_AUTH_PATHS + embed permissions
# --------------------------------------------------------------------- #


class TestPerimeterEmbedDenial:
    """The two-half contract that makes embed keys impossible at the
    dashboard surface:

      half 1: `ADMIN_AUTH_PATHS` includes `/api/v1/dashboard`.
      half 2: `EMBED_REQUIRED_PERMISSIONS` excludes `"admin"`.

    If either half drifts, embed keys could reach a dashboard handler.
    Pin both here.
    """

    def test_admin_auth_paths_includes_dashboard(self):
        src = MIDDLEWARE_PATH.read_text()
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, ast.Assign):
                if any(
                    isinstance(t, ast.Name) and t.id == "ADMIN_AUTH_PATHS"
                    for t in node.targets
                ):
                    value_src = ast.unparse(node.value)
                    assert "/api/v1/dashboard" in value_src, (
                        "ADMIN_AUTH_PATHS must include /api/v1/dashboard "
                        "so the middleware denies embed keys at the "
                        "perimeter before any handler runs"
                    )
                    return
        # Not found -- fail loudly.
        raise AssertionError("ADMIN_AUTH_PATHS assignment not found")

    def test_embed_required_permissions_excludes_admin(self):
        # Import-side check: the contract object the middleware and the
        # issuance validator share.
        from app.schemas.api_key import EMBED_REQUIRED_PERMISSIONS

        assert "admin" not in EMBED_REQUIRED_PERMISSIONS, (
            "EMBED_REQUIRED_PERMISSIONS must not include 'admin' -- "
            "otherwise embed keys could pass the ADMIN_AUTH_PATHS gate"
        )
        # The middleware's gate is `"admin" not in permissions` -> 403,
        # which depends on this precise key being absent from the set.

    def test_embed_required_permissions_is_chat_only(self):
        from app.schemas.api_key import EMBED_REQUIRED_PERMISSIONS

        # Pin the exact set so a permission addition triggers a
        # design-review on this file too.
        assert EMBED_REQUIRED_PERMISSIONS == frozenset({"chat"})


# --------------------------------------------------------------------- #
# 8. DI helper wraps DashboardService
# --------------------------------------------------------------------- #


class TestDI:
    def test_get_dashboard_service_constructs_dashboard_service(self):
        fn = ROUTER_FNS["get_dashboard_service"]
        src = ast.unparse(fn)
        assert "DashboardService(db)" in src, (
            "get_dashboard_service must construct DashboardService(db)"
        )

    def test_get_dashboard_service_returns_dashboard_service(self):
        # The return-annotation check is structural; we want the
        # annotation to read 'DashboardService'.
        fn = ROUTER_FNS["get_dashboard_service"]
        assert fn.returns is not None
        assert isinstance(fn.returns, ast.Name)
        assert fn.returns.id == "DashboardService"


# --------------------------------------------------------------------- #
# 9. Live import sanity: the router module loads + the three handlers
#    have a Request parameter named `request` (slowapi requires it as
#    the first param after the decorator -- it inspects the binding to
#    find the client key).
# --------------------------------------------------------------------- #


class TestLiveImport:
    def test_router_module_imports(self):
        # If something is broken (typo, missing import, etc.) this will
        # raise immediately. Keeps the AST checks honest.
        from app.api.v1 import dashboard

        assert hasattr(dashboard, "router")
        assert hasattr(dashboard, "get_dashboard_service")

    def test_handlers_take_request_first(self):
        from app.api.v1 import dashboard

        for name in (
            "get_tenant_dashboard",
            "get_domain_dashboard",
            "get_agent_dashboard",
        ):
            fn = getattr(dashboard, name)
            sig = inspect.signature(fn)
            params = list(sig.parameters.values())
            assert params, f"{name} has no parameters"
            assert params[0].name == "request", (
                f"{name}: first parameter must be `request` for slowapi"
            )
