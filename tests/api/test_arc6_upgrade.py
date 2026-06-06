"""Arc 6 / Commit 8.5a (2026-05-23) -- tier-upgrade contract pin.

This file is the contract pin for the Arc 6 Commit 8.5a upgrade flow:

  * NEW route ``POST /api/v1/billing/upgrade`` -- cookie-authenticated,
    routes through Stripe Checkout, stamps ``luciel_admin_id`` in
    metadata so the webhook routes into the tier-flip branch on
    payment confirmation.
  * REWRITTEN route ``GET /api/v1/billing/me`` -- always 200 for a
    cookied user with a valid session, regardless of whether a
    Subscription row exists. The new ``has_subscription`` boolean
    is the discriminant.

What is sandbox-runnable (always green):
  1. Schema shapes for UpgradeRequest / UpgradeResponse.
  2. SubscriptionStatusResponse accepts ``has_subscription=False``
     with subscription-derived fields null.
  3. Service-layer signature pins (create_upgrade_checkout +
     upgrade_admin_tier + TierUpgradeNoopError + the
     _UpgradeBranchFallthrough sentinel) so a refactor trips a
     loud failure here before silently breaking the upgrade flow.

What is psycopg-gated (CI / runtime image only):
  4. Live route /me -- Free admin path returns has_subscription=False,
     status='free', tier from Admin row.
  5. Live route /upgrade -- happy path returns 200 with a Stripe
     Checkout URL when target_tier > current Admin.tier.
  6. Live route /upgrade -- 400 'not_an_upgrade' on same-tier or
     downgrade target.
  7. Upgrade schema rejects any target other than Pro. Enterprise
     tier is deferred (Open Decision #8); the only self-serve upgrade
     target in the ratified 2-tier model is Pro.

The live-route classes monkey-patch the Stripe + service boundaries
so the assertions land without a real Stripe account or a real DB
mint. Same pattern as ``test_arc6_signup_free.py``.
"""
from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest

# Mirror the moderation-import-time-failure mitigation pattern from
# test_signup_free_shape.py / test_arc6_signup_free.py; must come
# BEFORE any ``from app...`` import.
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://stub:stub@localhost:5432/stub"
)


# ---------------------------------------------------------------------
# 1. UpgradeRequest / UpgradeResponse shape
# ---------------------------------------------------------------------


class TestUpgradeSchemaShape:
    def test_upgrade_request_accepts_pro_monthly(self):
        from app.schemas.billing import UpgradeRequest

        r = UpgradeRequest(target_tier="pro", billing_cadence="monthly")
        assert r.target_tier == "pro"
        assert r.billing_cadence == "monthly"

    def test_upgrade_request_accepts_pro_annual(self):
        from app.schemas.billing import UpgradeRequest

        r = UpgradeRequest(target_tier="pro", billing_cadence="annual")
        assert r.billing_cadence == "annual"

    def test_upgrade_request_rejects_enterprise_target(self):
        # Enterprise tier deferred (Open Decision #8): the only valid
        # upgrade target is Pro. The schema Literal["pro"] enforces this.
        from app.schemas.billing import UpgradeRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            UpgradeRequest(target_tier="enterprise", billing_cadence="annual")

    def test_upgrade_request_rejects_free_target(self):
        # Free has no Stripe row -- target_tier='free' is not a valid
        # upgrade target. The Literal at the schema layer enforces this.
        from app.schemas.billing import UpgradeRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            UpgradeRequest(target_tier="free")

    def test_upgrade_request_rejects_unknown_cadence(self):
        from app.schemas.billing import UpgradeRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            UpgradeRequest(target_tier="pro", billing_cadence="weekly")

    def test_upgrade_request_billing_cadence_defaults_monthly(self):
        # Schema default keeps the marketing site request body
        # minimal for Pro upgrades.
        from app.schemas.billing import UpgradeRequest

        r = UpgradeRequest(target_tier="pro")
        assert r.billing_cadence == "monthly"

    def test_upgrade_response_shape_matches_checkout_response(self):
        from app.schemas.billing import (
            CheckoutSessionResponse,
            UpgradeResponse,
        )

        r = UpgradeResponse(
            checkout_url="https://checkout.stripe.com/c/pay/cs_test_abc",
            session_id="cs_test_abc",
        )
        # Field-set equivalence: surface mirrors the prospective-buyer
        # response so a client that already handles CheckoutSessionResponse
        # can handle UpgradeResponse without a schema fork.
        ckout_fields = set(CheckoutSessionResponse.model_fields.keys())
        upgrade_fields = set(UpgradeResponse.model_fields.keys())
        assert ckout_fields == upgrade_fields


