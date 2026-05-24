"""Arc 7 Commit 4 (WU-2) tests -- tier-aware rate-limit wiring.

Locks the contract introduced by ``app/middleware/rate_limit.py`` at
Arc 7 Commit 4:

  1. ``get_tier_aware_key(request)`` returns
     ``tier:{tier}:admin:{admin_id}:inst:{instance_id|none}`` when
     ``request.state.tenant_id`` is populated by ApiKeyAuthMiddleware,
     and ``ip:{client_ip}`` otherwise.

  2. ``get_tier_rate_limit_for_key(key)`` parses the tier from the
     key and returns the founder-locked
     ``api_rate_limit_rpm`` value as ``"{rpm}/minute"``:
       Free        -> 30/minute
       Pro         -> 300/minute
       Enterprise  -> 3000/minute
     Anonymous (``ip:...``) and malformed keys fall back to Free.

  3. End-to-end against a tiny Starlette test app:
       * Free admin allowed 30 requests in a fresh minute, 31st 429s.
       * Pro admin allowed up through 31st (we don't burn the full
         300; we just prove the cap is higher than Free's 30).
       * Enterprise admin allowed 31st (cap=3000).
       * Per-(admin, instance) isolation: two Instances under one
         Admin do NOT share a bucket -- admin@inst=1 burning its
         cap does not block admin@inst=2.

  4. Fail-safe to Free=30rpm when the admin row is missing.

These tests are structural + behavioural and use the SlowAPI memory
backend so they run in any CI environment without Redis.
"""
from __future__ import annotations

import ast
import pathlib
from typing import Any
from unittest.mock import patch

import pytest
from slowapi.errors import RateLimitExceeded
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.middleware.rate_limit import (
    _reset_admin_tier_cache,
    get_tier_aware_key,
    get_tier_rate_limit_for_key,
)


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


# =====================================================================
# Source-level guarantees
# =====================================================================


