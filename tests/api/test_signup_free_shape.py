"""Arc 8 Work-Unit 5 -- Free-tier signup contract tests.

D-free-tier-captcha-missing-2026-05-22 resolution. The Free-tier
self-serve signup endpoint at ``POST /api/v1/billing/signup-free`` is
the new public surface that mints Free-tier Admin rows. Without a bot
gate it would be a SES-quota drain and a DB-row drain; hCaptcha is the
gate. This file pins the contract:

  * Route is registered on ``app.api.v1.billing.router`` at the right
    HTTP verb + path.
  * Request schema requires ``email``, ``display_name``, and
    ``captcha_token``.
  * Response schema carries the documented Literal status, optional
    admin_id, and message.
  * hCaptcha service raises ``CaptchaNotConfiguredError`` when the
    secret slot is empty (boot-safe -> route 501).
  * hCaptcha service raises ``CaptchaInvalidError`` on empty/blank
    tokens (no network call) and on upstream success=false
    (with the upstream error-codes list captured).
  * Hitting the live route with a missing/empty hCaptcha secret
    returns HTTP 501, NOT HTTP 500.
  * Hitting the live route with a stubbed-OK hCaptcha verify returns
    HTTP 200 with ``status="pending-arc-5"`` (admins table not yet
    minted -- Arc 5 owns that).
  * Hitting the live route with a stubbed-fail hCaptcha verify
    returns HTTP 422 with the upstream error-codes list in the body.

End-to-end correctness (real hCaptcha round-trip, real Admin mint
after Arc 5 lands) is covered by tests/e2e/. This file is the
surface-shape pin and the unit-level captcha gate test.
"""
from __future__ import annotations

import os

import httpx
import pytest

# Match the moderation-import-time-failure mitigation pattern from
# tests/api/test_step30a_billing_shape.py header (see drift
# D-step-30a-billing-shape-test-moderation-config-failure-2026-05-13
# resolution). Must come BEFORE any ``from app...`` import.
os.environ.setdefault("MODERATION_PROVIDER", "null")
# DATABASE_URL is referenced at Settings() construction time even
# though the tests below never touch a DB session. A non-empty stub
# is enough -- no driver is loaded.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://stub:stub@localhost:5432/stub"
)


# ---------------------------------------------------------------------
# 1. Settings field exists with the documented default
# ---------------------------------------------------------------------

class TestSettings:
    def test_hcaptcha_secret_key_field_exists_and_defaults_empty(self):
        from app.core.config import Settings
        s = Settings(database_url="postgresql+psycopg://x:x@h:5432/x")
        assert s.hcaptcha_secret_key == ""

    def test_hcaptcha_verify_url_default_is_public_endpoint(self):
        from app.core.config import Settings
        s = Settings(database_url="postgresql+psycopg://x:x@h:5432/x")
        assert s.hcaptcha_verify_url == "https://api.hcaptcha.com/siteverify"

    def test_hcaptcha_site_key_field_exists_and_defaults_empty(self):
        from app.core.config import Settings
        s = Settings(database_url="postgresql+psycopg://x:x@h:5432/x")
        assert s.hcaptcha_site_key == ""


# ---------------------------------------------------------------------
# 2. Pydantic schemas
# ---------------------------------------------------------------------

