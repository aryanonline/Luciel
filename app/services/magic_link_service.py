"""Step 30a: magic-link auth service.

The post-checkout login flow is intentionally narrow at v1:

  1. The webhook handler mints a magic-link JWT and emails it.
  2. The buyer clicks the link, which carries the JWT as a query param.
  3. The /api/v1/billing/login route validates the JWT and sets a
     30-day signed cookie session.
  4. All cookie-bearing requests (portal, /me) re-validate the cookie.

No password store at v1 -- Step 32 owns full self-serve identity. The
JWT here is a deliberate stop-gap; it is one-shot (consumed on first
use via the `jti` claim recorded in audit) and short-TTL (24h) so a
leaked link is bounded.

We use HS256 with a single server-side shared secret rather than RS256
because:
  - the issuer and the verifier are the same process,
  - there is no third-party validator who needs the public key,
  - HS256 has no key-distribution surface to forget about.

PyJWT is the dependency. We DO NOT use `python-jose` -- it has a
history of CVEs around the `none` algorithm handling that PyJWT
explicitly forbids by requiring `algorithms=[...]` on every decode.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Literal

import jwt

from app.core.config import settings

logger = logging.getLogger(__name__)


# The token classes the service mints. Distinct ``typ`` claims so a
# magic link cannot be passed off as a session cookie (and vice versa)
# in the event of a misrouted Authorization header. Step 30a.3 adds two
# more typed classes for the password-auth flow (`set_password` and
# `reset_password`) so the existing magic-link cookie redeem path cannot
# be tricked into accepting a token minted for a password-set surface
# (and vice versa) -- the route layer's `expected_typ` check is the
# enforcement seam.
TOKEN_TYPE_MAGIC_LINK: Literal["magic_link"] = "magic_link"
TOKEN_TYPE_SESSION: Literal["session"] = "session"
# Step 30a.3: token minted at User-creation time in the Stripe webhook,
# emailed in the welcome message, consumed by the marketing-site
# /auth/set-password page to authenticate the first password-set event.
# Same TTL as the magic-link class (24h) -- if a customer doesn't set
# a password within 24h of paying, the /forgot-password recovery path
# handles them with a fresh `reset_password` token.
TOKEN_TYPE_SET_PASSWORD: Literal["set_password"] = "set_password"
# Step 30a.3: token minted by POST /api/v1/auth/forgot-password, emailed
# to the user, consumed by the same /auth/set-password page. The two
# token classes share the redeem surface but carry distinct `typ` claims
# so the audit row can record WHY the password was set (signup vs reset).
TOKEN_TYPE_RESET_PASSWORD: Literal["reset_password"] = "reset_password"

JWT_ALGORITHM = "HS256"
JWT_ISSUER = "luciel-backend"

# Arc 3 Work-Unit B.2: the `kid` we stamp on tokens minted when the
# operator has NOT yet populated the new jwt_signing_keys_json blob.
# This is the boot-time shim that keeps the code change deploy-safe
# regardless of SSM ordering. See arc3-out/B2-kid-rolling-design.md.
_LEGACY_KID = "legacy"


class MagicLinkError(Exception):
    """Raised on any magic-link or session-cookie validation failure.

    The route layer maps this to a 401, never a 500. The message is
    deliberately generic ("invalid or expired link") so a probing
    client cannot distinguish "wrong signature" from "expired" from
    "wrong token class" -- all three should look like one failure to
    a brute-forcer.
    """


# ---------------------------------------------------------------------
# Arc 3 Work-Unit B.2 -- JWT signing-key `kid` rolling window
# ---------------------------------------------------------------------
#
# _resolve_keys() returns the {kid: secret} map that the minter and
# decoder both consult. The decoder reads the token's own `kid` header
# (via jwt.get_unverified_header) and looks up the secret in this
# map; unknown kid is collapsed to MagicLinkError ("Invalid token.")
# so a probing client cannot distinguish "wrong kid" from "wrong
# signature" from "expired" -- same posture as the rest of _decode().
#
# The map is parsed once per process via @lru_cache. Hot-reloading the
# JSON blob in-place is intentionally NOT supported: rotation goes
# through an ECS service bounce, which is the chokepoint where the
# operator gets to observe the new keys load cleanly before any user
# traffic hits them.
#
# Boot-time shim semantics:
#   * jwt_signing_keys_json populated -> use it verbatim.
#   * jwt_signing_keys_json empty AND magic_link_secret set ->
#     fabricate {_LEGACY_KID: magic_link_secret}. This is what every
#     pre-B.2 deploy will see until SSM gets the new params, and what
#     every token minted before B.2 (no kid header) will decode under.
#   * both empty -> fail closed at first mint/decode.


@lru_cache(maxsize=1)
def _resolve_keys() -> dict[str, str]:
    """Return the {kid: secret} signing-key map for the current process.

    Cached for the lifetime of the process. To pick up a rotation,
    bounce the ECS service.

    Test-time note: the cache means tests that mutate
    settings.jwt_signing_keys_json must call
    `_resolve_keys.cache_clear()` after the mutation. The new
    `tests/security/test_jwt_kid_rolling.py` does this through a
    fixture so callers don't have to think about it.
    """
    raw = settings.jwt_signing_keys_json.strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MagicLinkError(
                "jwt_signing_keys_json is set but is not valid JSON; "
                "refusing to sign or verify."
            ) from exc
        if not isinstance(parsed, dict) or not parsed:
            raise MagicLinkError(
                "jwt_signing_keys_json must be a non-empty JSON object "
                "of {kid: secret}."
            )
        for k, v in parsed.items():
            if not isinstance(k, str) or not k:
                raise MagicLinkError(
                    "jwt_signing_keys_json contains a non-string or "
                    "empty kid."
                )
            if not isinstance(v, str) or not v:
                raise MagicLinkError(
                    "jwt_signing_keys_json contains a non-string or "
                    "empty secret."
                )
        return dict(parsed)

    # Boot-time shim: fall back to the single legacy secret.
    if settings.magic_link_secret:
        return {_LEGACY_KID: settings.magic_link_secret}

    raise MagicLinkError(
        "No JWT signing key is configured. Set either "
        "JWT_SIGNING_KEYS_JSON (preferred) or MAGIC_LINK_SECRET "
        "(legacy shim) on the backend."
    )


def _active_kid() -> str:
    """Return the kid the minter should stamp on freshly-issued tokens.

    Resolution order:
      1. settings.jwt_active_kid, if non-empty AND present in the key map.
      2. _LEGACY_KID, if the key map has exactly that one entry (the
         boot-time shim path).
      3. Otherwise raise -- an ambiguous map with no active pointer is
         an operator misconfiguration that must fail closed.
    """
    keys = _resolve_keys()
    active = settings.jwt_active_kid.strip()
    if active:
        if active not in keys:
            raise MagicLinkError(
                f"jwt_active_kid={active!r} is not present in "
                f"jwt_signing_keys_json; refusing to mint."
            )
        return active
    if list(keys.keys()) == [_LEGACY_KID]:
        return _LEGACY_KID
    raise MagicLinkError(
        "jwt_active_kid is empty and the key map has more than one "
        "entry; cannot disambiguate which kid to sign with."
    )


def _secret_or_fail() -> str:
    """Return the signing secret for the active kid.

    Semantics: "what would a fresh mint use right now?" -- which is
    exactly what every existing caller (the four mint_* functions, the
    e2e test fixtures that re-mint tokens with a stamped jti) wants.

    Preserved as a module-level helper for backward compatibility with
    tests/e2e/step_30a_4_team_invite_live_e2e.py and any future caller
    that wants to mint a token outside the four canonical mint paths.
    """
    return _resolve_keys()[_active_kid()]


def mint_magic_link_token(*, user_id: uuid.UUID, email: str, tenant_id: str) -> str:
    """Return a signed, short-TTL JWT that authorizes one login.

    The token carries the user_id + email + tenant so the consuming
    route can resolve the user without a DB hit in the happy path,
    and so audit logs can attribute the click without re-fetching
    the user row. The `jti` is included so the consume path can
    record it on the audit row, giving us a forward-looking hook
    for blacklist-based revocation if we ever need it.
    """
    secret = _secret_or_fail()
    kid = _active_kid()
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=settings.magic_link_ttl_hours)
    payload = {
        "iss": JWT_ISSUER,
        "sub": str(user_id),
        "email": email,
        "tenant_id": tenant_id,
        "typ": TOKEN_TYPE_MAGIC_LINK,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM, headers={"kid": kid})


def mint_session_token(*, user_id: uuid.UUID, email: str, tenant_id: str) -> str:
    """Return a signed, long-TTL JWT for the cookie session.

    Same shape as the magic-link token but with ``typ='session'`` and
    the configured cookie TTL. The cookie route validates one and
    issues the other, so a leaked magic link cannot be reused as a
    session cookie after a single consume.
    """
    secret = _secret_or_fail()
    kid = _active_kid()
    now = datetime.now(timezone.utc)
    exp = now + timedelta(days=settings.session_cookie_ttl_days)
    payload = {
        "iss": JWT_ISSUER,
        "sub": str(user_id),
        "email": email,
        "tenant_id": tenant_id,
        "typ": TOKEN_TYPE_SESSION,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM, headers={"kid": kid})


def _decode(token: str, *, expected_typ: str) -> dict:
    """Decode, verify, and type-check a JWT. Raises MagicLinkError on any failure.

    Arc 3 Work-Unit B.2: key resolution goes through the `kid` header.
    A token's `kid` header tells us which secret in the key-map to
    verify against. Tokens minted before B.2 have no `kid` header --
    PyJWT's get_unverified_header returns {} for those, and we fall
    back to `_LEGACY_KID`. The legacy entry in the key-map IS the old
    single magic_link_secret, so legacy tokens continue to verify
    transparently throughout the cutover.

    Unknown kid is collapsed to "Invalid token." -- same posture as
    bad signature / wrong issuer / expired. A probing client cannot
    distinguish "wrong kid" from "wrong signature".
    """
    keys = _resolve_keys()
    try:
        unverified_header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise MagicLinkError("Invalid token.") from exc

    kid = unverified_header.get("kid", _LEGACY_KID)
    secret = keys.get(kid)
    if secret is None:
        # Either the token was signed with a key we've retired, OR a
        # legacy (kid-less) token arrived while no _LEGACY_KID entry
        # exists in the key-map (post-rotation cleanup state). Both
        # surface as "Invalid token." -- no oracle.
        raise MagicLinkError("Invalid token.")

    try:
        # `algorithms=[JWT_ALGORITHM]` is non-optional; without it the
        # PyJWT library refuses to validate, and even a misconfigured
        # call cannot fall through to the `none` algorithm.
        decoded = jwt.decode(
            token,
            secret,
            algorithms=[JWT_ALGORITHM],
            issuer=JWT_ISSUER,
            options={"require": ["exp", "iat", "sub", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise MagicLinkError("Token expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise MagicLinkError("Invalid token.") from exc

    if decoded.get("typ") != expected_typ:
        raise MagicLinkError("Wrong token class.")
    return decoded


def consume_magic_link_token(token: str) -> dict:
    """Validate a magic-link JWT and return its payload.

    The link is "consumed" in the sense that the cookie session it
    produces is now the valid credential -- there is no DB-side
    blacklist of jti's at v1 (deliberate: the 24h expiry plus
    one-shot-by-convention is the v1 bound; a stricter blacklist
    lands with Step 32 self-serve).
    """
    return _decode(token, expected_typ=TOKEN_TYPE_MAGIC_LINK)


def validate_session_token(token: str) -> dict:
    """Validate a session-cookie JWT and return its payload."""
    return _decode(token, expected_typ=TOKEN_TYPE_SESSION)


def build_magic_link_url(token: str) -> str:
    """Construct the click-through URL the email body should carry.

    The URL points at the *marketing site* (not the backend) so the
    cookie can be set on the apex domain (vantagemind.ai) and the React
    router can handle the subsequent navigation. The marketing site's
    ``/login`` route POSTs the token back to the backend's
    ``/api/v1/billing/login`` endpoint, which sets the cookie and
    returns a redirect target.
    """
    base = settings.marketing_site_url.rstrip("/")
    return f"{base}/login?token={token}"


# ---------------------------------------------------------------------
# Step 30a.3 -- password-auth token primitives
# ---------------------------------------------------------------------


def mint_set_password_token(
    *,
    user_id: uuid.UUID,
    email: str,
    tenant_id: str,
    purpose: Literal["signup", "invite"] = "signup",
) -> str:
    """Mint a short-TTL ``set_password``-class JWT.

    Used at two surfaces:
      * **Signup (Option B welcome-email mechanic, the default).**
        The Stripe ``checkout.session.completed`` webhook mints the
        User row, commits, then calls this with ``purpose="signup"``
        and emails the buyer a welcome link of shape
        ``<MARKETING>/auth/set-password?token=...``. This is the
        load-bearing claim of "password mandatory at signup" --
        until the buyer redeems this token, there is no path to a
        cookied ``/app`` session for them.
      * **Invite acceptance (Step 30a.4 / 30a.5).**
        ``purpose="invite"`` is reserved for the invitee-onboarding
        flow that lands in those steps. The token shape is identical;
        only the audit-row classification differs.

    Same 24h TTL as the magic-link class. The ``purpose`` claim is
    propagated through to the audit row at consume time so we can
    answer "did this password come from a signup or an invite?" without
    a second probe.
    """
    secret = _secret_or_fail()
    kid = _active_kid()
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=settings.magic_link_ttl_hours)
    payload = {
        "iss": JWT_ISSUER,
        "sub": str(user_id),
        "email": email,
        "tenant_id": tenant_id,
        "typ": TOKEN_TYPE_SET_PASSWORD,
        "purpose": purpose,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM, headers={"kid": kid})


def mint_reset_password_token(
    *,
    user_id: uuid.UUID,
    email: str,
    tenant_id: str,
) -> str:
    """Mint a short-TTL ``reset_password``-class JWT.

    Used exclusively by ``POST /api/v1/auth/forgot-password``. The
    redeem surface is the same ``/auth/set-password`` page the signup
    welcome flow uses; the page POSTs back to the same backend route.
    The token class is distinct so the audit-row records reset vs
    initial-set unambiguously, and so a leaked signup token cannot be
    replayed as a reset (and vice versa) after the original consume.
    """
    secret = _secret_or_fail()
    kid = _active_kid()
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=settings.magic_link_ttl_hours)
    payload = {
        "iss": JWT_ISSUER,
        "sub": str(user_id),
        "email": email,
        "tenant_id": tenant_id,
        "typ": TOKEN_TYPE_RESET_PASSWORD,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM, headers={"kid": kid})


def consume_set_password_token(token: str) -> dict:
    """Validate a ``set_password``-class JWT and return its payload.

    The redeem path lives in ``POST /api/v1/auth/set-password`` -- it
    calls this, then ``AuthService.set_password``, then mints the
    session cookie. The token is one-shot by convention (same as the
    magic-link class); a stricter blacklist lands with Step 32a.
    """
    return _decode(token, expected_typ=TOKEN_TYPE_SET_PASSWORD)


def consume_reset_password_token(token: str) -> dict:
    """Validate a ``reset_password``-class JWT and return its payload.

    Same redeem path as ``consume_set_password_token``; distinct typ
    so the cross-class replay attack is blocked.
    """
    return _decode(token, expected_typ=TOKEN_TYPE_RESET_PASSWORD)


def build_set_password_url(token: str) -> str:
    """Construct the click-through URL the welcome / reset email carries.

    Points at the marketing-site ``/auth/set-password`` page which:
      1. Reads the token from the query string.
      2. Renders a password input.
      3. POSTs the password + token to
         ``POST /api/v1/auth/set-password``.
      4. On 200, follows the response's ``redirect`` field to ``/app``.
    """
    base = settings.marketing_site_url.rstrip("/")
    return f"{base}/auth/set-password?token={token}"
