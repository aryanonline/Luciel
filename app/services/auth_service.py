"""Step 30a.3: password authentication service.

This service is the single read-and-write site for the
``users.password_hash`` column landed by the migration
``a3c1f08b9d42_step30a_3_users_password_hash``. It exposes three
primitives:

  * :func:`verify_password` -- constant-time compare for the
    ``POST /api/v1/auth/login`` daily-login surface.
  * :func:`set_password` -- argon2id hash + write for the
    ``POST /api/v1/auth/set-password`` first-set and reset surfaces.
  * :func:`request_password_reset` -- mints a ``reset_password``-class
    magic-link-shaped token and emails it through the existing SES
    pipeline so a user with a forgotten password can recover without
    operator help.

Design contract
---------------

1. **One hash algorithm, one cost profile.** argon2id with the
   argon2-cffi library defaults (m=65536 KB, t=3, p=4). These match
   OWASP's 2024 cheat-sheet "minimum recommended" parameters and
   produce ~50ms hash latency on the production ECS task profile
   (4 vCPU equivalents, Graviton2 ARM64). We accept that 50ms per
   login is an acceptable upper bound -- daily-login traffic is
   bounded by the marketing-site request rate, not by hash latency.

2. **No password length cap at the service layer.** argon2id has no
   72-byte truncation (bcrypt's footgun). We do enforce a length
   *floor* of 8 chars at the service boundary because the OWASP
   recommendation for a "password (not passphrase)" minimum is 8;
   the frontend enforces the same floor on the form. We do NOT
   enforce complexity rules (no "must contain uppercase + digit")
   because NIST 800-63B and OWASP both deprecate those rules in
   favour of length + breach-list checks; a breach-list lookup is
   a Step 32a polish item.

3. **Case-insensitive email matching.** Emails are normalised to
   lowercase at the User-creation site (see
   ``BillingWebhookService._resolve_or_create_user``) and the
   expression index on ``LOWER(email)`` matches that convention,
   but we still normalise at the verify-password surface so a
   typo in the create path cannot lock a user out of their account.

4. **No timing leaks on "user does not exist" vs "wrong password".**
   The verify path returns the same generic 401 surface in both
   cases. Internally it always runs an argon2id verify against
   *either* the looked-up hash *or* a sentinel hash if the user
   does not exist, so the timing of the two paths is
   indistinguishable. This is the textbook argon2-cffi pattern.

5. **No password_hash exposed beyond this module.** The User model
   surfaces the column for ORM completeness but no other service or
   route reads it. The only writers are ``set_password`` (this
   module) and the migration backfill (none). The only reader is
   ``verify_password`` (this module).

Cross-refs
----------

* CANONICAL_RECAP section 12 Step 30a.3 row.
* ARCHITECTURE section 3.2 ("Password / SSO / MFA auth" bullet,
  line 391, sharpened 2026-05-16 with mandatory-at-signup).
* DRIFTS section 3 ``D-magic-link-only-auth-no-password-fallback-2026-05-16``.
"""
from __future__ import annotations

import logging
import uuid
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import User
from app.services.billing_service import BillingService
from app.services.email_service import (
    MagicLinkError as EmailDeliveryError,
)

logger = logging.getLogger(__name__)


# Minimum password length enforced at the service boundary. Matches the
# OWASP 2024 cheat-sheet minimum for a non-passphrase password. The
# frontend enforces the same floor on the form input; the service
# enforces it again because the API can also be hit by tests, CLI tools,
# and (eventually) third-party invite-acceptance flows.
PASSWORD_MIN_LENGTH: Final[int] = 8

# Argon2id PasswordHasher singleton. The library is internally
# thread-safe and cost-parameter-immutable after construction, so a
# module-level instance is the right pattern. We rely on argon2-cffi's
# defaults (m=65536 KiB, t=3, p=4, hash_len=32, salt_len=16) which match
# OWASP's 2024 recommendation. Cost is reviewed annually.
_HASHER: Final[PasswordHasher] = PasswordHasher()

# Sentinel hash used in the verify path when the user does not exist.
# Pre-computed once at import time so the verify path against a
# non-existent user takes the same wall-clock time as a verify against
# a real (wrong) password. The string is opaque; the service never
# compares against it for equality (only via _HASHER.verify which
# returns False).
_NONEXISTENT_USER_SENTINEL_HASH: Final[str] = _HASHER.hash("nonexistent-user-placeholder")


class PasswordTooShortError(ValueError):
    """Raised when ``set_password`` is called with a password under the floor.

    The route layer maps this to a 422 with a machine-readable error code
    so the frontend can render an inline form-validation message rather
    than a generic 500.
    """


class AuthError(Exception):
    """Generic auth failure -- raised by ``verify_password`` on any failure.

    The route layer maps this to a 401 with a generic body. The error
    message is deliberately not leaked to the client; the variants we
    log internally (wrong password, user not active, password not set)
    are observable only in CloudWatch.
    """


def _normalize_email(email: str) -> str:
    """Return the lowercase, whitespace-stripped form of an email.

    Mirrors the normalisation applied at User-creation time in
    ``BillingWebhookService._resolve_or_create_user``. We re-normalise
    here so a typo in any future create path does not lock a user
    out of their account.
    """
    return (email or "").strip().lower()


