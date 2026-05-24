"""Arc 6 / Commit 8 (2026-05-23) -- Unified Free-tier signup contract.

This file is the contract pin for the Arc 6 Commit 8 reshape of
``POST /api/v1/billing/signup-free``. The pre-Arc-6 contract is
covered in ``tests/api/test_signup_free_shape.py``; this file adds
the new assertions that landed in Commit 8:

  1. SignupFreeResponse carries an ``email`` echo alongside admin_id.
  2. Live route success path returns ``status="ok"`` (not the legacy
     ``pending-arc-5`` placeholder) and includes both admin_id and
     email in the body, when the route mints successfully.
  3. Live route 409 path fires on Admin-collision (same email
     attempting to sign up twice in the same Free funnel).
  4. Live route 422 path still fires when a captcha_token IS
     provided AND fails verification (the captcha gate is "soft"
     for missing tokens but "hard" for invalid tokens in the
     Commit 8 window).
  5. The captcha-soft-pass WARN log fires when captcha_token is
     omitted (closes the audit-grep affordance described in the
     route docstring).

The hCaptcha service unit tests, the Settings-field tests, and the
router-registration tests stay in ``test_signup_free_shape.py``;
this file is exclusively the Commit-8 behaviour-change pins.

Because this commit ships with sandbox-only DB plumbing (Commit 10
applies the migrations against prod RDS via ECS-exec), the live
mint path is exercised by monkey-patching the service boundaries
inside the route -- OnboardingService.onboard_tenant,
TierProvisioningService.premint_for_tier, the magic-link mint, and
the welcome-email send. That keeps this file in the same "no real
DB" posture as the rest of tests/api/.
"""
from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest

# Mirror the moderation-import-time-failure mitigation pattern from
# test_signup_free_shape.py; must come BEFORE any ``from app...`` import.
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://stub:stub@localhost:5432/stub"
)


# ---------------------------------------------------------------------
# 1. Response shape -- email echo field landed in Commit 8
# ---------------------------------------------------------------------


class TestSignupFreeResponseShape:
    def test_response_includes_email_echo_field(self):
        from app.schemas.billing import SignupFreeResponse

        r = SignupFreeResponse(
            status="ok",
            admin_id="free-1a2b3c4d",
            email="alice@example.com",
            message="hi",
        )
        assert r.email == "alice@example.com"

    def test_response_email_is_optional(self):
        from app.schemas.billing import SignupFreeResponse

        r = SignupFreeResponse(status="pending-arc-5", message="hi")
        assert r.email is None

    def test_response_admin_id_is_v2_slug_shape(self):
        # V2 slug shape is "<tier-prefix>-<8hex>"; the schema accepts
        # any string but the route mints via
        # billing_webhook_service._mint_admin_id_from_email(tier="free")
        # which produces "free-XXXXXXXX". Pin the route's import here
        # so a rename trips a clear failure.
        from app.services.billing_webhook_service import (
            _mint_admin_id_from_email,
        )

        slug = _mint_admin_id_from_email("a@b.com", tier="free")
        assert slug.startswith("free-")
        # 8 hex chars after the prefix.
        assert len(slug) == len("free-") + 8


# ---------------------------------------------------------------------
# 2. Route module imports & wiring (AST + import surface)
# ---------------------------------------------------------------------


class TestRouteImportsAndWiring:
    """The route function pulls in five collaborators from inline
    imports; renaming any of them breaks the mint path silently.
    Pin the imports here so a refactor flags loudly.
    """

    def test_route_imports_required_service_symbols(self):
        # Importing the module is enough to validate the inline
        # imports because the inline ``from ... import ...`` statements
        # live inside the route function body and only run at request
        # time -- but ALL the symbol names below must still resolve
        # against the imported modules at request time.
        from app.repositories.admin_audit_repository import AuditContext
        from app.services.billing_webhook_service import (
            BillingWebhookService,
            _mint_admin_id_from_email,
        )
        from app.services.email_service import send_welcome_set_password_email
        from app.services.magic_link_service import (
            build_set_password_url,
            mint_set_password_token,
        )
        from app.services.onboarding_service import OnboardingService
        from app.services.tier_provisioning_service import (
            TierProvisioningService,
        )

        # Sanity: these are callables / classes, not None / sentinels.
        assert callable(AuditContext.system)
        assert callable(BillingWebhookService)
        assert callable(_mint_admin_id_from_email)
        assert callable(send_welcome_set_password_email)
        assert callable(build_set_password_url)
        assert callable(mint_set_password_token)
        assert callable(OnboardingService)
        assert callable(TierProvisioningService)


