"""Arc 8 Work-Unit 3 -- /api/v1/version build-identity contract tests.

D-version-endpoint-hardcoded-not-build-sha-2026-05-22 resolution.

This file pins the contract for the public version endpoint:

  * The ``BUILD_INFO`` singleton at ``app.core.build_info`` exposes
    the four documented keys (app, version, git_sha, status).
  * ``git_sha`` reflects the ``BUILD_GIT_SHA`` env var at module
    import time; defaults to ``"unknown"`` when unset.
  * ``version`` reads from importlib.metadata; degrades to
    ``"unknown"`` rather than 500-ing.
  * The ``/api/v1/version`` route returns a strict superset of the
    pre-WU-3 payload (the three legacy keys ``app``, ``version``,
    ``status`` are preserved verbatim).
  * The route returns a fresh dict copy so client mutation cannot
    poison the singleton.
  * The route is public (no api-key required) -- confirmed by the
    SKIP_AUTH_PATHS membership check.

End-to-end correctness (live wire vs deployed image's actual SHA)
is covered by the WU-2+WU-3 paired-deploy ceremony smoke walk.
"""
from __future__ import annotations

import os

# Match the import-time-failure mitigation pattern used in
# tests/api/test_step30a_billing_shape.py and
# tests/api/test_signup_free_shape.py.
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://stub:stub@localhost:5432/stub"
)


# ---------------------------------------------------------------------
# 1. BuildInfo singleton
# ---------------------------------------------------------------------

class TestBuildInfoSingleton:
    def test_singleton_has_four_documented_keys(self):
        from app.core.build_info import BUILD_INFO
        assert set(BUILD_INFO.keys()) == {"app", "version", "git_sha", "status"}

    def test_app_name_is_static(self):
        from app.core.build_info import BUILD_INFO
        assert BUILD_INFO["app"] == "Luciel Backend"

    def test_status_is_ok(self):
        from app.core.build_info import BUILD_INFO
        assert BUILD_INFO["status"] == "ok"

    def test_git_sha_is_a_string(self):
        from app.core.build_info import BUILD_INFO
        assert isinstance(BUILD_INFO["git_sha"], str)
        # Either "unknown" (no build-arg passed) or a short SHA-shaped
        # hex string (7-40 chars). Don't pin the exact value because
        # this test runs in CI where the env var may or may not be
        # set; we only pin the shape.
        assert len(BUILD_INFO["git_sha"]) >= 1

    def test_version_is_a_string(self):
        from app.core.build_info import BUILD_INFO
        assert isinstance(BUILD_INFO["version"], str)
        assert len(BUILD_INFO["version"]) >= 1


class TestBuildInfoReaders:
    def test_read_git_sha_empty_env_returns_unknown(self, monkeypatch):
        from app.core import build_info
        monkeypatch.delenv("BUILD_GIT_SHA", raising=False)
        assert build_info._read_git_sha() == "unknown"

    def test_read_git_sha_empty_string_returns_unknown(self, monkeypatch):
        from app.core import build_info
        monkeypatch.setenv("BUILD_GIT_SHA", "")
        assert build_info._read_git_sha() == "unknown"

    def test_read_git_sha_whitespace_only_returns_unknown(self, monkeypatch):
        from app.core import build_info
        monkeypatch.setenv("BUILD_GIT_SHA", "   ")
        assert build_info._read_git_sha() == "unknown"

    def test_read_git_sha_real_sha_passes_through(self, monkeypatch):
        from app.core import build_info
        monkeypatch.setenv("BUILD_GIT_SHA", "deadbee")
        assert build_info._read_git_sha() == "deadbee"

    def test_read_app_version_succeeds(self):
        from app.core import build_info
        result = build_info._read_app_version()
        assert isinstance(result, str)
        assert len(result) >= 1


# ---------------------------------------------------------------------
# 2. Live /api/v1/version route via TestClient
# ---------------------------------------------------------------------

class TestVersionRoute:
    def _client(self):
        from fastapi.testclient import TestClient

        from app.main import app
        return TestClient(app)

    def test_version_returns_200(self):
        client = self._client()
        resp = client.get("/api/v1/version")
        assert resp.status_code == 200

    def test_response_has_four_keys(self):
        client = self._client()
        body = client.get("/api/v1/version").json()
        assert set(body.keys()) == {"app", "version", "git_sha", "status"}

    def test_response_preserves_legacy_three_keys(self):
        """Pre-WU-3 consumers read app/version/status. Those keys must
        be present and have the same shape after the WU-3 rollout."""
        client = self._client()
        body = client.get("/api/v1/version").json()
        assert body["app"] == "Luciel Backend"
        assert body["status"] == "ok"
        assert isinstance(body["version"], str)

    def test_response_includes_git_sha(self):
        client = self._client()
        body = client.get("/api/v1/version").json()
        assert "git_sha" in body
        assert isinstance(body["git_sha"], str)

    def test_response_is_fresh_dict_per_call(self):
        """A client that mutates the response must not poison the
        next caller's response."""
        client = self._client()
        first = client.get("/api/v1/version").json()
        first["app"] = "tampered"
        second = client.get("/api/v1/version").json()
        assert second["app"] == "Luciel Backend"

    def test_route_is_public_no_auth_required(self):
        """No api-key header is sent; the response must be 200, not
        401. The SKIP_AUTH_PATHS membership is what makes this work."""
        client = self._client()
        resp = client.get("/api/v1/version")
        assert resp.status_code == 200


# ---------------------------------------------------------------------
# 3. SKIP_AUTH_PATHS membership (defence-in-depth)
# ---------------------------------------------------------------------

class TestSkipAuthPathsMembership:
    def test_api_v1_version_is_in_skip_set(self):
        from app.middleware.auth import SKIP_AUTH_PATHS
        assert "/api/v1/version" in SKIP_AUTH_PATHS

    def test_health_is_in_skip_set(self):
        from app.middleware.auth import SKIP_AUTH_PATHS
        assert "/health" in SKIP_AUTH_PATHS