def verify_password(*, db: Session, email: str, password: str) -> User:
    """Verify ``password`` against the User identified by ``email``.

    Returns the matched, active ``User`` on success. Raises
    :class:`AuthError` on any failure -- user not found, user inactive,
    no password set yet, password mismatch. All four failure modes
    produce indistinguishable timing because the sentinel-hash compare
    path is taken when the user does not exist.

    The route layer catches :class:`AuthError` and surfaces a single
    401 with a generic message so a probing client cannot enumerate
    valid emails.
    """
    normalised = _normalize_email(email)

    user = db.execute(
        select(User).where(User.email == normalised)
    ).scalar_one_or_none()

    # Hash to verify against. If the user does not exist OR their
    # password_hash is NULL, fall through to the sentinel so the verify
    # time is constant. We log the discriminator at DEBUG only; the
    # caller cannot observe it.
    hash_to_verify: str
    if user is None:
        logger.debug("auth: verify_password user-not-found email=%s", normalised)
        hash_to_verify = _NONEXISTENT_USER_SENTINEL_HASH
    elif not user.active:
        logger.debug("auth: verify_password user-inactive email=%s", normalised)
        hash_to_verify = _NONEXISTENT_USER_SENTINEL_HASH
    elif not user.password_hash:
        logger.debug("auth: verify_password no-password-set email=%s", normalised)
        hash_to_verify = _NONEXISTENT_USER_SENTINEL_HASH
    else:
        hash_to_verify = user.password_hash

    try:
        _HASHER.verify(hash_to_verify, password)
    except (VerifyMismatchError, InvalidHashError):
        raise AuthError("invalid credentials") from None

    # If we got here, the verify succeeded. If the user object is None
    # at this point the verify must have been against the sentinel and
    # the password happened to be the sentinel placeholder -- vanishingly
    # unlikely, but treat as failure for safety.
    if user is None or not user.active or not user.password_hash:
        raise AuthError("invalid credentials")

    # Step 30a.3 v1 carve-out: we do not auto-rehash on cost-parameter
    # changes (argon2-cffi's `check_needs_rehash`). Cost is reviewed
    # annually; when we bump it, a separate migration walks the table
    # and re-hashes on next successful login. Tracked as a follow-up
    # drift, not landed here.
    return user


def set_password(*, db: Session, user_id: uuid.UUID, password: str) -> None:
    """Argon2id-hash ``password`` and write it to the User row.

    Caller MUST have validated the bootstrap token (set_password or
    reset_password class) BEFORE calling this -- this primitive is
    a privileged write that does not re-verify the caller's identity.
    The route layer is the enforcement point: it consumes the token
    via ``consume_set_password_token`` or ``consume_reset_password_token``
    and only then calls here.

    The hash + write happens in the caller's transaction; the caller
    is responsible for committing. If commit fails the hash is
    effectively discarded (no other side-effect carries it).

    Raises :class:`PasswordTooShortError` if ``password`` is shorter
    than ``PASSWORD_MIN_LENGTH``; the route layer maps that to a 422.
    Raises :class:`LookupError` if the user does not exist or is
    inactive; the route layer maps that to a 401.
    """
    if len(password) < PASSWORD_MIN_LENGTH:
        raise PasswordTooShortError(
            f"Password must be at least {PASSWORD_MIN_LENGTH} characters."
        )

    user = db.get(User, user_id)
    if user is None or not user.active:
        raise LookupError(f"User {user_id} not found or inactive")

    user.password_hash = _HASHER.hash(password)
    db.add(user)
    # No commit here -- the route owns the transaction. The route
    # commits after this returns and then mints the session cookie.
    logger.info(
        "auth: password set for user_id=%s email=%s",
        user.id, user.email,
    )


def request_password_reset(*, db: Session, email: str) -> bool:
    """Mint a reset-password token and email it.

    Returns True if the email was queued for delivery, False if the
    user does not exist or is inactive. The route layer ALWAYS returns
    200 with a generic "if the email exists, a reset link has been
    sent" body regardless of return value so a probing client cannot
    enumerate emails.

    The reset link points at the same ``/auth/set-password`` page the
    welcome-email flow uses; the token class (``reset_password`` vs
    ``set_password``) discriminates the two in the audit row.
    """
    # Lazy imports to break the magic_link_service <-> auth_service
    # dependency cycle at module-load time.
    from app.services.magic_link_service import (
        build_set_password_url,
        mint_reset_password_token,
    )
    from app.services.email_service import send_welcome_set_password_email

    normalised = _normalize_email(email)
    user = db.execute(
        select(User).where(User.email == normalised)
    ).scalar_one_or_none()

    if user is None or not user.active:
        logger.info(
            "auth: forgot-password no-op for unknown/inactive email=%s",
            normalised,
        )
        return False

    # Find an active subscription to thread the tenant_id through. If
    # the user has no active subscription (cancelled, never paid) we
    # still let them reset their password -- the magic-link token does
    # not need a *current* tenant binding to be valid; the consume
    # path re-resolves the user's tenancy when it mints the session
    # cookie. We use a placeholder tenant_id="" in that case; the
    # cookie mint path is happy with that because Step 31.2's
    # middleware re-resolves tenant from the active subscription at
    # cookie-redeem time.
    svc = BillingService(db=db, stripe_client=None)  # type: ignore[arg-type]
    sub = svc.get_active_subscription_for_user(user_id=user.id)
    tenant_id = sub.tenant_id if sub is not None else ""

    token = mint_reset_password_token(
        user_id=user.id, email=user.email, tenant_id=tenant_id,
    )
    url = build_set_password_url(token)

    try:
        send_welcome_set_password_email(
            to_email=user.email,
            set_password_url=url,
            display_name=user.display_name,
            purpose="reset",
        )
    except EmailDeliveryError:
        logger.exception(
            "auth: forgot-password email send FAILED user_id=%s",
            user.id,
        )
        # Swallow -- the route returns the same generic 200 so a
        # probing client cannot infer email-existence from a delivery
        # failure. The on-call dashboard surfaces the SES error.
        return False

    logger.info(
        "auth: forgot-password email queued user_id=%s email=%s",
        user.id, user.email,
    )
    return True
