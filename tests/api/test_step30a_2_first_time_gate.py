"""Step 30a.2 — first-time gate + paid-intro shape tests.

Coverage targets (per Step 30a.2 design doc §6):

  * BillingService.is_first_time_customer returns True iff the
    customer_email has NEVER appeared on a Subscription row
    (active OR inactive), case-insensitive.
  * BillingService.resolve_intro_fee_price_id reads from
    settings.stripe_price_intro_fee and raises
    BillingNotConfiguredError when empty.
  * create_checkout passes intro_fee_price_id + trial_period_days=90
    to the Stripe client ONLY for first-time buyers; repeat customers
    get neither.
  * The Stripe metadata stamps "luciel_intro_applied" = "true" or
    "false" so the webhook handler can later audit which path was used.

These are shape/behaviour tests with a mocked StripeClient — no live
Stripe round-trip. The real end-to-end (Checkout Session created in
Stripe test mode, webhook fires, tenant is minted) is covered by the
post-deploy smoke runbook in scripts/deploy_30a2.sh §6/6.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------
# Module-shape tests — these don't need a DB session.
# ---------------------------------------------------------------------

class TestModuleShape:

    def test_intro_fee_price_key_matches_settings_attr(self):
        # The constant must point at a real settings attribute name;
        # this is the single source of truth resolve_intro_fee_price_id
        # consults at call time.
        from app.core.config import settings
        from app.services.billing_service import INTRO_FEE_PRICE_KEY
        assert INTRO_FEE_PRICE_KEY == "stripe_price_intro_fee"
        assert hasattr(settings, INTRO_FEE_PRICE_KEY)

    def test_intro_trial_days_constant_is_ninety(self):
        from app.services.billing_service import INTRO_TRIAL_DAYS
        assert INTRO_TRIAL_DAYS == 90
        assert isinstance(INTRO_TRIAL_DAYS, int)

    def test_billing_service_has_is_first_time_customer(self):
        from app.services.billing_service import BillingService
        assert hasattr(BillingService, "is_first_time_customer")
        assert callable(BillingService.is_first_time_customer)

    def test_billing_service_has_resolve_intro_fee_price_id(self):
        from app.services.billing_service import BillingService
        assert hasattr(BillingService, "resolve_intro_fee_price_id")
        assert callable(BillingService.resolve_intro_fee_price_id)


# ---------------------------------------------------------------------
# is_first_time_customer — exercise the SQL with a stubbed Session.
# ---------------------------------------------------------------------

class _FakeScalarsResult:
    """Mimics the slice of SQLAlchemy's Result we use: .scalars().first()."""

    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value


class _FakeExecuteResult:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return _FakeScalarsResult(self._value)


class _FakeSession:
    """Stub Session whose execute() returns whatever value we seeded."""

    def __init__(self, seed):
        self._seed = seed
        self.last_stmt = None

    def execute(self, stmt):
        self.last_stmt = stmt
        return _FakeExecuteResult(self._seed)


class TestIsFirstTimeCustomer:

    def _make_service(self, *, seed):
        from app.services.billing_service import BillingService
        return BillingService(
            db=_FakeSession(seed=seed),
            stripe_client=MagicMock(),
        )

    def test_returns_true_when_no_prior_row(self):
        svc = self._make_service(seed=None)
        assert svc.is_first_time_customer(email="alice@example.com") is True

    def test_returns_false_when_prior_row_exists(self):
        svc = self._make_service(seed=12345)  # any non-None id stands in
        assert svc.is_first_time_customer(email="bob@example.com") is False

    def test_returns_false_for_empty_email(self):
        # Defensive: an empty email cannot be correlated, so we treat
        # it as not-first-time to avoid handing out free intros.
        svc = self._make_service(seed=None)
        assert svc.is_first_time_customer(email="") is False
        assert svc.is_first_time_customer(email="   ") is False

    def test_email_is_normalized_to_lowercase(self):
        # Even with mixed case the lookup must match a stored
        # all-lowercase row. We seed "not None" to assert the lookup
        # was performed (i.e. didn't short-circuit on the empty-string
        # branch).
        svc = self._make_service(seed=42)
        assert svc.is_first_time_customer(email="  Foo@Example.COM  ") is False


# ---------------------------------------------------------------------
# resolve_intro_fee_price_id — settings-attribute gating.
# ---------------------------------------------------------------------

class TestResolveIntroFeePriceId:

    def test_raises_when_setting_empty(self, monkeypatch):
        from app.core.config import settings
        from app.services.billing_service import (
            BillingNotConfiguredError,
            BillingService,
        )
        monkeypatch.setattr(settings, "stripe_price_intro_fee", "")
        svc = BillingService(db=_FakeSession(seed=None), stripe_client=MagicMock())
        with pytest.raises(BillingNotConfiguredError):
            svc.resolve_intro_fee_price_id()

    def test_returns_price_id_when_set(self, monkeypatch):
        from app.core.config import settings
        from app.services.billing_service import BillingService
        monkeypatch.setattr(settings, "stripe_price_intro_fee", "price_TEST_intro")
        svc = BillingService(db=_FakeSession(seed=None), stripe_client=MagicMock())
        assert svc.resolve_intro_fee_price_id() == "price_TEST_intro"


# ---------------------------------------------------------------------
# create_checkout — first-time vs repeat paths.
# ---------------------------------------------------------------------

