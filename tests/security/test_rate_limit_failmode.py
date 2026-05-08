"""Step 29.y Cluster 5 (B-1) tests -- rate-limit fail-mode hardening.

The audit (findings_phase1b.md B-1, PHASE3_REMEDIATION_REPORT row 5)
called out three problems with app/middleware/rate_limit.py:

  1. The module read os.getenv("REDISURL") -- no underscore. Every
     other component reads REDIS_URL. In prod the typo silently
     resolved to None and SlowAPI fell through to memory:// storage,
     making per-route limits per-process instead of shared. With N
     ECS tasks behind the ALB a caller could do N*60/min on a
     "60/minute" limit. This was a security regression with no log.

  2. No connection-pool tuning. A flaky Redis would stall every
     request waiting on the default socket timeout, then bubble a
     long stack instead of returning a clean response.

  3. The fallback middleware failed OPEN for every HTTP method, and
     the fail-open implementation (re-calling call_next with the
     limiter disabled) is broken in modern Starlette because
     BaseHTTPMiddleware streams cannot be re-consumed within a
     single request. The retried call_next silently produced a 500.

The fix layers two mechanisms:
  * SlowAPI Limiter is constructed with in_memory_fallback_enabled
    so reads automatically degrade to per-process limiting when the
    primary backend dies. This is the read fail-open.
  * The fallback middleware intercepts exceptions that still escape
    (e.g. the storage probe itself raising during request entry)
    and returns 503 for write methods so quota integrity is
    preserved; non-write methods re-raise so we never silently
    swallow real errors as 200s.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
RL_PATH = REPO_ROOT / "app" / "middleware" / "rate_limit.py"


def _read_source() -> str:
    return RL_PATH.read_text(encoding="utf-8")


# =====================================================================
# B-1 source-level guarantees
# =====================================================================

def test_b1_reads_correct_env_var_name() -> None:
    """The module MUST read the Redis URL via the canonical path, not
    a typo'd env-var name.

    Pre-29.y the module called ``os.getenv("REDISURL")`` (no
    underscore), which silently neutered shared rate limits in
    prod. After Cluster 5 (B-1) the correct name was restored as
    ``os.getenv("REDIS_URL")``. After C19
    (``D-redis-url-centralize-via-settings-2026-05-08``) the module
    no longer reads the env var directly at all; it reads via
    ``settings.redis_url`` from ``app.core.config``, which is the
    single source of truth for every component that needs the
    Redis URL.

    This test now enforces the C19 architecture rather than the
    pre-C19 one. The intent (no REDISURL typo regression) is
    preserved -- and strengthened, because:

      1. Settings is a Pydantic model with a typed ``redis_url``
         field. A typo would be a Pydantic validation error at
         boot, not a silent None.
      2. There is exactly one place to read the Redis URL from
         (``app.core.config.settings.redis_url``); a future
         engineer cannot accidentally introduce a second reader
         site with a different env-var name without first removing
         the Settings-mediated read here.

    Closed by C30 (``D-c19-tests-not-updated-after-redis-centralize-2026-05-08``).
    """
    src = _read_source()
    tree = ast.parse(src)

    # Negative guard: no direct os.getenv / os.environ.get call
    # against the legacy or canonical env-var names. The Settings-
    # mediated path is the only valid reader.
    bad_typo: list[tuple[str, int]] = []
    direct_canonical: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        dotted = ""
        if isinstance(func, ast.Attribute):
            parts: list[str] = [func.attr]
            cur = func.value
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
            dotted = ".".join(reversed(parts))
        if dotted not in {"os.getenv", "os.environ.get"}:
            continue
        if not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        name = first.value
        if name == "REDISURL":
            bad_typo.append((name, node.lineno))
        if name == "REDIS_URL":
            direct_canonical.append((name, node.lineno))

    assert not bad_typo, (
        "B-1: rate_limit.py still reads the typo'd env var "
        "REDISURL. This is the original pre-29.y blocker -- it "
        "silently resolves to None in prod (where REDIS_URL is "
        "the actual name) and downgrades rate limiting to "
        "per-process memory. Found at lines: "
        f"{[lineno for _, lineno in bad_typo]}"
    )
    assert not direct_canonical, (
        "B-1 / C19: rate_limit.py must NOT call os.getenv(\"REDIS_URL\") "
        "or os.environ.get(\"REDIS_URL\") directly. After C19 "
        "(D-redis-url-centralize-via-settings-2026-05-08) the only "
        "valid reader is `settings.redis_url` from "
        "`app.core.config`. Direct env reads bypass the single "
        "source of truth and re-open the typo-regression risk "
        "that landed us here. Found at lines: "
        f"{[lineno for _, lineno in direct_canonical]}"
    )

    # Positive guard: the module imports settings and reads
    # settings.redis_url. Both are required -- import without read
    # is dead code; read without import is a NameError.
    has_settings_import = False
    has_settings_redis_url_read = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod_name = node.module or ""
            if mod_name == "app.core.config":
                for alias in node.names:
                    if alias.name == "settings":
                        has_settings_import = True
        if isinstance(node, ast.Attribute) and node.attr == "redis_url":
            inner = node.value
            if isinstance(inner, ast.Name) and inner.id == "settings":
                has_settings_redis_url_read = True

    assert has_settings_import, (
        "B-1 / C19: rate_limit.py must import `settings` from "
        "`app.core.config` so it shares the single source of truth "
        "for Redis URL with every other component (worker celery, "
        "verification probes, etc.). See "
        "D-redis-url-centralize-via-settings-2026-05-08."
    )
    assert has_settings_redis_url_read, (
        "B-1 / C19: rate_limit.py must read the Redis URL via "
        "`settings.redis_url` (a typed Pydantic field) so a typo "
        "is caught at boot as a validation error rather than "
        "silently resolving to None. See "
        "D-redis-url-centralize-via-settings-2026-05-08."
    )


def test_b1_redis_url_module_constant_exposed() -> None:
    """Sanity: the module MUST expose REDIS_URL as a module-level
    name so other modules / tests can introspect it."""
    from app.middleware import rate_limit
    assert hasattr(rate_limit, "REDIS_URL"), (
        "B-1: rate_limit must expose REDIS_URL at module scope."
    )


def test_b1_storage_options_carry_retry_on_timeout_when_redis_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When REDIS_URL is set the storage_options dict passed to the
    Limiter MUST include retry_on_timeout=True plus tight socket
    timeouts so the limiter fails fast and the fallback middleware
    can decide what to do.

    After C19 (D-redis-url-centralize-via-settings-2026-05-08) the
    rate-limit module reads through ``settings.redis_url``, so the
    monkeypatched env var must propagate through a Settings reload
    (not just a rate_limit reload) for the test to reflect the
    monkeypatched value. Without this Settings reload the test
    would observe the cached `.env`-less default of
    ``redis://localhost:6379/0`` regardless of what env we set.

    Updated by C30 (``D-c19-tests-not-updated-after-redis-centralize-2026-05-08``).
    """
    monkeypatch.setenv("REDIS_URL", "redis://stub:6379/0")

    import importlib
    import app.core.config as cfg
    import app.middleware.rate_limit as mod
    cfg = importlib.reload(cfg)
    mod = importlib.reload(mod)
    try:
        assert cfg.settings.redis_url == "redis://stub:6379/0", (
            "B-1 / C19: Settings reload must propagate the "
            "monkeypatched REDIS_URL into settings.redis_url. If "
            "this fails, the rate_limit module is reading from a "
            "stale Settings instance."
        )
        assert mod.REDIS_URL == "redis://stub:6379/0"
        assert mod.storage_uri == "redis://stub:6379/0"
        opts = mod.storage_options
        assert opts.get("retry_on_timeout") is True, (
            "B-1: storage_options must set retry_on_timeout=True so "
            "single-RTT blips do not bubble exceptions."
        )
        assert "socket_connect_timeout" in opts, (
            "B-1: storage_options must set socket_connect_timeout."
        )
        assert "socket_timeout" in opts, (
            "B-1: storage_options must set socket_timeout so the "
            "limiter fails fast and the fallback middleware can run."
        )
        assert "health_check_interval" in opts, (
            "B-1: storage_options must set health_check_interval "
            "so idle pools recover from broken sockets."
        )
    finally:
        monkeypatch.delenv("REDIS_URL", raising=False)
        importlib.reload(cfg)
        importlib.reload(mod)


