"""Arc 17 — OAuth initiate + callback route wiring + state-signing (shape).

Two layers, both running WITHOUT a live TestClient/DB or any AWS/Google
network (the real wire is exercised by the gated
``tests/integration/test_oauth_e2e_live.py``):

  1. AST/text shape — protects the initiate/callback wiring: four-walls
     auth on initiate, honest 409 when the provider is unconfigured (no
     fake redirect), callback authorizes ENTIRELY off the verified state,
     pointer-only secret storage (secret_ref, never the token value),
     and the no-fake-connected honesty fork on the callback.
  2. Behavioural — the ``app.integrations.oauth.state`` HMAC sign/verify
     round-trip, tamper rejection, and expiry. Pure functions, no I/O.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

# Importing the state module pulls app.integrations.oauth.__init__, which
# transitively imports app.core.config (a pydantic Settings requiring a
# few env vars). Mirror the behavioural-test convention: provide boot-safe
# defaults so the import never fails for lack of a real DB/keys.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

REPO_ROOT = Path(__file__).resolve().parents[2]
CONN_PATH = REPO_ROOT / "app" / "api" / "v1" / "admin_connections.py"
CONN_SCHEMA = REPO_ROOT / "app" / "schemas" / "connection.py"
AUDIT_PATH = REPO_ROOT / "app" / "models" / "admin_audit_log.py"
STATE_PATH = REPO_ROOT / "app" / "integrations" / "oauth" / "state.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _parse(p: Path) -> ast.Module:
    return ast.parse(_read(p))


def _function_node(path: Path, name: str) -> ast.FunctionDef:
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found in {path.name}")


# ---------------------------------------------------------------------
# Routes exist with the spec'd paths.
# ---------------------------------------------------------------------


def test_initiate_and_callback_paths_registered() -> None:
    src = _read(CONN_PATH)
    assert (
        '"/instances/{instance_id}/connections/oauth/{connection_type}/initiate"'
        in src
    )
    assert '"/connections/oauth/{connection_type}/callback"' in src


# ---------------------------------------------------------------------
# initiate: four-walls auth + honest 409 (never a fake redirect).
# ---------------------------------------------------------------------


def test_initiate_enforces_four_walls() -> None:
    src = ast.unparse(_function_node(CONN_PATH, "initiate_oauth_connection"))
    assert "_require_admin_id" in src
    assert "_load_active_instance" in src
    assert "_require_configure_connections" in src


def test_initiate_honest_409_when_not_configured() -> None:
    src = ast.unparse(_function_node(CONN_PATH, "initiate_oauth_connection"))
    # Provider absent / unconfigured → honest conflict, never a redirect.
    assert "get_oauth_provider" in src
    assert "is_configured" in src
    assert "HTTP_409_CONFLICT" in src
    assert "oauth_not_configured" in src


def test_initiate_signs_state_and_returns_auth_url() -> None:
    src = ast.unparse(_function_node(CONN_PATH, "initiate_oauth_connection"))
    assert "sign_state" in src
    assert "authorization_url" in src
    # Ensures the pending row exists in 'unconfigured'.
    assert "'unconfigured'" in src or '"unconfigured"' in src
    assert "ACTION_CONNECTION_OAUTH_INITIATED" in src


# ---------------------------------------------------------------------
# callback: authorizes off verified state, pointer-only secret, honesty.
# ---------------------------------------------------------------------


def test_callback_verifies_state_before_anything() -> None:
    src = ast.unparse(_function_node(CONN_PATH, "oauth_callback"))
    assert "verify_state" in src
    # A bad state is a 400 and NEVER reaches a token exchange.
    assert "OAuthStateError" in src
    assert "HTTP_400_BAD_REQUEST" in src
    assert "invalid_oauth_state" in src
    # The URL type must match the signed type (no state reuse across types).
    assert "oauth_state_type_mismatch" in src


def test_callback_resolves_tenant_off_state_not_request() -> None:
    src = ast.unparse(_function_node(CONN_PATH, "oauth_callback"))
    # Tenant identity comes from the verified state, not a session cookie.
    assert "verified.admin_id" in src
    assert "verified.instance_id" in src
    # System actor for the cookie-less callback's audit rows.
    assert "AuditContext.system" in src


def test_callback_real_exchange_and_pointer_only_storage() -> None:
    src = ast.unparse(_function_node(CONN_PATH, "oauth_callback"))
    assert "exchange_code" in src
    # Refresh token stored via the SecretStore; only the ref is persisted.
    assert "get_secret_store" in src
    assert ".put(" in src
    assert "secret_ref" in src
    assert "ACTION_CONNECTION_OAUTH_CONNECTED" in src


def test_callback_never_fakes_connected_on_failure() -> None:
    src = ast.unparse(_function_node(CONN_PATH, "oauth_callback"))
    # The failure path flips status to 'error' and audits — never connected.
    assert "'error'" in src or '"error"' in src
    assert "no refresh token" in src or "refresh_token" in src


# ---------------------------------------------------------------------
# disconnect: secret-cleanup enqueue for a non-null secret_ref.
# ---------------------------------------------------------------------


def test_disconnect_enqueues_secret_cleanup_for_stored_secret() -> None:
    src = ast.unparse(_function_node(CONN_PATH, "disconnect_connection"))
    assert "SecretCleanupOutboxRepository" in src
    assert "secret_ref" in src
    assert "secret_cleanup_enqueued" in src


# ---------------------------------------------------------------------
# Schemas + audit constants present.
# ---------------------------------------------------------------------


def test_oauth_initiate_response_schema() -> None:
    src = _read(CONN_SCHEMA)
    assert "class OAuthInitiateResponse" in src
    assert "authorization_url" in src
    assert "OAuthInitiateResponse" in src  # exported in __all__


def test_delete_response_carries_cleanup_flag() -> None:
    src = _read(CONN_SCHEMA)
    assert "secret_cleanup_enqueued" in src


def test_oauth_audit_constants_whitelisted() -> None:
    src = _read(AUDIT_PATH)
    for const in (
        "ACTION_CONNECTION_OAUTH_INITIATED",
        "ACTION_CONNECTION_OAUTH_CONNECTED",
    ):
        # Defined + added to ALLOWED_ACTIONS → at least two occurrences.
        assert src.count(const) >= 2, const


# =====================================================================
# Behavioural — the state HMAC sign/verify round-trip.
# =====================================================================


def test_state_sign_verify_round_trip() -> None:
    from app.integrations.oauth.state import sign_state, verify_state

    secret = "unit-test-secret"
    state = sign_state(
        admin_id="admin-1",
        instance_id=42,
        connection_type="calendar",
        secret=secret,
        now=1_000,
    )
    verified = verify_state(
        state, secret=secret, max_age_seconds=600, now=1_100
    )
    assert verified.admin_id == "admin-1"
    assert verified.instance_id == 42
    assert verified.connection_type == "calendar"
    assert verified.issued_at == 1_000
    assert verified.nonce  # a random nonce was embedded


def test_state_rejects_tampered_payload() -> None:
    from app.integrations.oauth.state import (
        OAuthStateError,
        sign_state,
        verify_state,
    )

    state = sign_state(
        admin_id="admin-1",
        instance_id=42,
        connection_type="calendar",
        secret="secret",
        now=1_000,
    )
    payload, _, sig = state.partition(".")
    # Flip the last char of the payload — HMAC must no longer match.
    tampered_payload = payload[:-1] + ("A" if payload[-1] != "A" else "B")
    tampered = f"{tampered_payload}.{sig}"
    with pytest.raises(OAuthStateError):
        verify_state(tampered, secret="secret", max_age_seconds=600, now=1_100)


def test_state_rejects_wrong_secret() -> None:
    from app.integrations.oauth.state import (
        OAuthStateError,
        sign_state,
        verify_state,
    )

    state = sign_state(
        admin_id="a",
        instance_id=1,
        connection_type="calendar",
        secret="real-secret",
        now=1_000,
    )
    with pytest.raises(OAuthStateError):
        verify_state(
            state, secret="forged-secret", max_age_seconds=600, now=1_100
        )


def test_state_rejects_expired() -> None:
    from app.integrations.oauth.state import (
        OAuthStateError,
        sign_state,
        verify_state,
    )

    state = sign_state(
        admin_id="a",
        instance_id=1,
        connection_type="calendar",
        secret="secret",
        now=1_000,
    )
    with pytest.raises(OAuthStateError):
        # 601s later, TTL 600 → expired.
        verify_state(state, secret="secret", max_age_seconds=600, now=1_601)


def test_state_rejects_future_issued_at() -> None:
    from app.integrations.oauth.state import (
        OAuthStateError,
        sign_state,
        verify_state,
    )

    state = sign_state(
        admin_id="a",
        instance_id=1,
        connection_type="calendar",
        secret="secret",
        now=10_000,
    )
    with pytest.raises(OAuthStateError):
        # Verifier's clock is far behind issued_at → implausible future.
        verify_state(state, secret="secret", max_age_seconds=600, now=1_000)


def test_state_malformed_rejected() -> None:
    from app.integrations.oauth.state import OAuthStateError, verify_state

    for bad in ("", "no-dot", "only.", ".only", "@@@.@@@"):
        with pytest.raises(OAuthStateError):
            verify_state(bad, secret="secret", max_age_seconds=600, now=1_000)
