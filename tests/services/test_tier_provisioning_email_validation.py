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

from unittest.mock import patch

from app.services.tier_provisioning_service import (
    DELIVERABILITY_BYPASS_DISABLED,
    DELIVERABILITY_BYPASS_SYNTHETIC,
    DELIVERABILITY_ERROR,
    DELIVERABILITY_FAILED,
    DELIVERABILITY_OK,
    TierProvisioningValidationError,
    _check_email_deliverability,
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


# ---------------------------------------------------------------------
# Arc 8 Commit 2 -- deliverability gate
# ---------------------------------------------------------------------
#
# These tests cover the second of the two pre-mint email gates: the
# MX-record lookup that closes
# ``D-stripe-checkout-no-email-validation-2026-05-18``. The contract is
# documented on ``_check_email_deliverability`` itself; the tests below
# pin the five branches without making real DNS queries, so they stay
# green in the no-DNS CI sandbox.
#
# Patching strategy: ``settings.email_deliverability_check_enabled`` is
# the kill-switch and is the easiest place to force the disabled-branch
# path. The ``email_validator`` import is patched per-test (rather than
# at module level) so the soft-pass branch can assert behaviour even
# when the library is installed locally.


class _FakeSettings:
    """Minimal stand-in for ``app.core.config.settings`` -- only the
    two attributes the gate reads. Keeps the test isolated from real
    env vars.
    """

    def __init__(self, *, enabled: bool = True, timeout: float = 2.0) -> None:
        self.email_deliverability_check_enabled = enabled
        self.email_deliverability_check_timeout_seconds = timeout


def test_deliverability_bypasses_synthetic_luciel_local() -> None:
    """Synthetic ``*.luciel.local`` addresses must skip the MX lookup
    unconditionally -- they are real internal identifiers, not
    deliverable external addresses. If this ever flips, every Option-B
    onboarding intermediate state and every anon-resolver mint would
    log a false deliverability warning at signup.
    """
    fake = _FakeSettings(enabled=True)
    with patch("app.core.config.settings", fake):
        status, detail = _check_email_deliverability(
            "identity-7a4b8c19@tenant-acme.luciel.local"
        )
    assert status == DELIVERABILITY_BYPASS_SYNTHETIC
    assert detail is None


def test_deliverability_respects_kill_switch() -> None:
    """With the feature flag off the gate must return
    ``DELIVERABILITY_BYPASS_DISABLED`` without touching the
    email_validator library at all. We assert the no-import path by
    forcing an import error in the validator module and confirming
    the function still returns the disabled-bypass sentinel
    (i.e. it short-circuits BEFORE the import).
    """
    fake = _FakeSettings(enabled=False)
    with patch("app.core.config.settings", fake):
        # If the function tried to import email_validator we would
        # land in the ERROR branch (import_failed); the disabled
        # branch is the only way to see BYPASS_DISABLED here.
        with patch.dict(
            "sys.modules", {"email_validator": None}
        ):  # would force ImportError on real import
            status, detail = _check_email_deliverability(
                "someone@example-domain-that-might-fail.tld"
            )
    assert status == DELIVERABILITY_BYPASS_DISABLED
    assert detail is None


def test_deliverability_returns_ok_when_validator_passes() -> None:
    """Happy-path: stubbed ``validate_email`` does not raise -> the
    gate reports ``DELIVERABILITY_OK`` with no detail. The real DNS
    path is exercised in live smoke; here we pin the wiring.
    """
    fake = _FakeSettings(enabled=True)
    with patch("app.core.config.settings", fake):
        with patch(
            "email_validator.validate_email",
            return_value=object(),  # arbitrary truthy return
        ):
            status, detail = _check_email_deliverability(
                "real-customer@example.com"
            )
    assert status == DELIVERABILITY_OK
    assert detail is None


def test_deliverability_reports_failed_on_typo_injection() -> None:
    """The drift was raised against a real-customer typo at Stripe
    checkout (``yourdomain.com`` placeholder accepted as-is). We
    simulate the same shape: ``validate_email`` raises
    ``EmailNotValidError`` -> gate reports ``DELIVERABILITY_FAILED``
    with the exception class name as detail (machine-friendly).
    """
    from email_validator import EmailNotValidError

    fake = _FakeSettings(enabled=True)

    def _raise_nx(*_args, **_kwargs):
        raise EmailNotValidError(
            "The domain name yourdomain.com does not exist."
        )

    with patch("app.core.config.settings", fake):
        with patch("email_validator.validate_email", side_effect=_raise_nx):
            status, detail = _check_email_deliverability(
                "aryan+smoke-30a5@yourdomain.com"
            )
    assert status == DELIVERABILITY_FAILED
    # Detail is the exception class name -- machine-friendly and
    # PII-safe (no domain leak, no exception message body).
    assert detail == "EmailNotValidError"


def test_deliverability_soft_passes_on_resolver_error() -> None:
    """Transient resolver fault (SERVFAIL, timeout, socket error)
    must soft-pass: gate reports ``DELIVERABILITY_ERROR`` but does
    NOT raise. Stripe has collected payment by this point; the
    customer must not be punished for a network blip.
    """
    fake = _FakeSettings(enabled=True)

    def _raise_timeout(*_args, **_kwargs):
        raise TimeoutError("resolver flapped")

    with patch("app.core.config.settings", fake):
        with patch("email_validator.validate_email", side_effect=_raise_timeout):
            status, detail = _check_email_deliverability(
                "real-customer@example.com"
            )
    assert status == DELIVERABILITY_ERROR
    assert detail == "TimeoutError"


def test_deliverability_never_raises_on_unexpected_exception() -> None:
    """Defensive contract pin: even a completely unexpected exception
    class from inside ``validate_email`` must be trapped. The gate
    is a soft signal, not a blocking validator -- one bad day in the
    email_validator library upstream must not abort every pre-mint.
    """
    fake = _FakeSettings(enabled=True)

    def _raise_weird(*_args, **_kwargs):
        raise RuntimeError("surprise from upstream")

    with patch("app.core.config.settings", fake):
        with patch("email_validator.validate_email", side_effect=_raise_weird):
            status, detail = _check_email_deliverability(
                "real-customer@example.com"
            )
    assert status == DELIVERABILITY_ERROR
    assert detail == "RuntimeError"


# ---------------------------------------------------------------------
# Arc 8 Commit 2 -- premint_for_tier kwarg alias
# ---------------------------------------------------------------------
#
# Closes ``D-tier-provisioning-tenant-id-kwarg-mismatch-2026-05-24``
# (discovered during C2 recon). Both production callers --
# ``BillingWebhookService._on_checkout_completed`` and
# ``api.v1.billing.signup_free`` -- pass ``tenant_id=`` instead of the
# new ``admin_id=``. We accept both kwargs so neither caller breaks.
# These tests pin the alias contract without needing a real DB.


def test_premint_for_tier_accepts_tenant_id_alias() -> None:
    """Calling ``premint_for_tier(tenant_id=...)`` must NOT raise
    ``TypeError`` on the kwarg shape -- the alias is the gate that
    keeps every paid/free signup from silently failing.

    We do not exercise the full pre-mint walk here (that needs a real
    DB session); we only assert the kwarg-acceptance shape by letting
    the call fail downstream of the kwarg gate (i.e. on an
    intentionally-missing admin row) and confirming the failure is
    NOT the kwarg-mismatch TypeError.
    """
    from unittest.mock import MagicMock

    from app.services.tier_provisioning_service import TierProvisioningService

    svc = TierProvisioningService(MagicMock())
    # Stub the admin lookup to return None so the call short-circuits
    # after the kwarg gate with a clean ValueError. If the kwarg
    # alias is broken we would see TypeError instead.
    svc.admin = MagicMock()
    svc.admin.get_tenant_config.return_value = None

    primary_user = MagicMock()
    primary_user.email = "real@example.com"

    fake_settings = _FakeSettings(enabled=False)  # bypass MX lookup
    with patch("app.core.config.settings", fake_settings):
        with pytest.raises(ValueError, match="missing or inactive"):
            svc.premint_for_tier(
                tenant_id="admin-abc123",  # old kwarg name
                tier="free",
                primary_user=primary_user,
                audit_ctx=MagicMock(),
            )


def test_premint_for_tier_accepts_admin_id_canonical() -> None:
    """The canonical post-Arc-5 kwarg ``admin_id`` must also work --
    same downstream behaviour, no regressions on the new shape.
    """
    from unittest.mock import MagicMock

    from app.services.tier_provisioning_service import TierProvisioningService

    svc = TierProvisioningService(MagicMock())
    svc.admin = MagicMock()
    svc.admin.get_tenant_config.return_value = None

    primary_user = MagicMock()
    primary_user.email = "real@example.com"

    fake_settings = _FakeSettings(enabled=False)
    with patch("app.core.config.settings", fake_settings):
        with pytest.raises(ValueError, match="missing or inactive"):
            svc.premint_for_tier(
                admin_id="admin-abc123",  # new kwarg name
                tier="free",
                primary_user=primary_user,
                audit_ctx=MagicMock(),
            )


def test_premint_for_tier_rejects_missing_admin_id_and_tenant_id() -> None:
    """Neither kwarg supplied -> ``TypeError`` with a clear message.
    This is programmer error (not a runtime/customer-facing failure)
    and the type-error class is what the rest of the codebase
    expects for missing-required-kwarg programmer errors.
    """
    from unittest.mock import MagicMock

    from app.services.tier_provisioning_service import TierProvisioningService

    svc = TierProvisioningService(MagicMock())
    primary_user = MagicMock()
    primary_user.email = "real@example.com"

    fake_settings = _FakeSettings(enabled=False)
    with patch("app.core.config.settings", fake_settings):
        with pytest.raises(TypeError, match="admin_id= or tenant_id= must be supplied"):
            svc.premint_for_tier(
                tier="free",
                primary_user=primary_user,
                audit_ctx=MagicMock(),
            )