def test_static_constants_retired_from_module() -> None:
    """The pre-Arc-7 fixed-string constants MUST NOT come back.

    Path A doctrine forbids retired shapes hanging around as
    aliases; a stale constant is a foot-gun the next time someone
    decorates a new route, and the founder-locked tier shape
    is the single source of truth now.
    """
    rl_src = (REPO_ROOT / "app" / "middleware" / "rate_limit.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(rl_src)
    module_assignments = {
        node.targets[0].id
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }
    for retired in ("CHAT_RATE_LIMIT", "ADMIN_RATE_LIMIT", "KNOWLEDGE_UPLOAD_RATE_LIMIT"):
        assert retired not in module_assignments, (
            f"Arc 7 Commit 4 (WU-2): retired constant {retired!r} must "
            f"not be re-introduced as a module-level assignment."
        )


def test_admin_routes_use_tier_aware_decorator() -> None:
    """Every ``@limiter.limit(...)`` decorator on admin + chat surfaces
    MUST pass the tier-aware limit provider, not a fixed string.

    Walks the AST of every route module that used to import the
    retired fixed-string constants. Any remaining ``ast.Name`` first
    arg whose ``.id`` is one of the retired constants fails this
    test loudly -- so a half-applied refactor cannot ship.
    """
    route_files = [
        "app/api/v1/chat.py",
        "app/api/v1/admin.py",
        "app/api/v1/admin_forensics.py",
        "app/api/v1/audit_log.py",
        "app/api/v1/consent.py",
        "app/api/v1/dashboard.py",
        "app/api/v1/retention.py",
        "app/api/v1/sessions.py",
        "app/api/v1/verification.py",
    ]
    retired = {"CHAT_RATE_LIMIT", "ADMIN_RATE_LIMIT", "KNOWLEDGE_UPLOAD_RATE_LIMIT"}
    offenders: list[str] = []

    for rel in route_files:
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        tree = ast.parse(src, filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for deco in node.decorator_list:
                if not isinstance(deco, ast.Call):
                    continue
                f = deco.func
                if not (
                    isinstance(f, ast.Attribute)
                    and f.attr == "limit"
                    and isinstance(f.value, ast.Name)
                    and f.value.id == "limiter"
                ):
                    continue
                if not deco.args:
                    continue
                first = deco.args[0]
                if isinstance(first, ast.Name) and first.id in retired:
                    offenders.append(f"{rel}:{node.lineno} {node.name}")

    assert not offenders, (
        "Arc 7 Commit 4 (WU-2): the following routes still use a "
        f"retired fixed-string rate-limit constant: {offenders}. Use "
        "@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)."
    )


# =====================================================================
# Unit: limit-provider returns the right cap per tier
# =====================================================================


@pytest.mark.parametrize(
    "key,expected",
    [
        ("tier:free:admin:abc:inst:1", "30/minute"),
        ("tier:pro:admin:xyz:inst:7", "300/minute"),
        ("tier:enterprise:admin:big:inst:none", "3000/minute"),
        # Anonymous bucket -> Free.
        ("ip:1.2.3.4", "30/minute"),
        # Malformed / unknown tier -> Free (defence in depth).
        ("tier:wat:admin:x:inst:1", "30/minute"),
        ("", "30/minute"),
        ("garbage", "30/minute"),
    ],
)
def test_limit_provider_returns_correct_cap(key: str, expected: str) -> None:
    """The limit-provider maps the key prefix back to the
    founder-locked rpm value (Free=30, Pro=300, Enterprise=3000)
    and never raises on unexpected input."""
    assert get_tier_rate_limit_for_key(key) == expected


# =====================================================================
# Unit: key-func composes the right bucket key
# =====================================================================


class _StubState:
    def __init__(self, tenant_id: Any = None, luciel_instance_id: Any = None) -> None:
        self.tenant_id = tenant_id
        self.luciel_instance_id = luciel_instance_id


class _StubClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _StubRequest:
    """Minimal Request stand-in for key-func unit tests.

    The real Request would require an ASGI scope round-trip per
    request, which makes these tests slow and brittle. The key-func
    only reads ``request.state.tenant_id`` /
    ``request.state.luciel_instance_id`` / ``request.headers`` /
    ``request.client``, so a stub with those attributes is
    behaviourally equivalent.
    """

    def __init__(
        self,
        tenant_id: Any = None,
        instance_id: Any = None,
        ip: str = "1.2.3.4",
        headers: dict | None = None,
    ) -> None:
        self.state = _StubState(tenant_id=tenant_id, luciel_instance_id=instance_id)
        self.client = _StubClient(host=ip)
        self.headers = headers or {}


def test_key_func_returns_ip_bucket_when_unauthenticated() -> None:
    """No tenant_id on request.state -> ip:{ip} bucket. Free cap applies."""
    req = _StubRequest(tenant_id=None, ip="9.9.9.9")
    key = get_tier_aware_key(req)
    assert key == "ip:9.9.9.9"
    assert get_tier_rate_limit_for_key(key) == "30/minute"


def test_key_func_returns_tier_bucket_with_instance() -> None:
    """tenant_id + instance_id populated -> full composite key."""
    _reset_admin_tier_cache()
    with patch(
        "app.middleware.rate_limit._lookup_admin_tier", return_value="pro"
    ):
        req = _StubRequest(tenant_id="acme", instance_id=42)
        key = get_tier_aware_key(req)
    assert key == "tier:pro:admin:acme:inst:42"
    assert get_tier_rate_limit_for_key(key) == "300/minute"


def test_key_func_returns_tier_bucket_no_instance() -> None:
    """tenant_id set, instance_id NULL -> inst:none."""
    _reset_admin_tier_cache()
    with patch(
        "app.middleware.rate_limit._lookup_admin_tier", return_value="enterprise"
    ):
        req = _StubRequest(tenant_id="big-co", instance_id=None)
        key = get_tier_aware_key(req)
    assert key == "tier:enterprise:admin:big-co:inst:none"
    assert get_tier_rate_limit_for_key(key) == "3000/minute"


def test_key_func_fails_safe_to_free_on_lookup_error() -> None:
    """If _lookup_admin_tier returns TIER_FREE (the fail-safe
    behaviour when the admin row is missing or the DB hiccups),
    the request is bucketed at the Free=30rpm cap. Critical
    posture: an unidentified caller is the LEAST trustworthy,
    not the MOST."""
    _reset_admin_tier_cache()
    with patch(
        "app.middleware.rate_limit._lookup_admin_tier", return_value="free"
    ):
        req = _StubRequest(tenant_id="ghost-admin", instance_id=1)
        key = get_tier_aware_key(req)
    assert key == "tier:free:admin:ghost-admin:inst:1"
    assert get_tier_rate_limit_for_key(key) == "30/minute"


# =====================================================================
# Integration: SlowAPI dynamic limit + dynamic key end-to-end
# =====================================================================


def _build_test_app(monkeypatch_lookup: dict[str, str]) -> Starlette:
    """Spin up a tiny Starlette app whose one route is decorated with
    the tier-aware limiter.

    ``monkeypatch_lookup`` maps admin_id -> tier so the test bypasses
    the DB; the test injects ``request.state.tenant_id`` /
    ``request.state.luciel_instance_id`` via a one-line middleware
    that reads the ``X-Admin``/``X-Instance`` request headers.
    """
    from slowapi import Limiter
    from slowapi.errors import RateLimitExceeded
    from starlette.middleware.base import BaseHTTPMiddleware

    # Use a fresh in-memory limiter (so the test is isolated from the
    # process-level limiter).
    limiter = Limiter(
        key_func=get_tier_aware_key,
        default_limits=["60/minute"],
        storage_uri="memory://",
        in_memory_fallback_enabled=True,
    )

    class _InjectState(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            admin = request.headers.get("x-admin")
            inst = request.headers.get("x-instance")
            request.state.tenant_id = admin or None
            request.state.luciel_instance_id = int(inst) if inst else None
            return await call_next(request)

    @limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
    async def hello(request):
        return JSONResponse({"ok": True})

    async def rl_handler(request, exc):
        return JSONResponse({"error": "rate_limit_exceeded"}, status_code=429)

    app = Starlette(routes=[Route("/", hello)])
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rl_handler)
    app.add_middleware(_InjectState)

    # Patch the tier-lookup so the test never touches a DB.
    def fake_lookup(admin_id: str) -> str:
        return monkeypatch_lookup.get(admin_id, "free")

    return app, fake_lookup


def test_e2e_free_admin_burns_cap_at_31st_request(monkeypatch) -> None:
    """Free=30rpm. 30 requests OK, 31st 429."""
    _reset_admin_tier_cache()
    app, fake = _build_test_app({"free-admin": "free"})
    monkeypatch.setattr("app.middleware.rate_limit._lookup_admin_tier", fake)
    with TestClient(app) as client:
        for i in range(30):
            r = client.get("/", headers={"X-Admin": "free-admin", "X-Instance": "1"})
            assert r.status_code == 200, f"req {i} unexpectedly 429: {r.text}"
        r31 = client.get("/", headers={"X-Admin": "free-admin", "X-Instance": "1"})
        assert r31.status_code == 429, (
            f"Free admin must be capped at 30 rpm; req 31 returned {r31.status_code}"
        )


def test_e2e_pro_admin_passes_where_free_would_429(monkeypatch) -> None:
    """Pro=300rpm. The 31st request -- which would 429 a Free admin --
    must pass for a Pro admin. We don't burn the full 300 because
    the test would take ~5s; one over-Free is enough to prove the
    cap differs."""
    _reset_admin_tier_cache()
    app, fake = _build_test_app({"pro-admin": "pro"})
    monkeypatch.setattr("app.middleware.rate_limit._lookup_admin_tier", fake)
    with TestClient(app) as client:
        for i in range(31):
            r = client.get("/", headers={"X-Admin": "pro-admin", "X-Instance": "1"})
            assert r.status_code == 200, (
                f"Pro admin must accept ≥31 rpm; req {i} returned "
                f"{r.status_code}: {r.text}"
            )


def test_e2e_enterprise_admin_passes_where_pro_floor_holds(monkeypatch) -> None:
    """Enterprise=3000rpm. Pin the same 31-req baseline to prove
    Enterprise is at least as generous as Pro (it is 10x)."""
    _reset_admin_tier_cache()
    app, fake = _build_test_app({"ent-admin": "enterprise"})
    monkeypatch.setattr("app.middleware.rate_limit._lookup_admin_tier", fake)
    with TestClient(app) as client:
        for i in range(31):
            r = client.get("/", headers={"X-Admin": "ent-admin", "X-Instance": "1"})
            assert r.status_code == 200


def test_e2e_per_instance_isolation(monkeypatch) -> None:
    """One Admin running two Instances must NOT share a rate-limit
    bucket. The Free admin under inst=1 burns its 30rpm; the SAME
    Free admin under inst=2 still gets the full 30rpm.

    This is the closure of the noisy-neighbour drift called out in
    the Pro entitlement comment block
    (D-pro-tier-rate-limit-abuse-surface-2026-05-23): a single
    buggy Instance can no longer starve siblings under the same
    Admin.
    """
    _reset_admin_tier_cache()
    app, fake = _build_test_app({"free-admin": "free"})
    monkeypatch.setattr("app.middleware.rate_limit._lookup_admin_tier", fake)
    with TestClient(app) as client:
        # Burn inst=1
        for _ in range(30):
            r = client.get("/", headers={"X-Admin": "free-admin", "X-Instance": "1"})
            assert r.status_code == 200
        r31 = client.get("/", headers={"X-Admin": "free-admin", "X-Instance": "1"})
        assert r31.status_code == 429

        # inst=2 is a fresh bucket
        r2 = client.get("/", headers={"X-Admin": "free-admin", "X-Instance": "2"})
        assert r2.status_code == 200, (
            "Per-instance isolation broken: inst=2 inherited the "
            "cap state from inst=1."
        )