def test_b1_storage_options_empty_when_redis_unset() -> None:
    """Local dev path: REDIS_URL unset -> memory:// + empty options."""
    from app.middleware import rate_limit as mod
    if mod.REDIS_URL is None:
        assert mod.storage_uri == "memory://"
        assert mod.storage_options == {}


def test_b1_limiter_has_in_memory_fallback_enabled() -> None:
    """The Limiter MUST be constructed with
    in_memory_fallback_enabled=True so reads fail open transparently
    when the primary backend dies."""
    from app.middleware.rate_limit import limiter
    # SlowAPI exposes the flag as _in_memory_fallback_enabled (see
    # slowapi/extension.py). Verify it is True.
    assert getattr(limiter, "_in_memory_fallback_enabled", False) is True, (
        "B-1: Limiter must enable in_memory_fallback so reads fail "
        "open during Redis outages instead of 500ing."
    )


# =====================================================================
# B-1 fail-mode posture: writes fail closed, reads do NOT swallow
# =====================================================================

def test_b1_write_methods_constant_defined() -> None:
    from app.middleware.rate_limit import WRITE_METHODS
    assert isinstance(WRITE_METHODS, frozenset), (
        "B-1: WRITE_METHODS must be a frozenset (immutable)."
    )
    for m in ("POST", "PUT", "PATCH", "DELETE"):
        assert m in WRITE_METHODS, (
            f"B-1: WRITE_METHODS must include {m} -- writes "
            "fail closed when the rate-limit storage is down."
        )
    for m in ("GET", "HEAD", "OPTIONS"):
        assert m not in WRITE_METHODS, (
            f"B-1: WRITE_METHODS must NOT include {m} -- reads "
            "fall back via in_memory_fallback during outages."
        )


