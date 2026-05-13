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

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

import jwt

from app.core.config import settings

logger = logging.getLogger(__name__)


# The two token classes the service mints. Distinct ``typ`` claims so a
# magic link cannot be passed off as a session cookie (and vice versa)
# in the event of a misrouted Authorization header.
TOKEN_TYPE_MAGIC_LINK: Literal["magic_link"] = "magic_link"
TOKEN_TYPE_SESSION: Literal["session"] = "session"

JWT_ALGORITHM = "HS256"
JWT_ISSUER = "luciel-backend"


class MagicLinkError(Exception):
    """Raised on any magic-link or session-cookie validation failure.

    The route layer maps this to a 401, never a 500. The message is
    deliberately generic ("invalid or expired link") so a probing
    client cannot distinguish "wrong signature" from "expired" from
    "wrong token class" -- all three should look like one failure to
    a brute-forcer.
    """


def _secret_or_fail() -> str:
    """Pull the signing secret from settings, raising MagicLinkError if empty."""
    if not settings.magic_link_secret:
        raise MagicLinkError(
            "Magic-link signing secret is not configured. "
            "Set MAGIC_LINK_SECRET on the backend."
        )
    return settings.magic_link_secret


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
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def mint_session_token(*, user_id: uuid.UUID, email: str, tenant_id: str) -> str:
    """Return a signed, long-TTL JWT for the cookie session.

    Same shape as the magic-link token but with ``typ='session'`` and
    the configured cookie TTL. The cookie route validates one and
    issues the other, so a leaked magic link cannot be reused as a
    session cookie after a single consume.
    """
    secret = _secret_or_fail()
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
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def _decode(token: str, *, expected_typ: str) -> dict:
    """Decode, verify, and type-check a JWT. Raises MagicLinkError on any failure."""
    secret = _secret_or_fail()
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
    cookie can be set on the apex domain (luciel.ai) and the React
    router can handle the subsequent navigation. The marketing site's
    ``/login`` route POSTs the token back to the backend's
    ``/api/v1/billing/login`` endpoint, which sets the cookie and
    returns a redirect target.
    """
    base = settings.marketing_site_url.rstrip("/")
    return f"{base}/login?token={token}"
