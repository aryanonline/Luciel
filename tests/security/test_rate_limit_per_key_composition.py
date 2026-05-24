"""Arc 8 Commit 3 (WU-3 abuse-surface) tests -- per-embed-key bucket
+ composition + bucket_scope reporting.

Locks the contract introduced by ``app/middleware/rate_limit.py`` and
``app/policy/entitlements.py`` at Arc 8 Commit 3:

  1. ``per_instance_api_rate_limit_rpm(tier=...)`` derivation:
       Free        -> 30 (30rpm / 1 instance)
       Pro         -> 30 (300rpm / 10 instances)
       Enterprise  -> 3000 (unlimited subdivision = identity)

  2. ``per_key_api_rate_limit_rpm(tier=...)`` derivation:
       Free        -> 30 (30rpm / 1 key)
       Pro         -> 30 (300rpm / 10 keys)
       Enterprise  -> 30 (3000rpm / 100 keys)

  3. ``get_embed_key_aware_key(request)`` returns
     ``embed:tier:{tier}:admin:{admin_id}:key:{api_key_id}`` when the
     request carries a resolved embed key, ``ip:{client_ip}``
     otherwise. Admin keys fall through to the IP bucket so they do
     not inherit the per-embed-key generous derivation.

  4. ``get_embed_key_rate_limit_for_key(key)`` parses the tier and
     returns the per-key cap as ``"{rpm}/minute"``.

  5. End-to-end against a tiny Starlette test app:
       * Per-embed-key isolation: two embed keys under one Pro Admin
         do NOT share a bucket -- key=A burning its 30rpm cap does
         NOT block key=B.
       * One Pro embed key burns at 30rpm (the per-key derived cap),
         NOT at the 300rpm admin-level tier-aware cap.

  6. ``bucket_scope`` on 429 response surfaces the bucket label
     (``tier_admin_instance`` / ``embed_key`` / ``ip``) so the
     client and ops can distinguish which bucket fired.

  7. Defensive: 1rpm floor when ``api_rate_limit_rpm < count_cap``
     (cannot zero a bucket).

These tests use the SlowAPI memory backend so they run without Redis.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.middleware.rate_limit import (
    _classify_bucket_scope,
    _reset_admin_tier_cache,
    get_embed_key_aware_key,
    get_embed_key_rate_limit_for_key,
    get_tier_aware_key,
    rate_limit_exceeded_handler,
)
from app.policy.entitlements import (
    per_instance_api_rate_limit_rpm,
    per_key_api_rate_limit_rpm,
)


# =====================================================================
# Entitlement derivations
# =====================================================================


@pytest.mark.parametrize(
    "tier,expected",
    [
        ("free", 30),
        ("pro", 30),
        ("enterprise", 3000),
    ],
)
def test_per_instance_derivation(tier: str, expected: int) -> None:
    """Per-instance rpm derived = api_rate_limit_rpm // instance_count_cap.

    Enterprise instance_count_cap=None (unlimited) -> identity = 3000.
    """
    assert per_instance_api_rate_limit_rpm(tier=tier) == expected


@pytest.mark.parametrize(
    "tier,expected",
    [
        ("free", 30),
        ("pro", 30),
        ("enterprise", 30),
    ],
)
def test_per_key_derivation(tier: str, expected: int) -> None:
    """Per-key rpm derived = api_rate_limit_rpm // embed_key_count_cap.

    Pro: 300 / 10 = 30. Enterprise: 3000 / 100 = 30. Each tier ends
    up at 30rpm per key by design -- the multiplier on api_rate_limit_rpm
    matches the multiplier on embed_key_count_cap.
    """
    assert per_key_api_rate_limit_rpm(tier=tier) == expected


def test_per_instance_with_override_enterprise() -> None:
    """Enterprise override hook still flows through derivations."""
    rpm = per_instance_api_rate_limit_rpm(
        tier="enterprise",
        overrides={"api_rate_limit_rpm": 6000, "instance_count_cap": 30},
    )
    assert rpm == 200  # 6000 // 30


def test_per_instance_floor_when_cap_exceeds_rpm() -> None:
    """If api_rate_limit_rpm < instance_count_cap the floor of 1rpm
    must kick in so the bucket never completely zeroes out."""
    # Synthetic enterprise override that would otherwise yield 0:
    rpm = per_instance_api_rate_limit_rpm(
        tier="enterprise",
        overrides={"api_rate_limit_rpm": 5, "instance_count_cap": 10},
    )
    assert rpm == 1


def test_per_key_zero_cap_returns_full_rpm() -> None:
    """``embed_key_count_cap=0`` is treated as 'no subdivision' so
    the bucket inherits the full admin-level rpm rather than crashing
    with ZeroDivisionError."""
    rpm = per_key_api_rate_limit_rpm(
        tier="enterprise",
        overrides={"api_rate_limit_rpm": 3000, "embed_key_count_cap": 0},
    )
    assert rpm == 3000


# =====================================================================
# Per-embed-key key-func unit
# =====================================================================


class _StubState:
    def __init__(
        self,
        tenant_id: Any = None,
        api_key_id: Any = None,
        key_kind: Any = None,
        luciel_instance_id: Any = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.api_key_id = api_key_id
        self.key_kind = key_kind
        self.luciel_instance_id = luciel_instance_id


class _StubClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _StubRequest:
    def __init__(
        self,
        tenant_id: Any = None,
        api_key_id: Any = None,
        key_kind: Any = None,
        ip: str = "1.2.3.4",
        headers: dict | None = None,
    ) -> None:
        self.state = _StubState(
            tenant_id=tenant_id,
            api_key_id=api_key_id,
            key_kind=key_kind,
        )
        self.client = _StubClient(host=ip)
        self.headers = headers or {}


def test_embed_key_func_anonymous_falls_back_to_ip() -> None:
    req = _StubRequest(tenant_id=None, ip="7.7.7.7")
    key = get_embed_key_aware_key(req)
    assert key == "ip:7.7.7.7"


def test_embed_key_func_admin_key_falls_back_to_ip() -> None:
    """Admin keys reaching this code path -- which shouldn't happen,
    because require_embed_key gates them off -- must fall through to
    the IP bucket so they cannot inherit the more-generous per-key
    derivation."""
    req = _StubRequest(
        tenant_id="acme", api_key_id="key-123", key_kind="admin", ip="3.3.3.3"
    )
    key = get_embed_key_aware_key(req)
    assert key == "ip:3.3.3.3"


def test_embed_key_func_returns_per_key_bucket() -> None:
    """Embed key with full state -> embed:tier:{tier}:admin:{a}:key:{k}."""
    _reset_admin_tier_cache()
    with patch(
        "app.middleware.rate_limit._lookup_admin_tier", return_value="pro"
    ):
        req = _StubRequest(
            tenant_id="acme", api_key_id="key-abc", key_kind="embed"
        )
        key = get_embed_key_aware_key(req)
    assert key == "embed:tier:pro:admin:acme:key:key-abc"
    assert get_embed_key_rate_limit_for_key(key) == "30/minute"


def test_embed_key_limit_provider_anonymous_is_free() -> None:
    """ip:{ip} (anonymous) keys fall back to Free per-key cap."""
    assert get_embed_key_rate_limit_for_key("ip:9.9.9.9") == "30/minute"


def test_embed_key_limit_provider_malformed_is_free() -> None:
    """Malformed keys (defence in depth) fall back to Free per-key."""
    assert get_embed_key_rate_limit_for_key("garbage:not:a:bucket") == "30/minute"


@pytest.mark.parametrize(
    "tier,expected",
    [
        ("free", "30/minute"),
        ("pro", "30/minute"),
        ("enterprise", "30/minute"),
    ],
)
def test_embed_key_limit_provider_per_tier(tier: str, expected: str) -> None:
    """Every tier ends up at 30rpm per key by Option-A construction."""
    key = f"embed:tier:{tier}:admin:acme:key:k1"
    assert get_embed_key_rate_limit_for_key(key) == expected


# =====================================================================
# bucket_scope classifier
# =====================================================================


@pytest.mark.parametrize(
    "key,expected",
    [
        ("embed:tier:pro:admin:acme:key:k1", "embed_key"),
        ("tier:free:admin:acme:inst:1", "tier_admin_instance"),
        ("tier:enterprise:admin:big:inst:none", "tier_admin_instance"),
        ("ip:1.2.3.4", "ip"),
        ("garbage", "unknown"),
        (None, "unknown"),
        (12345, "unknown"),
    ],
)
def test_classify_bucket_scope(key: Any, expected: str) -> None:
    assert _classify_bucket_scope(key) == expected


# =====================================================================
# 429 response surfaces bucket_scope
# =====================================================================


class _StubLimitExceeded(Exception):
    """Stand-in for slowapi.errors.RateLimitExceeded with the same
    shape the handler reads (``detail``, ``limit.key_func``)."""

    def __init__(self, detail: str, key_func) -> None:
        super().__init__(detail)
        self.detail = detail

        class _L:
            pass

        self.limit = _L()
        self.limit.key_func = key_func


def test_handler_surfaces_embed_key_scope_on_429() -> None:
    """When a widget request 429s, bucket_scope == 'embed_key'."""
    _reset_admin_tier_cache()
    with patch(
        "app.middleware.rate_limit._lookup_admin_tier", return_value="pro"
    ):
        req = _StubRequest(
            tenant_id="acme", api_key_id="k1", key_kind="embed"
        )
        exc = _StubLimitExceeded("30/minute exceeded", get_embed_key_aware_key)
        resp = rate_limit_exceeded_handler(req, exc)

    import json
    body = json.loads(resp.body)
    assert resp.status_code == 429
    assert body["bucket_scope"] == "embed_key"
    assert body["error"] == "rate_limit_exceeded"


def test_handler_surfaces_tier_admin_instance_scope_on_429() -> None:
    """When an admin/chat request 429s, bucket_scope ==
    'tier_admin_instance'."""
    _reset_admin_tier_cache()
    with patch(
        "app.middleware.rate_limit._lookup_admin_tier", return_value="free"
    ):
        # tenant_id populated, key_kind absent (admin path doesn't set
        # it on this stub) -- embed-key func will see no key_kind and
        # fall to ip, but the tier-aware key-func should fire here.
        req = _StubRequest(tenant_id="ghost-admin")
        # Force the request through luciel_instance_id by mutating
        # state directly so the tier-aware key returns a tier:* bucket.
        req.state.luciel_instance_id = 7
        exc = _StubLimitExceeded("30/minute exceeded", get_tier_aware_key)
        resp = rate_limit_exceeded_handler(req, exc)

    import json
    body = json.loads(resp.body)
    assert resp.status_code == 429
    assert body["bucket_scope"] == "tier_admin_instance"


def test_handler_surfaces_ip_scope_on_429() -> None:
    """Anonymous caller 429: bucket_scope == 'ip'."""
    req = _StubRequest(tenant_id=None, ip="9.9.9.9")
    exc = _StubLimitExceeded("30/minute exceeded", get_tier_aware_key)
    resp = rate_limit_exceeded_handler(req, exc)

    import json
    body = json.loads(resp.body)
    assert resp.status_code == 429
    assert body["bucket_scope"] == "ip"


def test_handler_falls_through_to_unknown_when_keyfunc_explodes() -> None:
    """Defensive: if the key-func itself raises, we don't crash the
    429 -- we tag the scope as 'unknown' and still ship the body."""
    def boom(_req):
        raise RuntimeError("simulated key-func failure")

    req = _StubRequest(tenant_id=None, ip="1.1.1.1")
    exc = _StubLimitExceeded("30/minute exceeded", boom)
    resp = rate_limit_exceeded_handler(req, exc)

    import json
    body = json.loads(resp.body)
    assert resp.status_code == 429
    assert body["bucket_scope"] == "unknown"


# =====================================================================
# Integration: per-embed-key isolation under SlowAPI
# =====================================================================


def _build_widget_test_app(tier_for_admin: str = "pro") -> tuple[Starlette, Any]:
    """Spin up a tiny Starlette app whose one route is decorated with
    the per-embed-key limiter. The injecting middleware reads
    X-Admin / X-Key / X-Kind headers and writes them onto
    request.state to mirror what ApiKeyAuthMiddleware does in prod.
    """
    limiter = Limiter(
        key_func=get_embed_key_aware_key,
        default_limits=["60/minute"],
        storage_uri="memory://",
        in_memory_fallback_enabled=True,
    )

    class _InjectState(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            admin = request.headers.get("x-admin")
            api_key = request.headers.get("x-key")
            kind = request.headers.get("x-kind") or "embed"
            request.state.tenant_id = admin or None
            request.state.api_key_id = api_key or None
            request.state.key_kind = kind if admin else None
            return await call_next(request)

    @limiter.limit(
        get_embed_key_rate_limit_for_key,
        key_func=get_embed_key_aware_key,
    )
    async def widget(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/widget", widget)])
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.add_middleware(_InjectState)

    def fake_lookup(_admin_id: str) -> str:
        return tier_for_admin

    return app, fake_lookup


def test_e2e_per_embed_key_isolation(monkeypatch) -> None:
    """Two embed keys under one Pro Admin must NOT share a bucket.

    Closes the per-key half of D-pro-tier-rate-limit-abuse-surface:
    a leaked or buggy key cannot starve siblings under the same admin
    because each key gets its own 30rpm derived bucket.
    """
    _reset_admin_tier_cache()
    app, fake = _build_widget_test_app("pro")
    monkeypatch.setattr("app.middleware.rate_limit._lookup_admin_tier", fake)
    with TestClient(app) as client:
        # Burn key=A to its 30rpm cap.
        for i in range(30):
            r = client.get(
                "/widget",
                headers={"X-Admin": "pro-admin", "X-Key": "key-A"},
            )
            assert r.status_code == 200, f"req {i} unexpectedly 429: {r.text}"
        r_burnt = client.get(
            "/widget", headers={"X-Admin": "pro-admin", "X-Key": "key-A"}
        )
        assert r_burnt.status_code == 429, (
            "Per-key cap must fire at the 31st request on key-A"
        )
        assert r_burnt.json()["bucket_scope"] == "embed_key"

        # key=B under the SAME admin must still pass -- the bucket is
        # per-key, not per-admin.
        r_sibling = client.get(
            "/widget", headers={"X-Admin": "pro-admin", "X-Key": "key-B"}
        )
        assert r_sibling.status_code == 200, (
            "Per-key isolation broken: key-B inherited key-A's cap "
            "state. The whole point of WU-3 is that this does NOT "
            "happen."
        )


def test_e2e_per_embed_key_caps_at_derived_rpm_not_admin_rpm(monkeypatch) -> None:
    """A Pro Admin's single embed key burns at 30rpm (the DERIVED
    per-key cap), not at the 300rpm admin-level cap.

    Pre-WU-3 a single leaked key on Pro could burn the whole 300rpm
    admin allotment. Post-WU-3 the per-key bucket cuts that off at
    30rpm. This test pins the 31st request on a single key to 429.
    """
    _reset_admin_tier_cache()
    app, fake = _build_widget_test_app("pro")
    monkeypatch.setattr("app.middleware.rate_limit._lookup_admin_tier", fake)
    with TestClient(app) as client:
        for i in range(30):
            r = client.get(
                "/widget",
                headers={"X-Admin": "pro-admin", "X-Key": "key-only"},
            )
            assert r.status_code == 200, f"req {i} unexpectedly 429: {r.text}"
        r31 = client.get(
            "/widget", headers={"X-Admin": "pro-admin", "X-Key": "key-only"}
        )
        assert r31.status_code == 429, (
            "Per-key cap must fire at 30rpm on Pro -- pre-WU-3 this "
            "would have passed because the bucket was the 300rpm "
            "admin bucket."
        )
        body = r31.json()
        assert body["bucket_scope"] == "embed_key"
        assert body["error"] == "rate_limit_exceeded"