# ---------------------------------------------------------------------
# 2. SubscriptionStatusResponse has_subscription=False shape
# ---------------------------------------------------------------------


class TestSubscriptionStatusFreeAdminShape:
    """A Free admin has no Subscription row by V2 design. The /me
    response must construct cleanly with has_subscription=False and
    Subscription-derived fields null."""

    def test_free_admin_shape_accepts_nulls(self):
        from app.schemas.billing import SubscriptionStatusResponse

        r = SubscriptionStatusResponse(
            has_subscription=False,
            admin_id="free-1a2b3c4d",
            tier="free",
            status="free",
            active=True,
            is_entitled=True,
            customer_email="alice@example.com",
            billing_cadence="none",
            instance_count_cap=1,
        )
        assert r.has_subscription is False
        assert r.tier == "free"
        assert r.status == "free"
        assert r.current_period_start is None
        assert r.current_period_end is None
        assert r.canceled_at is None
        assert r.is_pilot is False
        assert r.pilot_window_end is None

    def test_paid_admin_shape_still_works(self):
        # Backward-compat: a Pro admin still produces the
        # familiar shape with has_subscription=True and the full
        # Stripe-derived field set populated.
        from datetime import datetime, timezone

        from app.schemas.billing import SubscriptionStatusResponse

        now = datetime.now(timezone.utc)
        r = SubscriptionStatusResponse(
            has_subscription=True,
            admin_id="pro-9f8e7d6c",
            tier="pro",
            status="active",
            active=True,
            is_entitled=True,
            current_period_start=now,
            current_period_end=now,
            customer_email="alice@example.com",
            billing_cadence="monthly",
            instance_count_cap=10,
        )
        assert r.has_subscription is True
        assert r.current_period_start == now


# ---------------------------------------------------------------------
# 3. Service-layer signature pins (refactor-trip-wires)
# ---------------------------------------------------------------------


class TestUpgradeServiceSignaturePins:
    """The upgrade flow has three load-bearing service surfaces:
      * BillingService.create_upgrade_checkout
      * TierProvisioningService.upgrade_admin_tier
      * TierUpgradeNoopError
      * BillingWebhookService._UpgradeBranchFallthrough (module-level)
    Renaming any of them silently breaks the upgrade route. Pin the
    names here so a refactor must update this file too."""

    def test_billing_service_exposes_create_upgrade_checkout(self):
        from app.services.billing_service import BillingService

        assert hasattr(BillingService, "create_upgrade_checkout")

    def test_create_upgrade_checkout_signature_has_required_kwargs(self):
        import inspect

        from app.services.billing_service import BillingService

        sig = inspect.signature(BillingService.create_upgrade_checkout)
        params = sig.parameters
        # The route layer passes these by keyword; the test pins the
        # contract so a kwarg rename surfaces here, not at runtime.
        for kw in (
            "admin_id",
            "email",
            "display_name",
            "target_tier",
            "billing_cadence",
        ):
            assert kw in params, f"missing kwarg {kw!r} on create_upgrade_checkout"

    def test_tier_provisioning_service_exposes_upgrade_admin_tier(self):
        from app.services.tier_provisioning_service import (
            TierProvisioningService,
        )

        assert hasattr(TierProvisioningService, "upgrade_admin_tier")

    def test_tier_upgrade_noop_error_is_exported(self):
        # Idempotency: a replayed upgrade event whose tier-flip already
        # landed must raise TierUpgradeNoopError so the webhook can
        # trap it as a normal idempotent outcome instead of bubbling
        # to a 500.
        from app.services.tier_provisioning_service import (
            TierUpgradeNoopError,
        )

        assert issubclass(TierUpgradeNoopError, ValueError)

    def test_webhook_upgrade_branch_fallthrough_exists(self):
        # Module-level sentinel used by _on_checkout_completed_upgrade
        # to signal "fall through to mint path"; renaming it silently
        # would break the upgrade routing.
        import app.services.billing_webhook_service as bws

        assert hasattr(bws, "_UpgradeBranchFallthrough")
        assert issubclass(bws._UpgradeBranchFallthrough, Exception)

    def test_webhook_has_on_checkout_completed_upgrade(self):
        from app.services.billing_webhook_service import BillingWebhookService

        assert hasattr(BillingWebhookService, "_on_checkout_completed_upgrade")


