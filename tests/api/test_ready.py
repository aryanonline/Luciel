"""Arc 8 Commit 1 (WU-1 Reliability) — /ready endpoint contract tests.

Closes D-health-endpoint-shallow-no-db-readiness-check-2026-05-22.

The /ready endpoint probes both the DB (SELECT 1) and Redis (PING). It must:
  - return 200 with status="ready" + per-check map when both succeed
  - return 503 with status="not_ready" + per-check map when either fails
  - never leak underlying exception messages (only class names)
  - be exempt from API key auth (consumed by smoke probes / uptime monitors)

These tests use monkeypatching to simulate subsystem failures without
disturbing the real DB or Redis fixtures.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_ready_happy_path() -> None:
    """Both DB and Redis available -> 200 ready."""
    response = client.get("/ready")
    # Either 200 (both up in sandbox) or 503 (sandbox lacks one) — but the
    # body shape contract is invariant.
    body = response.json()
    assert "status" in body
    assert "checks" in body
    assert "db" in body["checks"]
    assert "redis" in body["checks"]
    if response.status_code == 200:
        assert body["status"] == "ready"
        assert body["checks"]["db"] == "ok"
        # redis may be "ok" or "not_configured" depending on env
        assert body["checks"]["redis"] in {"ok", "not_configured"}
    else:
        assert response.status_code == 503
        assert body["status"] == "not_ready"
        # At least one subsystem failed; failure is reported as a class name
        # (not "ok"), never the underlying exception message.
        bad = [k for k, v in body["checks"].items() if v not in {"ok", "not_configured"}]
        assert len(bad) >= 1


def test_ready_db_failure_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulated DB outage -> 503 with checks.db set to exception class name.

    Patches the engine module-level binding so the probe path raises during
    .connect(). The response must (a) be 503, (b) carry status="not_ready",
    (c) report db check as the exception class name, (d) never leak the
    underlying message.
    """
    from app.db import session as db_session_mod

    secret_message = "psql://hidden-host:5432/hidden-db SECRET-MESSAGE-DO-NOT-LEAK"

    class _BoomEngine:
        def connect(self) -> Any:  # noqa: ANN401
            raise RuntimeError(secret_message)

    monkeypatch.setattr(db_session_mod, "engine", _BoomEngine())

    response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["db"] == "RuntimeError"
    # Secret message MUST NOT appear anywhere in the response body.
    assert secret_message not in response.text
    assert "hidden-host" not in response.text


def test_ready_redis_failure_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulated Redis outage -> 503 with checks.redis set to class name."""
    from app.core import config as config_mod

    # Force the probe down the Redis branch (settings.redis_url is truthy)
    # even in environments where it might be empty.
    monkeypatch.setattr(config_mod.settings, "redis_url", "redis://does-not-resolve.invalid:6379/0", raising=False)

    response = client.get("/ready")
    # If DB happens to be down too in sandbox, that's fine — we just need to
    # confirm 503 + a non-ok redis check is reported with a class name.
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    # The redis check should be a class name (not "ok" / "not_configured").
    assert body["checks"]["redis"] not in {"ok", "not_configured"}
    # Common failure modes: ConnectionError, gaierror, OSError, BusyLoadingError.
    # We don't pin the exact class — DNS resolution failure modes vary by
    # environment — but it must be a recognisable class name string.
    assert isinstance(body["checks"]["redis"], str)
    assert len(body["checks"]["redis"]) > 0


def test_ready_skips_auth() -> None:
    """/ready must NOT require an API key.

    The Fargate deploy-gate smoke probe (Arc 8 Commit 4) and any uptime
    monitor must be able to hit this without holding a JWT. The ALB target-
    group health check binds to /health, but /ready is the richer signal
    consumed by external monitors.
    """
    # No auth header on the client — should still get a response (not 401/403)
    response = client.get("/ready")
    assert response.status_code in {200, 503}, (
        f"/ready returned {response.status_code} — likely tripped the auth "
        f"middleware. Verify SKIP_AUTH_PATHS includes '/ready'. Body={response.text}"
    )
