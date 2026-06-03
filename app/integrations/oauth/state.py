"""Tamper-resistant OAuth ``state`` — Arc 17 callback authorization.

The OAuth callback endpoint is UNAUTHENTICATED in the session-cookie
sense: Google redirects the browser to it server-to-browser-to-server
with NO cookie. So the callback cannot trust the request to tell it
which tenant the consent belongs to — it must derive ``(admin_id,
instance_id, connection_type)`` from the ``state`` round-tripped through
Google, and that state MUST be tamper-resistant.

Design: the initiate endpoint encodes the tuple plus a random nonce and
an issued-at timestamp into a compact payload, then appends an HMAC-SHA256
over the payload keyed by ``settings.oauth_state_signing_secret``. The
callback recomputes the HMAC and rejects any mismatch (forged / tampered)
in constant time, then rejects an expired state (older than
``oauth_state_ttl_seconds``). The signing secret never leaves the server;
a client cannot mint a valid state for a tenant it does not own.

Wire format (all URL-safe base64, no padding, joined by ``.``)::

    b64(payload_json) . b64(hmac_sha256(payload_json))

``payload_json`` is a sorted-key JSON object::

    {"a": admin_id, "i": instance_id, "t": connection_type,
     "n": nonce_hex, "ts": issued_at_epoch_seconds}

The nonce makes two states minted for the same tuple distinct (so a
short-lived state row / replay-detection layer could key off it later);
this module does not itself persist nonces — the HMAC + TTL is the
tamper/expiry fence the brief requires. Keeping it stateless means the
callback needs no DB lookup to validate, which matches the existing
deploy-gated, boot-safe posture of the connections layer.
"""
from __future__ import annotations

import base64
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from hashlib import sha256


class OAuthStateError(ValueError):
    """Raised when a ``state`` fails to verify: malformed, bad HMAC
    (forged / tampered), or expired. The callback maps this to a 400 and
    NEVER proceeds to a token exchange — an unverifiable state must not
    resolve a tenant."""


@dataclass(frozen=True)
class OAuthState:
    """The verified tuple a callback authorizes off of."""

    admin_id: str
    instance_id: int
    connection_type: str
    nonce: str
    issued_at: int


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload: bytes, *, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), payload, sha256).digest()


def sign_state(
    *,
    admin_id: str,
    instance_id: int,
    connection_type: str,
    secret: str,
    now: int | None = None,
) -> str:
    """Mint a signed, opaque ``state`` for the initiate endpoint.

    The returned string is safe to pass verbatim as the OAuth ``state``
    query param. ``now`` is injectable for tests; production omits it.
    """
    issued_at = int(time.time()) if now is None else now
    payload_obj = {
        "a": admin_id,
        "i": instance_id,
        "t": connection_type,
        "n": secrets.token_hex(16),
        "ts": issued_at,
    }
    payload = json.dumps(
        payload_obj, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    sig = _sign(payload, secret=secret)
    return f"{_b64encode(payload)}.{_b64encode(sig)}"


def verify_state(
    state: str,
    *,
    secret: str,
    max_age_seconds: int,
    now: int | None = None,
) -> OAuthState:
    """Verify a ``state`` and return its tuple, or raise OAuthStateError.

    Checks, in order: structural shape, HMAC (constant-time), required
    fields, then expiry. The HMAC check happens BEFORE any field is
    trusted, so a tampered payload never reaches tenant resolution.
    """
    if not state or "." not in state:
        raise OAuthStateError("state is empty or malformed")

    encoded_payload, _, encoded_sig = state.partition(".")
    try:
        payload = _b64decode(encoded_payload)
        provided_sig = _b64decode(encoded_sig)
    except (ValueError, TypeError) as exc:
        raise OAuthStateError("state is not valid base64") from exc

    expected_sig = _sign(payload, secret=secret)
    if not hmac.compare_digest(provided_sig, expected_sig):
        raise OAuthStateError("state signature mismatch (tampered or forged)")

    try:
        obj = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:  # pragma: no cover
        # Unreachable for a payload we ourselves signed, but fail honest.
        raise OAuthStateError("state payload is not valid JSON") from exc

    try:
        admin_id = str(obj["a"])
        instance_id = int(obj["i"])
        connection_type = str(obj["t"])
        nonce = str(obj["n"])
        issued_at = int(obj["ts"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OAuthStateError("state payload is missing required fields") from exc

    current = int(time.time()) if now is None else now
    if current - issued_at > max_age_seconds:
        raise OAuthStateError("state has expired")
    # A state issued in the future (clock skew beyond a generous window)
    # is also suspect — reject it rather than trust an unbounded skew.
    if issued_at - current > max_age_seconds:
        raise OAuthStateError("state issued-at is implausibly in the future")

    return OAuthState(
        admin_id=admin_id,
        instance_id=instance_id,
        connection_type=connection_type,
        nonce=nonce,
        issued_at=issued_at,
    )