class TestSignupFreeSchemas:
    def test_request_requires_three_fields(self):
        from app.schemas.billing import SignupFreeRequest
        req = SignupFreeRequest(
            email="a@b.com",
            display_name="Test User",
            captcha_token="x" * 20,
        )
        assert req.email == "a@b.com"
        assert req.display_name == "Test User"
        assert req.captcha_token == "x" * 20

    def test_request_rejects_missing_captcha_token(self):
        from pydantic import ValidationError

        from app.schemas.billing import SignupFreeRequest
        with pytest.raises(ValidationError):
            SignupFreeRequest(email="a@b.com", display_name="X")  # type: ignore[call-arg]

    def test_request_rejects_empty_captcha_token(self):
        from pydantic import ValidationError

        from app.schemas.billing import SignupFreeRequest
        with pytest.raises(ValidationError):
            SignupFreeRequest(
                email="a@b.com",
                display_name="X",
                captcha_token="",
            )

    def test_request_rejects_bad_email_shape(self):
        from pydantic import ValidationError

        from app.schemas.billing import SignupFreeRequest
        with pytest.raises(ValidationError):
            SignupFreeRequest(
                email="not-an-email",
                display_name="X",
                captcha_token="x" * 20,
            )

    def test_response_status_is_literal(self):
        from app.schemas.billing import SignupFreeResponse
        ok = SignupFreeResponse(
            status="ok",
            admin_id="00000000-0000-0000-0000-000000000000",
            message="hi",
        )
        pending = SignupFreeResponse(
            status="pending-arc-5",
            admin_id=None,
            message="hi",
        )
        assert ok.status == "ok"
        assert pending.status == "pending-arc-5"

    def test_response_rejects_unknown_status(self):
        from pydantic import ValidationError

        from app.schemas.billing import SignupFreeResponse
        with pytest.raises(ValidationError):
            SignupFreeResponse(status="bogus", message="hi")  # type: ignore[arg-type]


# ---------------------------------------------------------------------
# 3. hCaptcha service unit tests
# ---------------------------------------------------------------------

class TestHCaptchaServiceUnit:
    @pytest.mark.asyncio
    async def test_empty_secret_raises_not_configured(self, monkeypatch):
        from app.core.config import settings
        from app.services.hcaptcha_service import (
            CaptchaNotConfiguredError,
            verify_captcha,
        )

        monkeypatch.setattr(settings, "hcaptcha_secret_key", "")
        with pytest.raises(CaptchaNotConfiguredError):
            await verify_captcha("anything")

    @pytest.mark.asyncio
    async def test_empty_token_raises_invalid_no_network(self, monkeypatch):
        from app.core.config import settings
        from app.services.hcaptcha_service import (
            CaptchaInvalidError,
            verify_captcha,
        )

        monkeypatch.setattr(settings, "hcaptcha_secret_key", "stub-secret")

        # No HTTP client passed; if the service made a network call
        # the test would fail because httpx would try to resolve a
        # real host. The pre-network token check should catch it
        # first.
        with pytest.raises(CaptchaInvalidError) as exc_info:
            await verify_captcha("   ")
        assert "missing-input-response" in exc_info.value.error_codes

    @pytest.mark.asyncio
    async def test_upstream_failure_raises_invalid(self, monkeypatch):
        from app.core.config import settings
        from app.services.hcaptcha_service import (
            CaptchaInvalidError,
            verify_captcha,
        )

        monkeypatch.setattr(settings, "hcaptcha_secret_key", "stub-secret")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "success": False,
                    "error-codes": ["invalid-input-response"],
                },
            )

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)

        with pytest.raises(CaptchaInvalidError) as exc_info:
            await verify_captcha("bad-token", http_client=client)
        assert "invalid-input-response" in exc_info.value.error_codes

    @pytest.mark.asyncio
    async def test_upstream_success_returns_metadata(self, monkeypatch):
        from app.core.config import settings
        from app.services.hcaptcha_service import verify_captcha

        monkeypatch.setattr(settings, "hcaptcha_secret_key", "stub-secret")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "challenge_ts": "2026-05-22T15:07:00.000Z",
                    "hostname": "www.vantagemind.ai",
                },
            )

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)

        result = await verify_captcha("good-token", http_client=client)
        assert result["success"] is True
        assert result["hostname"] == "www.vantagemind.ai"
        assert result["challenge_ts"] == "2026-05-22T15:07:00.000Z"

    @pytest.mark.asyncio
    async def test_upstream_non_200_raises_invalid(self, monkeypatch):
        from app.core.config import settings
        from app.services.hcaptcha_service import (
            CaptchaInvalidError,
            verify_captcha,
        )

        monkeypatch.setattr(settings, "hcaptcha_secret_key", "stub-secret")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="Service Unavailable")

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)

        with pytest.raises(CaptchaInvalidError) as exc_info:
            await verify_captcha("any-token", http_client=client)
        assert any(
            "upstream-503" in code for code in exc_info.value.error_codes
        )


