"""Unit tests for the email-shape gate added to TierProvisioningService.

Arc 3 Work-Unit C (2026-05-22) — pin the contract of
``_validate_email_shape`` so future drift on the synthetic-email
allow-list, the length cap, or the exception type is caught at unit-
test time instead of in the post-checkout webhook hot path.

Why pure-function tests (no DB, no service fixture)?

  * The validator is a pure helper. The branch table fits in one file
    and tests stay green without a DB.
  * The TierProvisioningService entry-point integration is covered by
    ``tests/api/test_step30a_1_tiered_self_serve_shape.py`` and the
    live e2e; this file pins the gate's own contract.

Coverage:

  * Valid: real-shape emails, synthetic-shape emails (the
    ``*.luciel.local`` and ``*.luciel.local`` patterns the resolver
    and onboarding mint).
  * Normalisation: case-folded + whitespace-stripped on the way out.
  * Rejection: None, non-str, empty, whitespace-only, missing ``@``,
    multiple ``@``, control-char, embedded space, oversize.
  * Exception class: subclass of ``ValueError`` (so the webhook's
    existing ``except ValueError`` trap path keeps catching).
"""

from __future__ import annotations

import pytest

from app.services.tier_provisioning_service import (
    TierProvisioningValidationError,
    _validate_email_shape,
    _EMAIL_MAX_LEN,
)


# ---------------------------------------------------------------------
# Acceptance set
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Real, RFC-shape emails.
        ("user@example.com", "user@example.com"),
        ("first.last@example.co.uk", "first.last@example.co.uk"),
        ("a+tag@sub.domain.example", "a+tag@sub.domain.example"),
        ("digits123@example.com", "digits123@example.com"),
        # Case-folding + whitespace strip.
        ("USER@EXAMPLE.COM", "user@example.com"),
        ("  user@example.com  ", "user@example.com"),
        ("\tuser@example.com\n", "user@example.com"),
        # Synthetic emails minted by Option B onboarding and identity
        # resolver -- MUST pass per app/models/user.py:14 and
        # app/identity/resolver.py:_SYNTHETIC_EMAIL_TEMPLATE.
        (
            "identity-7a4b8c19@tenant-acme.luciel.local",
            "identity-7a4b8c19@tenant-acme.luciel.local",
        ),
        (
            "agent-primary@tenant-foo.luciel.local",
            "agent-primary@tenant-foo.luciel.local",
        ),
    ],
)
def test_validate_email_shape_accepts_valid(raw: str, expected: str) -> None:
    assert _validate_email_shape(raw) == expected


def test_validate_email_shape_accepts_at_max_length() -> None:
    """Exactly _EMAIL_MAX_LEN chars must pass; one over must fail."""
    # Construct an email that is exactly _EMAIL_MAX_LEN chars long.
    domain = "@example.com"
    local_len = _EMAIL_MAX_LEN - len(domain)
    at_cap = ("a" * local_len) + domain
    assert len(at_cap) == _EMAIL_MAX_LEN
    assert _validate_email_shape(at_cap) == at_cap


# ---------------------------------------------------------------------
# Rejection set
# ---------------------------------------------------------------------


def test_validate_email_shape_rejects_none() -> None:
    with pytest.raises(TierProvisioningValidationError, match="required"):
        _validate_email_shape(None)


@pytest.mark.parametrize("bad_type", [123, 1.5, ["user@example.com"], {"x": "y"}, b"u@e.com"])
def test_validate_email_shape_rejects_non_str(bad_type: object) -> None:
    with pytest.raises(TierProvisioningValidationError, match="must be str"):
        _validate_email_shape(bad_type)  # type: ignore[arg-type]


@pytest.mark.parametrize("raw", ["", "   ", "\t\n", "  \r\n  "])
def test_validate_email_shape_rejects_empty_or_whitespace(raw: str) -> None:
    with pytest.raises(TierProvisioningValidationError, match="empty"):
        _validate_email_shape(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "no-at-sign",
        "no-tld@example",  # no dot in domain part
        "user@@example.com",  # double @
        "first@second@example.com",  # double @ separated
        "user @example.com",  # embedded space in local
        "user@exa mple.com",  # embedded space in domain
        "user\nname@example.com",  # embedded newline (control char)
        "user\t@example.com",  # embedded tab
        "@example.com",  # missing local
        "user@",  # missing domain
        "@",  # bare @
        ".",  # no @
    ],
)
def test_validate_email_shape_rejects_malformed(raw: str) -> None:
    with pytest.raises(TierProvisioningValidationError):
        _validate_email_shape(raw)


def test_validate_email_shape_rejects_oversize() -> None:
    """One char over _EMAIL_MAX_LEN must fail with a length-cap message."""
    domain = "@example.com"
    local_len = _EMAIL_MAX_LEN - len(domain) + 1  # one too many
    too_long = ("a" * local_len) + domain
    assert len(too_long) == _EMAIL_MAX_LEN + 1
    with pytest.raises(TierProvisioningValidationError, match="exceeds RFC 5321"):
        _validate_email_shape(too_long)


# ---------------------------------------------------------------------
# Exception-class contract
# ---------------------------------------------------------------------


def test_tier_provisioning_validation_error_subclasses_value_error() -> None:
    """The webhook traps ``except ValueError`` -- the new validator
    error MUST be catchable through that path. Pinning here so a
    future refactor that demotes the inheritance is caught.
    """
    assert issubclass(TierProvisioningValidationError, ValueError)


def test_tier_provisioning_validation_error_is_catchable_as_value_error() -> None:
    """End-to-end: raise from the validator, catch with the bare
    ``ValueError`` shape the webhook already uses. If this ever fails,
    the webhook trap will start swallowing real ValueErrors while
    letting validation failures bubble -- both sides of the bug are
    bad, so the test pins both.
    """
    try:
        _validate_email_shape("not-an-email")
    except ValueError as exc:
        assert isinstance(exc, TierProvisioningValidationError)
    else:  # pragma: no cover - guarded by raises path above
        pytest.fail("expected TierProvisioningValidationError")
