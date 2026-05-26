"""Arc 3 Work-Unit B.2 -- JWT `kid` rolling-window contract.

Drift: ``D-set-password-token-logged-plaintext-2026-05-17``
(residual on the 13 unmatched leaked JTIs from Arc 3 A.2a).

Before B.2, ``app/services/magic_link_service.py`` had a single
shared HS256 secret with no key-versioning, no kid header on minted
tokens, and no rolling-window decode path. That meant in-place key
rotation atomically invalidated every signed-in customer's 30-day
session cookie plus every in-flight 24h ephemeral token (magic-link,
set_password, reset_password). Not acceptable.

B.2 introduces a ``kid``-header-based two-key rolling-window scheme:

  * The minter stamps the active ``kid`` on every newly-issued token.
  * The decoder reads the token's own ``kid`` header via
    ``jwt.get_unverified_header`` and looks up the correct secret
    from a {kid: secret} map populated from
    ``settings.jwt_signing_keys_json``.
  * A boot-time shim lets legacy (kid-less) tokens decode under a
    fabricated ``"legacy"`` entry that mirrors
    ``settings.magic_link_secret``. This keeps the code change deploy-
    safe regardless of SSM-rollout ordering.
  * Unknown kid is collapsed to ``MagicLinkError("Invalid token.")``
    -- same posture as bad signature / wrong issuer / expired. No
    oracle.

This file is the **invariant pin** for the contract. Seven tests:

  1. Active-kid mint stamps the right kid header.
  2. Decode under secondary kid (rotation grace window) succeeds.
  3. Unknown kid surfaces as Invalid token.
  4. Legacy shim: kidless token decodes under the magic_link_secret
     fallback.
  5. Legacy shim + kid promotion: minter uses new kid, kidless legacy
     tokens still decode.
  6. Both keys empty: fail closed.
  7. Active kid pointing at a missing entry: fail closed at mint.

Pairs with arc3-out/B2-kid-rolling-design.md.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import jwt as _jwt
import pytest

from app.core.config import settings
from app.services import magic_link_service as svc
from app.services.magic_link_service import (
    JWT_ALGORITHM,
    JWT_ISSUER,
    MagicLinkError,
    TOKEN_TYPE_MAGIC_LINK,
    TOKEN_TYPE_SESSION,
    TOKEN_TYPE_SET_PASSWORD,
    _LEGACY_KID,
    _active_kid,
    _resolve_keys,
    consume_magic_link_token,
    consume_set_password_token,
    mint_magic_link_token,
    mint_session_token,
    mint_set_password_token,
    validate_session_token,
)


# ---------------------------------------------------------------------
# Fixture: reset settings + clear the @lru_cache around each test.
# The kid-rolling design caches the key map at module import; tests
# that mutate settings must clear the cache or the next call reads
# the previous snapshot.
# ---------------------------------------------------------------------


@pytest.fixture
def jwt_env(monkeypatch):
    """Mutate JWT-related settings and clear the resolution cache.

    Yields a small DSL:

        jwt_env.set_keys({"a": "secret-a"}, active="a")
        jwt_env.set_legacy("legacy-secret")        # boot-time shim only
        jwt_env.clear_all()                        # both empty
    """
    class _Env:
        def set_keys(self, keys: dict[str, str], *, active: str | None,
                     grace: str = "", legacy_secret: str = ""):
            monkeypatch.setattr(
                settings, "jwt_signing_keys_json", json.dumps(keys)
            )
            monkeypatch.setattr(
                settings, "jwt_active_kid", active or ""
            )
            monkeypatch.setattr(
                settings, "jwt_grace_kid", grace
            )
            monkeypatch.setattr(
                settings, "magic_link_secret", legacy_secret
            )
            _resolve_keys.cache_clear()

        def set_legacy(self, secret: str):
            monkeypatch.setattr(settings, "jwt_signing_keys_json", "")
            monkeypatch.setattr(settings, "jwt_active_kid", "")
            monkeypatch.setattr(settings, "jwt_grace_kid", "")
            monkeypatch.setattr(settings, "magic_link_secret", secret)
            _resolve_keys.cache_clear()

        def clear_all(self):
            monkeypatch.setattr(settings, "jwt_signing_keys_json", "")
            monkeypatch.setattr(settings, "jwt_active_kid", "")
            monkeypatch.setattr(settings, "jwt_grace_kid", "")
            monkeypatch.setattr(settings, "magic_link_secret", "")
            _resolve_keys.cache_clear()

    env = _Env()
    yield env
    # Defensive: clear once more so leak between tests is impossible.
    _resolve_keys.cache_clear()


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _sample_user():
    return {
        "user_id": uuid.uuid4(),
        "email": "test@example.com",
        "admin_id": "tenant-test",
    }


def _peel_kid(token: str) -> str | None:
    """Read the kid claim straight off the unverified header."""
    return _jwt.get_unverified_header(token).get("kid")


# ---------------------------------------------------------------------
# 1. mint_*: active kid is stamped on the token header
# ---------------------------------------------------------------------


def test_mint_stamps_active_kid_on_all_four_classes(jwt_env):
    jwt_env.set_keys({"v2026-05-21": "primary-secret"}, active="v2026-05-21")
    u = _sample_user()

    for fn in (
        lambda: mint_magic_link_token(**u),
        lambda: mint_session_token(**u),
        lambda: mint_set_password_token(**u, purpose="signup"),
        # The reset-password mint is the fourth class; cover it explicitly.
        lambda: svc.mint_reset_password_token(**u),
    ):
        token = fn()
        assert _peel_kid(token) == "v2026-05-21"


# ---------------------------------------------------------------------
# 2. Decode under secondary kid: rotation grace window works
# ---------------------------------------------------------------------


def test_decode_succeeds_under_secondary_kid_during_grace(jwt_env):
    # Steady-state rotation snapshot: primary is "v2", grace key is "v1".
    jwt_env.set_keys(
        {"v1": "old-secret", "v2": "new-secret"},
        active="v2",
        grace="v1",
    )
    u = _sample_user()

    # Hand-mint a token signed with the OLD ("v1") key, stamped with kid=v1,
    # mimicking what would still be in flight after the operator promoted
    # active=v2.
    now = datetime.now(timezone.utc)
    payload = {
        "iss": JWT_ISSUER,
        "sub": str(u["user_id"]),
        "email": u["email"],
        "admin_id": u["admin_id"],
        "typ": TOKEN_TYPE_MAGIC_LINK,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
        "jti": str(uuid.uuid4()),
    }
    old_token = _jwt.encode(
        payload, "old-secret", algorithm=JWT_ALGORITHM, headers={"kid": "v1"}
    )

    decoded = consume_magic_link_token(old_token)
    assert decoded["sub"] == str(u["user_id"])
    assert decoded["typ"] == TOKEN_TYPE_MAGIC_LINK

    # And a freshly-minted token (kid=v2) also decodes.
    new_token = mint_magic_link_token(**u)
    assert _peel_kid(new_token) == "v2"
    decoded_new = consume_magic_link_token(new_token)
    assert decoded_new["sub"] == str(u["user_id"])


# ---------------------------------------------------------------------
# 3. Unknown kid is collapsed to Invalid token (no oracle)
# ---------------------------------------------------------------------


def test_unknown_kid_raises_invalid_token(jwt_env):
    jwt_env.set_keys({"v2": "new-secret"}, active="v2")
    u = _sample_user()

    # Mint a token with a kid that is NOT in the map.
    now = datetime.now(timezone.utc)
    payload = {
        "iss": JWT_ISSUER,
        "sub": str(u["user_id"]),
        "email": u["email"],
        "admin_id": u["admin_id"],
        "typ": TOKEN_TYPE_MAGIC_LINK,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
        "jti": str(uuid.uuid4()),
    }
    bad_token = _jwt.encode(
        payload,
        "some-old-key-no-longer-in-map",
        algorithm=JWT_ALGORITHM,
        headers={"kid": "retired-kid"},
    )

    with pytest.raises(MagicLinkError) as exc:
        consume_magic_link_token(bad_token)
    # Must surface as generic "Invalid token." -- not a kid-specific message.
    assert "Invalid token" in str(exc.value)
    assert "kid" not in str(exc.value).lower()


# ---------------------------------------------------------------------
# 4. Legacy shim: kidless tokens decode under magic_link_secret
# ---------------------------------------------------------------------


def test_legacy_shim_decodes_kidless_token(jwt_env):
    # Boot-time shim: only magic_link_secret is set; jwt_signing_keys_json
    # is empty. This is what every existing deploy looks like the moment
    # B.2 code lands but before SSM is touched.
    jwt_env.set_legacy("legacy-secret")
    u = _sample_user()

    # Hand-mint a token with NO kid header (the pre-B.2 shape).
    now = datetime.now(timezone.utc)
    payload = {
        "iss": JWT_ISSUER,
        "sub": str(u["user_id"]),
        "email": u["email"],
        "admin_id": u["admin_id"],
        "typ": TOKEN_TYPE_SESSION,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=1)).timestamp()),
        "jti": str(uuid.uuid4()),
    }
    pre_b2_token = _jwt.encode(payload, "legacy-secret", algorithm=JWT_ALGORITHM)
    # Sanity: this token genuinely has no kid header.
    assert _peel_kid(pre_b2_token) is None

    decoded = validate_session_token(pre_b2_token)
    assert decoded["sub"] == str(u["user_id"])

    # And a fresh mint via the service path stamps kid=_LEGACY_KID.
    fresh = mint_session_token(**u)
    assert _peel_kid(fresh) == _LEGACY_KID
    decoded_fresh = validate_session_token(fresh)
    assert decoded_fresh["sub"] == str(u["user_id"])


# ---------------------------------------------------------------------
# 5. Legacy shim + kid promotion: cutover step still decodes kidless
# ---------------------------------------------------------------------


def test_legacy_shim_then_kid_promotion_keeps_kidless_decoding(jwt_env):
    # Cutover snapshot: magic_link_secret is still set as the boot-time
    # default, but the operator has populated jwt_signing_keys_json with
    # both the legacy value AND a new primary key, and promoted active=v2.
    # Tokens minted before B.2 (no kid header) must still decode, under
    # the _LEGACY_KID entry in the new map.
    jwt_env.set_keys(
        {_LEGACY_KID: "old-secret", "v2": "new-secret"},
        active="v2",
        grace=_LEGACY_KID,
        legacy_secret="old-secret",  # still set on the shim, harmless
    )
    u = _sample_user()

    # Hand-mint a pre-B.2 kidless token signed with old-secret.
    now = datetime.now(timezone.utc)
    payload = {
        "iss": JWT_ISSUER,
        "sub": str(u["user_id"]),
        "email": u["email"],
        "admin_id": u["admin_id"],
        "typ": TOKEN_TYPE_SET_PASSWORD,
        "purpose": "signup",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=23)).timestamp()),
        "jti": str(uuid.uuid4()),
    }
    legacy_token = _jwt.encode(payload, "old-secret", algorithm=JWT_ALGORITHM)
    assert _peel_kid(legacy_token) is None

    decoded = consume_set_password_token(legacy_token)
    assert decoded["sub"] == str(u["user_id"])

    # Fresh mint uses the promoted v2 key.
    fresh = mint_set_password_token(**u, purpose="signup")
    assert _peel_kid(fresh) == "v2"
    decoded_fresh = consume_set_password_token(fresh)
    assert decoded_fresh["sub"] == str(u["user_id"])


# ---------------------------------------------------------------------
# 6. Both empty -> fail closed
# ---------------------------------------------------------------------


def test_no_signing_key_configured_fails_closed(jwt_env):
    jwt_env.clear_all()
    u = _sample_user()
    with pytest.raises(MagicLinkError) as exc:
        mint_magic_link_token(**u)
    msg = str(exc.value).lower()
    # Operator-facing message names BOTH env vars so the runbook is
    # discoverable from the exception alone.
    assert "no jwt signing key" in msg
    assert "jwt_signing_keys_json" in msg.lower()
    assert "magic_link_secret" in msg.lower()


# ---------------------------------------------------------------------
# 7. Active kid pointing at a missing entry -> fail closed at mint
# ---------------------------------------------------------------------


def test_active_kid_pointing_at_missing_entry_fails_closed(jwt_env):
    jwt_env.set_keys({"v1": "old-secret"}, active="v2-typo")
    u = _sample_user()
    with pytest.raises(MagicLinkError) as exc:
        mint_magic_link_token(**u)
    assert "jwt_active_kid" in str(exc.value)
    assert "v2-typo" in str(exc.value)


# ---------------------------------------------------------------------
# Bonus invariant: _active_kid() raises (not returns ambiguous) when
# the operator forgets to set jwt_active_kid on a multi-key map.
# ---------------------------------------------------------------------


def test_multi_key_map_without_active_pointer_fails_closed(jwt_env):
    jwt_env.set_keys({"v1": "a", "v2": "b"}, active=None)
    with pytest.raises(MagicLinkError) as exc:
        _active_kid()
    assert "ambiguous" in str(exc.value).lower() or \
        "more than one" in str(exc.value).lower() or \
        "cannot disambiguate" in str(exc.value).lower()


# ---------------------------------------------------------------------
# Bonus invariant: malformed JSON in jwt_signing_keys_json fails closed
# ---------------------------------------------------------------------


def test_malformed_keys_json_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "jwt_signing_keys_json", "{not-json")
    monkeypatch.setattr(settings, "jwt_active_kid", "v1")
    monkeypatch.setattr(settings, "magic_link_secret", "")
    _resolve_keys.cache_clear()
    with pytest.raises(MagicLinkError) as exc:
        _resolve_keys()
    assert "not valid JSON" in str(exc.value)
    _resolve_keys.cache_clear()