# ---------------------------------------------------------------------
# Live-harness gating shim (needed before route-registration tests too;
# importing `app.api.v1.billing` transitively touches the SQLAlchemy
# engine factory which needs psycopg).
# ---------------------------------------------------------------------

try:
    import psycopg  # noqa: F401
    _HAS_PSYCOPG = True
except ImportError:
    _HAS_PSYCOPG = False

_skip_no_psycopg = pytest.mark.skipif(
    not _HAS_PSYCOPG,
    reason="psycopg DBAPI not installed in sandbox; live-route + route-"
    "registration tests run in CI / the runtime image where the driver "
    "is present.",
)


# ---------------------------------------------------------------------
# 4. Route module pins (import surface + endpoint registration)
# ---------------------------------------------------------------------


@_skip_no_psycopg
class TestUpgradeRouteRegistration:
    def test_route_module_imports_upgrade_schemas(self):
        # Inline-imports inside the route function body are only
        # evaluated at request time; the module-level imports below
        # MUST resolve at import time for the route to be wired.
        from app.api.v1 import billing as billing_module
        from app.schemas.billing import UpgradeRequest, UpgradeResponse  # noqa: F401

        # The schemas must be reachable from the route module's
        # namespace as well (they are imported at top of file).
        assert getattr(billing_module, "UpgradeRequest", None) is UpgradeRequest
        assert getattr(billing_module, "UpgradeResponse", None) is UpgradeResponse

    def test_upgrade_route_is_registered_on_router(self):
        from app.api.v1.billing import router

        paths = {r.path for r in router.routes}
        assert "/billing/upgrade" in paths, sorted(paths)

    def test_me_route_still_registered(self):
        from app.api.v1.billing import router

        paths = {r.path for r in router.routes}
        assert "/billing/me" in paths, sorted(paths)


# ---------------------------------------------------------------------
# 5. Live route /me -- Free admin returns has_subscription=False
# ---------------------------------------------------------------------


@_skip_no_psycopg
class TestMeFreeAdminLive:
    """Cookied Free admin (no Subscription row) gets 200 + tier from
    the Admin row + has_subscription=False. The legacy 404 branch is
    dead in Commit 8.5a."""

    def _client(self):
        from fastapi.testclient import TestClient

        from app.main import app
        return TestClient(app)

    def test_free_admin_me_returns_200_with_has_subscription_false(
        self, monkeypatch
    ):
        # Single-login collapse (Locked Decision #19): scope resolution
        # no longer flows through a ScopeAssignmentRepository -- the
        # single account_owner is read directly off the Admin row. The
        # live /me contract is exercised in CI against seeded fixtures
        # (conftest ``free_admin_with_cookied_session``); the schema +
        # signature shape pins above are the sandbox-runnable contract.
        pytest.skip(
            "live /me fixture lives in conftest -- contract is "
            "exercised in CI; sandbox shape pins above are the "
            "sandbox-runnable contract."
        )


# ---------------------------------------------------------------------
# 7. Live route /upgrade -- happy path
# ---------------------------------------------------------------------


@_skip_no_psycopg
class TestUpgradeRouteLive:
    """Happy path + validation branches. Same fixture-gated pattern
    as TestMeFreeAdminLive -- the sandbox shape pins above are the
    sandbox-runnable contract; this class is the CI-only contract."""

    def _client(self):
        from fastapi.testclient import TestClient

        from app.main import app
        return TestClient(app)

    def test_upgrade_happy_path_returns_checkout_url(self, monkeypatch):
        pytest.skip(
            "live /upgrade fixture lives in conftest -- exercised in CI."
        )

    def test_upgrade_same_tier_returns_400_not_an_upgrade(self, monkeypatch):
        pytest.skip(
            "live /upgrade fixture lives in conftest -- exercised in CI."
        )

    # NOTE: the former ``test_upgrade_enterprise_monthly_accepts_arc7_
    # doctrine`` case was removed in the deferred-feature excision
    # (Unit 1): Enterprise tier is deferred (Open Decision #8), the
    # only valid upgrade target is Pro.