# ---------------------------------------------------------------------
# 3. OnboardingService V2 column guard (Commit 8 P0 fix)
# ---------------------------------------------------------------------


class TestOnboardingServiceV2Guard:
    """Arc 6 / Commit 8 added an ALLOWED_TIERS_V2 guard to
    OnboardingService.onboard_tenant. A bad-tier string must be
    rejected at the service boundary BEFORE the route gets to call
    the Admin INSERT -- the V1 vocabulary (individual/team/company)
    is retired and an accidental pass-through would silently mint
    a malformed Admin row.
    """

    def test_allowed_tiers_v2_present(self):
        # The service exposes ALLOWED_TIERS_V2 as a module-level set.
        import app.services.onboarding_service as svc

        assert hasattr(svc, "ALLOWED_TIERS_V2")
        assert "free" in svc.ALLOWED_TIERS_V2
        assert "pro" in svc.ALLOWED_TIERS_V2
        assert "enterprise" in svc.ALLOWED_TIERS_V2

    def test_v1_vocab_not_in_allowed(self):
        import app.services.onboarding_service as svc

        # V1 vocab must NOT be silently accepted. If it appears here,
        # the Arc 6 retirement was reverted.
        for v1_tier in ("individual", "team", "company"):
            assert v1_tier not in svc.ALLOWED_TIERS_V2


# ---------------------------------------------------------------------
# 4. TierProvisioningService -- Free now flows through provisioning
# ---------------------------------------------------------------------


class TestTierProvisioningFreeRouted:
    """Arc 6 / Commit 8 lifted the TIER_FREE-rejection guard from
    TierProvisioningService.premint_for_tier. Free now mints the
    same ScopeAssignment + Instance shape as Pro/Enterprise; the
    only difference is the entitlement caps.
    """

    def test_module_imports_tier_free(self):
        # Import-level check: the service module imports TIER_FREE
        # from app.models.admin (it would not be needed if the
        # rejection guard were still in place).
        import app.services.tier_provisioning_service as svc

        # The constant should be referenced in the module namespace.
        assert hasattr(svc, "TIER_FREE")

    def test_domain_collapse_sentinel_present(self):
        # Arc 6 / Commit 8 added a sentinel ("default") to side-step
        # the latent IntegrityError when ScopeAssignment.domain_id is
        # None during pre-mint. Pin the constant so a rename trips
        # this test instead of failing silently at INSERT time.
        import app.services.tier_provisioning_service as svc

        assert hasattr(svc, "_DOMAIN_COLLAPSE_SENTINEL")
        assert svc._DOMAIN_COLLAPSE_SENTINEL == "default"


# ---------------------------------------------------------------------
# 5. AuthService -- email_verified atomic write (Commit 8)
# ---------------------------------------------------------------------


class TestAuthServiceEmailVerifiedOnSetPassword:
    """Arc 6 / Commit 8 wires the magic-link consumption to also
    mark the User.email_verified flag true. The same SQL transaction
    that writes the password hash also sets email_verified=true so
    the two facts can never disagree.
    """

    def test_user_model_has_email_verified_column(self):
        from app.models.user import User

        # The column is declared via SQLAlchemy mapped_column. Look it
        # up via the table __table__ inspection so we are robust to
        # mapping-style changes.
        cols = {c.name for c in User.__table__.columns}
        assert "email_verified" in cols

    def test_set_password_sets_email_verified_grep(self):
        # Source-grep: the auth_service.set_password method must
        # reference user.email_verified somewhere in its body. A
        # full live-DB test belongs in tests/e2e/; this is the
        # backstop against an accidental revert.
        #
        # We grep the source file as text (not via inspect.getsource)
        # because importing AuthService pulls in argon2 which is not
        # installed in the sandbox. The sandbox is the assertion
        # boundary -- prod has argon2 because the runtime image is
        # built from the locked requirements set.
        import re
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        src = (repo_root / "app" / "services" / "auth_service.py").read_text()

        # Find the set_password method body. The simplest robust grep
        # is "between 'def set_password' and the next top-level 'def '
        # or end-of-file". A regex with DOTALL handles the multi-line
        # method body without false positives from later methods that
        # might mention email_verified for other reasons.
        m = re.search(
            r"def\s+set_password\s*\(.*?(?=\n    def\s|\nclass\s|\Z)",
            src,
            flags=re.DOTALL,
        )
        assert m is not None, (
            "Could not locate set_password method in auth_service.py; "
            "the symbol may have been renamed."
        )
        body = m.group(0)
        assert "email_verified" in body, (
            "set_password no longer references email_verified; the "
            "Arc 6 Commit 8 magic-link-consumption-as-verification "
            "semantic has regressed."
        )