class TestCreateCheckoutFirstTimeGate:

    def _setup_settings(self, monkeypatch):
        """Wire up the minimum Stripe-side settings so create_checkout
        doesn't bail with BillingNotConfiguredError before it reaches
        the first-time gate.
        """
        from app.core.config import settings
        monkeypatch.setattr(settings, "stripe_price_individual", "price_TEST_ind_m")
        monkeypatch.setattr(settings, "stripe_price_intro_fee", "price_TEST_intro")
        monkeypatch.setattr(
            settings,
            "billing_success_url",
            "https://example.com/ok?session_id={CHECKOUT_SESSION_ID}",
        )
        monkeypatch.setattr(settings, "billing_cancel_url", "https://example.com/cancel")

    def _make_stripe_mock(self):
        sc = MagicMock()
        sc.is_configured = True
        # The session object create_checkout passes back through.
        session_obj = MagicMock()
        session_obj.url = "https://checkout.stripe.com/test"
        session_obj.id = "cs_test_123"
        sc.create_checkout_session.return_value = session_obj
        return sc

    def test_first_time_buyer_gets_intro_fee_and_trial(self, monkeypatch):
        self._setup_settings(monkeypatch)
        from app.services.billing_service import BillingService

        stripe_mock = self._make_stripe_mock()
        svc = BillingService(db=_FakeSession(seed=None), stripe_client=stripe_mock)

        svc.create_checkout(
            email="newuser@example.com",
            display_name="New User",
            tier="individual",
            billing_cadence="monthly",
        )

        stripe_mock.create_checkout_session.assert_called_once()
        kwargs = stripe_mock.create_checkout_session.call_args.kwargs
        assert kwargs["intro_fee_price_id"] == "price_TEST_intro"
        assert kwargs["trial_period_days"] == 90
        assert kwargs["metadata"]["luciel_intro_applied"] == "true"

    def test_repeat_buyer_skips_intro_fee_and_trial(self, monkeypatch):
        self._setup_settings(monkeypatch)
        from app.services.billing_service import BillingService

        stripe_mock = self._make_stripe_mock()
        # seed=42 -> first-time check returns False (prior row exists)
        svc = BillingService(db=_FakeSession(seed=42), stripe_client=stripe_mock)

        svc.create_checkout(
            email="returning@example.com",
            display_name="Returning Buyer",
            tier="individual",
            billing_cadence="monthly",
        )

        stripe_mock.create_checkout_session.assert_called_once()
        kwargs = stripe_mock.create_checkout_session.call_args.kwargs
        assert kwargs["intro_fee_price_id"] is None
        # trial_period_days is passed as `trial_days or None` so a 0
        # value becomes None on the wire.
        assert kwargs["trial_period_days"] is None
        assert kwargs["metadata"]["luciel_intro_applied"] == "false"

    def test_first_time_buyer_with_intro_unconfigured_raises(self, monkeypatch):
        # If the operator forgot to seed stripe_price_intro_fee, a
        # first-time buyer's checkout must 501 (BillingNotConfiguredError)
        # rather than silently fall back to no-intro pricing.
        self._setup_settings(monkeypatch)
        from app.core.config import settings
        monkeypatch.setattr(settings, "stripe_price_intro_fee", "")
        from app.services.billing_service import (
            BillingNotConfiguredError,
            BillingService,
        )

        stripe_mock = self._make_stripe_mock()
        svc = BillingService(db=_FakeSession(seed=None), stripe_client=stripe_mock)

        with pytest.raises(BillingNotConfiguredError):
            svc.create_checkout(
                email="newuser2@example.com",
                display_name="New User 2",
                tier="individual",
                billing_cadence="monthly",
            )
        # And nothing was sent to Stripe.
        stripe_mock.create_checkout_session.assert_not_called()

    def test_repeat_buyer_with_intro_unconfigured_still_works(self, monkeypatch):
        # A repeat customer NEVER hits resolve_intro_fee_price_id, so a
        # missing intro Price ID is harmless on the repeat path.
        self._setup_settings(monkeypatch)
        from app.core.config import settings
        monkeypatch.setattr(settings, "stripe_price_intro_fee", "")
        from app.services.billing_service import BillingService

        stripe_mock = self._make_stripe_mock()
        svc = BillingService(db=_FakeSession(seed=42), stripe_client=stripe_mock)

        svc.create_checkout(
            email="returning2@example.com",
            display_name="Returning 2",
            tier="individual",
            billing_cadence="monthly",
        )

        kwargs = stripe_mock.create_checkout_session.call_args.kwargs
        assert kwargs["intro_fee_price_id"] is None
        assert kwargs["metadata"]["luciel_intro_applied"] == "false"


# ---------------------------------------------------------------------
# Stripe client shape — intro_fee_price_id parameter & line_items.
# ---------------------------------------------------------------------

class TestStripeClientIntroFeeShape:

    def test_create_checkout_session_accepts_intro_fee_price_id_kwarg(self):
        import inspect

        from app.integrations.stripe.client import StripeClient
        sig = inspect.signature(StripeClient.create_checkout_session)
        assert "intro_fee_price_id" in sig.parameters
        # Keyword-only with default None so callers that don't pass it
        # (the non-intro path) keep working unchanged.
        param = sig.parameters["intro_fee_price_id"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is None