# ---------------------------------------------------------------------
# 4. Router registration
# ---------------------------------------------------------------------

class TestRouterRegistration:
    def test_signup_free_route_registered(self):
        from app.api.v1.billing import router
        paths = {
            (route.path, tuple(sorted(route.methods)))
            for route in router.routes
            if hasattr(route, "methods")
        }
        assert ("/billing/signup-free", ("POST",)) in paths


# ---------------------------------------------------------------------
# 5. Live route behaviour (via FastAPI TestClient)
# ---------------------------------------------------------------------

class TestSignupFreeRoute:
    def _client(self):
        # Lazy import so the module-level env vars are honored.
        from fastapi.testclient import TestClient

        from app.main import app
        return TestClient(app)

    def test_unconfigured_captcha_returns_501(self, monkeypatch):
        from app.core.config import settings
        monkeypatch.setattr(settings, "hcaptcha_secret_key", "")

        client = self._client()
        resp = client.post(
            "/api/v1/billing/signup-free",
            json={
                "email": "alice@example.com",
                "display_name": "Alice",
                "captcha_token": "stub-token",
            },
        )
        assert resp.status_code == 501

    def test_missing_token_returns_422_pydantic(self, monkeypatch):
        from app.core.config import settings
        monkeypatch.setattr(settings, "hcaptcha_secret_key", "stub-secret")

        client = self._client()
        resp = client.post(
            "/api/v1/billing/signup-free",
            json={
                "email": "alice@example.com",
                "display_name": "Alice",
                # captcha_token omitted
            },
        )
        assert resp.status_code == 422

    def test_captcha_failure_returns_422_with_error_codes(self, monkeypatch):
        from app.core.config import settings
        from app.services import hcaptcha_service

        monkeypatch.setattr(settings, "hcaptcha_secret_key", "stub-secret")

        async def fake_verify(token, *, remote_ip=None, http_client=None):
            raise hcaptcha_service.CaptchaInvalidError(
                "Captcha verification failed.",
                error_codes=["invalid-input-response"],
            )

        # Patch the symbol on the billing module (route imported via
        # ``from app.services.hcaptcha_service import verify_captcha``).
        from app.api.v1 import billing as billing_module
        monkeypatch.setattr(billing_module, "verify_captcha", fake_verify)

        client = self._client()
        resp = client.post(
            "/api/v1/billing/signup-free",
            json={
                "email": "alice@example.com",
                "display_name": "Alice",
                "captcha_token": "bad-token",
            },
        )
        assert resp.status_code == 422
        body = resp.json()
        assert "error_codes" in body["detail"]
        assert "invalid-input-response" in body["detail"]["error_codes"]

    def test_captcha_success_returns_200_pending_arc_5(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "hcaptcha_secret_key", "stub-secret")

        async def fake_verify(token, *, remote_ip=None, http_client=None):
            return {
                "success": True,
                "challenge_ts": "2026-05-22T15:07:00.000Z",
                "hostname": "www.vantagemind.ai",
                "credit": None,
            }

        from app.api.v1 import billing as billing_module
        monkeypatch.setattr(billing_module, "verify_captcha", fake_verify)

        client = self._client()
        resp = client.post(
            "/api/v1/billing/signup-free",
            json={
                "email": "alice@example.com",
                "display_name": "Alice",
                "captcha_token": "good-token",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "pending-arc-5"
        assert body["admin_id"] is None
        assert "message" in body