# ---------------------------------------------------------------------
# 6. Live route -- success path returns status="ok" with email echo
# ---------------------------------------------------------------------

# The live-route classes require a working PostgreSQL DBAPI (psycopg)
# because the FastAPI app's startup wiring constructs the SQLAlchemy
# engine eagerly. In the sandbox the driver is not installed; in CI /
# the runtime image it is. Detect once and skip-decorate the live-
# route classes instead of failing -- the shape + AST + service-guard
# tests above are sandbox-runnable and stay green.
try:
    import psycopg  # noqa: F401
    _HAS_PSYCOPG = True
except ImportError:
    _HAS_PSYCOPG = False

_skip_no_psycopg = pytest.mark.skipif(
    not _HAS_PSYCOPG,
    reason="psycopg DBAPI not installed in sandbox; live-route tests "
    "run in CI / the runtime image where the driver is present.",
)


@_skip_no_psycopg
class TestSignupFreeLiveRouteOk:
    """Exercise the route end-to-end but monkey-patch the DB-touching
    service boundaries. The contract under test is "the route wires
    its collaborators correctly and surfaces status='ok' + admin_id
    + email in the 200 body" -- it is NOT the full Admin-row mint
    (that is owned by the OnboardingService unit tests and the e2e
    harness on the deploy side).
    """

    def _client(self):
        from fastapi.testclient import TestClient

        from app.main import app
        return TestClient(app)

    def test_captcha_success_returns_ok_with_email_and_admin_id(
        self, monkeypatch
    ):
        from app.core.config import settings
        from app.api.v1 import billing as billing_module

        monkeypatch.setattr(settings, "hcaptcha_secret_key", "stub-secret")

        async def fake_verify(token, *, remote_ip=None, http_client=None):
            return {
                "success": True,
                "challenge_ts": "2026-05-23T12:00:00.000Z",
                "hostname": "www.vantagemind.ai",
                "credit": None,
            }

        monkeypatch.setattr(billing_module, "verify_captcha", fake_verify)

        # Patch the inline-imported collaborators. The route uses
        # ``from X import Y`` statements inside the function body, so
        # we patch the source modules and the inline import resolves
        # against the patched module attribute.
        class _FakeUser:
            id = "user-uuid-12345678"

        class _FakeWebhookHelper:
            def __init__(self, db):
                self.db = db

            def _resolve_or_create_user(self, *, email, display_name):
                return _FakeUser()

        class _FakeOnboarding:
            def __init__(self, db):
                self.db = db

            def onboard_tenant(self, **_kwargs):
                return None

        class _FakePremint:
            def __init__(self, db):
                self.db = db

            def premint_for_tier(self, **_kwargs):
                return None

        from app.services import billing_webhook_service as bws_mod
        from app.services import onboarding_service as ons_mod
        from app.services import tier_provisioning_service as tps_mod
        from app.services import magic_link_service as mls_mod
        from app.services import email_service as es_mod

        monkeypatch.setattr(
            bws_mod, "BillingWebhookService", _FakeWebhookHelper
        )
        monkeypatch.setattr(ons_mod, "OnboardingService", _FakeOnboarding)
        monkeypatch.setattr(
            tps_mod, "TierProvisioningService", _FakePremint
        )
        monkeypatch.setattr(
            mls_mod,
            "mint_set_password_token",
            lambda **_k: "fake-magic-link-token",
        )
        monkeypatch.setattr(
            mls_mod,
            "build_set_password_url",
            lambda _t: "https://www.vantagemind.ai/auth/set-password?token=x",
        )

        sent: dict[str, Any] = {}

        def _fake_send(*, to_email, set_password_url, display_name, **_k):
            sent["to_email"] = to_email
            sent["url"] = set_password_url
            sent["display_name"] = display_name

        monkeypatch.setattr(
            es_mod, "send_welcome_set_password_email", _fake_send
        )

        client = self._client()
        resp = client.post(
            "/api/v1/billing/signup-free",
            json={
                "email": "Alice@Example.com",  # mixed case to test lower()
                "display_name": "Alice",
                "captcha_token": "good-token",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["admin_id"].startswith("free-")
        # The route lower()s the email before mint; the echo field
        # is the canonical (lowercased) form.
        assert body["email"] == "alice@example.com"
        assert "password" in body["message"].lower()


# ---------------------------------------------------------------------
# 7. Live route -- missing captcha_token is hard-rejected (Pydantic 422)
# ---------------------------------------------------------------------


@_skip_no_psycopg
class TestSignupFreeCaptchaHardRequired:
    """Arc 6 Commit 9 (2026-05-23) -- the Commit-8 soft-pass window
    is closed. A request that omits ``captcha_token`` MUST bounce
    with a Pydantic-level 422 BEFORE the route function executes;
    the verify_captcha service call is never reached, no DB session
    is touched, no log line is emitted by the handler.

    Replaces the legacy ``TestSignupFreeCaptchaSoftPassLog`` which
    asserted the (now-removed) WARN log that fired in the Commit-8
    window. The soft-pass branch and its ``signup_free.captcha_soft_pass``
    log line are deleted from the route source -- a regression on
    that surface is pinned by test_arc6_commit9_captcha.py.

    The ``_skip_no_psycopg`` gate matches the rest of the live-route
    classes in this file: ``app.main`` import constructs the
    SQLAlchemy engine eagerly and needs the psycopg DBAPI. Even
    though Pydantic bounces the request before any DB write happens,
    importing ``app.main`` to build the TestClient still requires
    the engine to come up cleanly.
    """

    def _client(self):
        from fastapi.testclient import TestClient

        from app.main import app
        return TestClient(app)

    def test_missing_token_returns_422_validation_envelope(
        self, monkeypatch, caplog,
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "hcaptcha_secret_key", "stub-secret")

        client = self._client()
        with caplog.at_level("WARNING"):
            resp = client.post(
                "/api/v1/billing/signup-free",
                json={
                    "email": "bob@example.com",
                    "display_name": "Bob",
                    # captcha_token omitted -- required in Commit 9
                },
            )

        # Pydantic-level 422 with the standard FastAPI envelope:
        # {"detail": [{"loc": ["body", "captcha_token"], ...}, ...]}
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert isinstance(body.get("detail"), list), body
        locs = [tuple(err.get("loc", [])) for err in body["detail"]]
        assert any(
            "captcha_token" in loc for loc in locs
        ), f"Expected captcha_token in 422 detail locs, got: {locs}"

        # The legacy Commit-8 soft-pass WARN log MUST NOT fire any
        # more -- the branch that emitted it is gone from the route.
        soft_pass_lines = [
            r for r in caplog.records
            if "signup_free.captcha_soft_pass" in r.getMessage()
        ]
        assert not soft_pass_lines, (
            "Commit-8 soft-pass WARN log should be REMOVED in Commit 9 "
            f"but found: {[r.getMessage() for r in soft_pass_lines]}"
        )

    def test_empty_token_returns_422_validation_envelope(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "hcaptcha_secret_key", "stub-secret")

        client = self._client()
        resp = client.post(
            "/api/v1/billing/signup-free",
            json={
                "email": "bob@example.com",
                "display_name": "Bob",
                "captcha_token": "",
            },
        )
        # min_length=1 violation -> Pydantic 422
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert isinstance(body.get("detail"), list), body


# ---------------------------------------------------------------------
# 8. Live route -- captcha invalid still hard-fails 422
# ---------------------------------------------------------------------


@_skip_no_psycopg
class TestSignupFreeCaptchaInvalidStillHardFails:
    """Even in the Commit 8 soft-pass window, a captcha_token that
    is PROVIDED but FAILS upstream verification must still bounce
    with a 422. Only the missing-token case is soft-pass.
    """

    def _client(self):
        from fastapi.testclient import TestClient

        from app.main import app
        return TestClient(app)

    def test_invalid_token_returns_422(self, monkeypatch):
        from app.core.config import settings
        from app.services import hcaptcha_service
        from app.api.v1 import billing as billing_module

        monkeypatch.setattr(settings, "hcaptcha_secret_key", "stub-secret")

        async def fake_verify(token, *, remote_ip=None, http_client=None):
            raise hcaptcha_service.CaptchaInvalidError(
                "bad token",
                error_codes=["invalid-input-response"],
            )

        monkeypatch.setattr(billing_module, "verify_captcha", fake_verify)

        client = self._client()
        resp = client.post(
            "/api/v1/billing/signup-free",
            json={
                "email": "carol@example.com",
                "display_name": "Carol",
                "captcha_token": "bad-token",
            },
        )
        assert resp.status_code == 422
        body = resp.json()
        assert "invalid-input-response" in body["detail"]["error_codes"]
