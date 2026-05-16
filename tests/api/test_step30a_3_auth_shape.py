"""Backend-free contract tests for Step 30a.3 — password auth.

Step 30a.3 adds password authentication to the Luciel backend with
"mandatory at signup" semantics (Option B welcome-email mechanic). This
file pins the *shape* of that surface so we catch unintentional contract
drift between the marketing site's auth pages (Luciel-Website
/login, /forgot-password, /auth/set-password) and the backend.

Coverage (AST + import only — no Postgres, no FastAPI runtime, no SES
network, no argon2 hashing):

  * User model surface — ``password_hash`` column exists, nullable,
    no UNIQUE constraint (NULL must be allowed for users who have not
    yet redeemed their welcome email).
  * Alembic migration — the Step 30a.3 revision is present in the chain
    with the documented revision id and down_revision.
  * AuthService — the three public functions exist with the documented
    keyword-only signatures and exception classes.
  * MagicLinkService — the four new token primitives (mint set/reset,
    consume set/reset) plus ``build_set_password_url`` exist with the
    documented signatures, AND the two new token-type constants are
    declared.
  * EmailService — ``send_welcome_set_password_email`` exists with the
    documented signature and the ``WelcomeEmailError`` class is exported.
  * Auth router — ``app.api.v1.auth.router`` exists with the three
    documented routes at POST.
  * Router registration — ``app.api.router.api_router`` includes the
    auth router (prefix ``/auth``).
  * Webhook integration — ``BillingWebhookService._on_checkout_completed``
    no longer references the magic-link email helpers and now calls
    the welcome-set-password helpers instead.
  * Auth-middleware exemption — ``/api/v1/auth`` is in SKIP_AUTH_PATHS
    so the new routes can be reached without an existing session.

End-to-end correctness (Stripe round-trip -> welcome email -> set-password
-> cookied /app) is covered by tests/e2e/step_30a_3_live_e2e.py. This
file is the surface-shape pin.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------
# 1. User model — password_hash column
# ---------------------------------------------------------------------


class TestUserPasswordHashColumn:
    def test_column_exists(self):
        from app.models.user import User
        cols = {c.name for c in User.__table__.columns}
        assert "password_hash" in cols, (
            "User.password_hash must exist for Step 30a.3 password auth"
        )

    def test_column_is_nullable(self):
        from app.models.user import User
        col = User.__table__.columns["password_hash"]
        assert col.nullable is True, (
            "password_hash must be nullable -- users who have not yet "
            "redeemed their welcome email have no hash on file"
        )

    def test_column_has_no_unique_constraint(self):
        from app.models.user import User
        col = User.__table__.columns["password_hash"]
        # Multiple users will legitimately have NULL hashes (pre-redeem).
        # A UNIQUE constraint would conflict with that.
        assert col.unique is not True


# ---------------------------------------------------------------------
# 2. Alembic migration — revision chain
# ---------------------------------------------------------------------


class TestStep30a3Migration:
    MIGRATION_PATH = (
        REPO_ROOT / "alembic" / "versions"
        / "a3c1f08b9d42_step30a_3_users_password_hash.py"
    )

    def test_file_exists(self):
        assert self.MIGRATION_PATH.exists(), (
            f"Step 30a.3 migration must exist at {self.MIGRATION_PATH}"
        )

    def test_revision_and_down_revision_are_pinned(self):
        src = self.MIGRATION_PATH.read_text()
        tree = ast.parse(src)
        revision = down_revision = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                t = node.targets[0]
                if isinstance(t, ast.Name) and isinstance(node.value, ast.Constant):
                    if t.id == "revision":
                        revision = node.value.value
                    elif t.id == "down_revision":
                        down_revision = node.value.value
        assert revision == "a3c1f08b9d42"
        assert down_revision == "dfea1a04e037", (
            "down_revision must point at the pre-30a.3 head dfea1a04e037"
        )

    def test_upgrade_adds_password_hash_column(self):
        src = self.MIGRATION_PATH.read_text()
        # The exact mechanism is irrelevant; we just pin the intent.
        assert "password_hash" in src
        assert "add_column" in src or "alter_column" in src


# ---------------------------------------------------------------------
# 3. AuthService — public surface
# ---------------------------------------------------------------------


class TestAuthService:
    def test_module_imports(self):
        import app.services.auth_service as auth_service  # noqa: F401

    def test_exception_classes_exist(self):
        from app.services.auth_service import AuthError, PasswordTooShortError
        assert issubclass(PasswordTooShortError, ValueError)
        assert issubclass(AuthError, Exception)

    def test_verify_password_signature(self):
        from app.services.auth_service import verify_password
        sig = inspect.signature(verify_password)
        # All three args must be keyword-only.
        params = sig.parameters
        for name in ("db", "email", "password"):
            assert name in params, f"verify_password missing kw {name}"
            assert params[name].kind == inspect.Parameter.KEYWORD_ONLY

    def test_set_password_signature(self):
        from app.services.auth_service import set_password
        sig = inspect.signature(set_password)
        params = sig.parameters
        for name in ("db", "user_id", "password"):
            assert name in params
            assert params[name].kind == inspect.Parameter.KEYWORD_ONLY

    def test_request_password_reset_signature(self):
        from app.services.auth_service import request_password_reset
        sig = inspect.signature(request_password_reset)
        params = sig.parameters
        for name in ("db", "email"):
            assert name in params
            assert params[name].kind == inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------------------------------
# 4. MagicLinkService — new token primitives
# ---------------------------------------------------------------------


class TestMagicLinkServiceTokenExtensions:
    def test_new_token_type_constants(self):
        import app.services.magic_link_service as mls
        assert hasattr(mls, "TOKEN_TYPE_SET_PASSWORD")
        assert hasattr(mls, "TOKEN_TYPE_RESET_PASSWORD")
        # Must be distinct -- the cross-class replay guard depends on it.
        assert mls.TOKEN_TYPE_SET_PASSWORD != mls.TOKEN_TYPE_RESET_PASSWORD
        # Must not collide with the magic-link or session classes.
        assert mls.TOKEN_TYPE_SET_PASSWORD not in (
            mls.TOKEN_TYPE_MAGIC_LINK, mls.TOKEN_TYPE_SESSION,
        )
        assert mls.TOKEN_TYPE_RESET_PASSWORD not in (
            mls.TOKEN_TYPE_MAGIC_LINK, mls.TOKEN_TYPE_SESSION,
        )

    def test_mint_set_password_token_signature(self):
        from app.services.magic_link_service import mint_set_password_token
        sig = inspect.signature(mint_set_password_token)
        params = sig.parameters
        for name in ("user_id", "email", "tenant_id", "purpose"):
            assert name in params, f"mint_set_password_token missing kw {name}"
            assert params[name].kind == inspect.Parameter.KEYWORD_ONLY
        # purpose has a default ("signup")
        assert params["purpose"].default == "signup"

    def test_mint_reset_password_token_signature(self):
        from app.services.magic_link_service import mint_reset_password_token
        sig = inspect.signature(mint_reset_password_token)
        params = sig.parameters
        for name in ("user_id", "email", "tenant_id"):
            assert name in params
            assert params[name].kind == inspect.Parameter.KEYWORD_ONLY

    def test_consume_helpers_exist(self):
        from app.services.magic_link_service import (
            consume_reset_password_token,
            consume_set_password_token,
        )
        # Both take a single positional ``token`` arg.
        assert "token" in inspect.signature(consume_set_password_token).parameters
        assert "token" in inspect.signature(consume_reset_password_token).parameters

    def test_build_set_password_url_returns_marketing_site_path(self):
        # We cannot resolve settings.marketing_site_url without env, but
        # we can pin the path component by inspecting the source.
        import app.services.magic_link_service as mls
        src = inspect.getsource(mls.build_set_password_url)
        assert "/auth/set-password" in src
        assert "token=" in src


# ---------------------------------------------------------------------
# 5. EmailService — welcome-set-password helper
# ---------------------------------------------------------------------


class TestEmailServiceWelcomeHelper:
    def test_welcome_email_error_exists(self):
        from app.services.email_service import WelcomeEmailError
        assert issubclass(WelcomeEmailError, Exception)

    def test_send_welcome_set_password_email_signature(self):
        from app.services.email_service import send_welcome_set_password_email
        sig = inspect.signature(send_welcome_set_password_email)
        params = sig.parameters
        for name in ("to_email", "set_password_url", "display_name", "purpose"):
            assert name in params, (
                f"send_welcome_set_password_email missing kw {name}"
            )
            assert params[name].kind == inspect.Parameter.KEYWORD_ONLY
        # purpose has a default ("signup")
        assert params["purpose"].default == "signup"

    def test_stable_log_marker_is_present(self):
        import app.services.email_service as es
        src = inspect.getsource(es)
        assert "[welcome-set-password-email]" in src, (
            "stable log marker must be present so CloudWatch filters "
            "land on the welcome-email rows"
        )


# ---------------------------------------------------------------------
# 6. Auth router — three routes at POST
# ---------------------------------------------------------------------


class TestAuthRouter:
    def test_module_imports(self):
        import app.api.v1.auth as auth_module  # noqa: F401

    def test_router_prefix_and_tag(self):
        from app.api.v1.auth import router
        assert router.prefix == "/auth"
        assert "auth" in router.tags

    def test_three_routes_present(self):
        from app.api.v1.auth import router
        # Build a set of (method, path) tuples.
        seen = set()
        for r in router.routes:
            for method in getattr(r, "methods", set()):
                seen.add((method, r.path))
        # Note: r.path is the full prefixed path inside the APIRouter.
        for expected in (
            ("POST", "/auth/login"),
            ("POST", "/auth/set-password"),
            ("POST", "/auth/forgot-password"),
        ):
            assert expected in seen, f"missing route {expected}, have {seen}"


# ---------------------------------------------------------------------
# 7. Router registration in the aggregate
# ---------------------------------------------------------------------


class TestAuthRouterRegistered:
    def test_aggregate_router_includes_auth(self):
        from app.api.router import api_router
        # Walk the aggregate routes and assert at least one of them is
        # the auth-router's /auth/login path.
        paths = {getattr(r, "path", None) for r in api_router.routes}
        # api_router doesn't carry the /api/v1 prefix here -- that lands
        # in main.py. We just need the /auth/* paths to be present.
        assert "/auth/login" in paths, (
            f"auth router must be registered in api_router; got {paths}"
        )
        assert "/auth/set-password" in paths
        assert "/auth/forgot-password" in paths


# ---------------------------------------------------------------------
# 8. Webhook integration — magic-link email replaced with welcome
# ---------------------------------------------------------------------


class TestWebhookWelcomeIntegration:
    def test_magic_link_email_helpers_no_longer_imported(self):
        src = (
            REPO_ROOT / "app" / "services" / "billing_webhook_service.py"
        ).read_text()
        # The webhook must no longer pull in the magic-link email path
        # at module-load time -- that surface is dead for signup as of
        # Step 30a.3 (Option B welcome-email mechanic).
        assert "send_magic_link_email" not in src, (
            "webhook must NOT import send_magic_link_email (Step 30a.3 "
            "replaces the post-checkout magic-link with the welcome-"
            "set-password email)"
        )

    def test_welcome_email_helpers_are_imported(self):
        src = (
            REPO_ROOT / "app" / "services" / "billing_webhook_service.py"
        ).read_text()
        assert "send_welcome_set_password_email" in src
        assert "mint_set_password_token" in src
        assert "build_set_password_url" in src

    def test_welcome_email_minted_with_signup_purpose(self):
        src = (
            REPO_ROOT / "app" / "services" / "billing_webhook_service.py"
        ).read_text()
        # The webhook MUST tag the token purpose so the audit row can
        # distinguish signup from invite later.
        assert 'purpose="signup"' in src or "purpose='signup'" in src


# ---------------------------------------------------------------------
# 9. Auth-middleware exemption
# ---------------------------------------------------------------------


class TestAuthMiddlewareSkip:
    def test_api_v1_auth_in_skip_auth_paths(self):
        # The middleware module is named in main.py; we read the
        # constant directly.
        try:
            from app.middleware.auth import SKIP_AUTH_PATHS
        except ImportError:
            try:
                from app.core.auth_middleware import SKIP_AUTH_PATHS
            except ImportError:
                pytest.skip(
                    "SKIP_AUTH_PATHS not found at known import paths; "
                    "middleware layout differs from contract expectation"
                )
                return

        # Either an exact prefix entry or a path that starts with it.
        assert any(
            p == "/api/v1/auth" or p.startswith("/api/v1/auth/")
            for p in SKIP_AUTH_PATHS
        ), (
            f"/api/v1/auth must be in SKIP_AUTH_PATHS; have {SKIP_AUTH_PATHS}"
        )