def _build_failmode_app(raise_exc: BaseException) -> Starlette:
    """Build a tiny Starlette app whose every endpoint raises the
    given exception, with the real RateLimitFallbackMiddleware
    mounted on top. This lets us assert the middleware's response
    to backend failures without standing up Redis."""
    from app.middleware.rate_limit import create_rate_limit_middleware

    async def _boom(request):
        raise raise_exc

    app = Starlette(
        debug=False,
        routes=[
            Route("/read", _boom, methods=["GET"]),
            Route("/write", _boom, methods=["POST"]),
            Route("/write-put", _boom, methods=["PUT"]),
            Route("/write-patch", _boom, methods=["PATCH"]),
            Route("/write-delete", _boom, methods=["DELETE"]),
        ],
    )
    middleware_cls = create_rate_limit_middleware()
    app.add_middleware(middleware_cls)
    return app


def test_b1_post_fails_closed_on_redis_outage() -> None:
    """POST during a Redis outage must return 503 with Retry-After,
    so the ALB / caller backs off."""
    exc = ConnectionError("redis connection refused: ECONNREFUSED")
    app = _build_failmode_app(exc)
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post("/write", json={})
    assert r.status_code == 503, r.text
    assert r.headers.get("retry-after") == "5"
    body = r.json()
    assert body.get("error") == "rate_limit_backend_unavailable"


@pytest.mark.parametrize("method,path", [
    ("PUT", "/write-put"),
    ("PATCH", "/write-patch"),
    ("DELETE", "/write-delete"),
])
def test_b1_other_writes_fail_closed_on_redis_outage(
    method: str, path: str
) -> None:
    exc = ConnectionError("redis connection reset by peer")
    app = _build_failmode_app(exc)
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.request(method, path, json={})
    assert r.status_code == 503, (method, r.status_code, r.text)


def test_b1_get_does_not_swallow_redis_error_as_200() -> None:
    """If a Redis error escapes for a GET, the middleware must NOT
    silently mask it as a 200 -- it re-raises so the caller sees the
    real error. The in_memory_fallback on the Limiter is the actual
    fail-open mechanism (covered by
    test_b1_limiter_has_in_memory_fallback_enabled)."""
    exc = ConnectionError("redis connection refused")
    app = _build_failmode_app(exc)
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/read")
    # The exception was re-raised -> Starlette converts to 500
    # under the test client. The crucial assertion: NOT a 200,
    # NOT a silently-swallowed 503-on-GET.
    assert r.status_code != 200, (
        "B-1: GET fail path must not silently produce a 200. The "
        "in_memory_fallback handles graceful degradation; if an "
        "exception still escapes the route, surface it."
    )
    assert r.status_code != 503, (
        "B-1: 503 fail-closed is for write methods only."
    )


def test_b1_non_redis_exception_is_reraised_on_writes() -> None:
    """Application errors unrelated to the rate-limit backend MUST
    bubble up unchanged on writes -- the middleware must NOT swallow
    them as 503s."""
    exc = ValueError("application validation error: field foo is invalid")
    app = _build_failmode_app(exc)
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post("/write", json={})
    assert r.status_code != 503, (
        "B-1: non-Redis exceptions must not be coerced into 503; "
        "fail-closed only applies to actual rate-limit backend errors."
    )


def test_b1_non_redis_exception_is_reraised_on_reads() -> None:
    exc = ValueError("application validation error: field foo is invalid")
    app = _build_failmode_app(exc)
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/read")
    assert r.status_code != 503
    assert r.status_code != 200  # the boom endpoint cannot succeed


def test_b1_is_rate_limit_backend_error_is_specific() -> None:
    """The classifier must NOT match every exception that happens to
    contain the word "timeout" -- application code raising
    ValueError("request timed out for user X") would otherwise be
    misclassified as a Redis backend error."""
    from app.middleware.rate_limit import _is_rate_limit_backend_error
    # True cases.
    assert _is_rate_limit_backend_error(
        ConnectionError("redis connection refused")
    )
    assert _is_rate_limit_backend_error(
        Exception("Error 111 connecting to localhost:6379. Connection refused.")
    )
    assert _is_rate_limit_backend_error(
        Exception("redis.exceptions.ConnectionError")
    )
    # False cases: real application errors that should bubble up.
    assert not _is_rate_limit_backend_error(
        ValueError("user input invalid")
    )
    assert not _is_rate_limit_backend_error(
        RuntimeError("unrelated bug in business logic")
    )


# =====================================================================
# Smoke import
# =====================================================================

def test_cluster5_module_imports() -> None:
    import importlib
    importlib.import_module("app.middleware.rate_limit")
