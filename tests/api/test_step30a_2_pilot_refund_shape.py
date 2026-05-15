"""Step 30a.2-pilot — pilot-refund route + service shape pins.

Coverage targets (per Step 30a.2-pilot Commit 3b spec):

  * Route POST /api/v1/billing/pilot-refund is mounted on the router
    with response_model=PilotRefundResponse and status_code=200.
  * BillingService exposes process_pilot_refund(user=...).
  * Audit constant ACTION_SUBSCRIPTION_PILOT_REFUNDED is present in
    ALLOWED_ACTIONS and is short enough for the action column.
  * The Stripe client exposes find_intro_charge_id, refund_charge,
    and cancel_subscription helpers required by the service.
  * The three pilot-refund error classes (NotFirstTimePilotError,
    PilotWindowExpiredError, PilotChargeNotFoundError) exist on the
    billing_service module and the route maps them to 403 / 409 / 404
    respectively.
  * PilotRefundResponse carries the six locked fields with stable names.

These are shape tests; the live end-to-end refund (Stripe test-mode
Checkout + day-91 test-clock + self-refund) lives in
tests/e2e/step_30a_live_e2e.py scenario_8.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------
# Route + schema shape
# ---------------------------------------------------------------------

class TestRouteShape:

    def test_router_has_pilot_refund_post_route(self):
        # The billing router is mounted under prefix='/billing' (see
        # APIRouter init in app/api/v1/billing.py); the full mounted
        # path is '/billing/pilot-refund'.
        from app.api.v1.billing import router
        paths = {(r.path, tuple(sorted(r.methods))) for r in router.routes}
        assert ("/billing/pilot-refund", ("POST",)) in paths, (
            f"POST /billing/pilot-refund not mounted; have: {paths}"
        )

    def test_pilot_refund_response_schema_fields(self):
        from app.schemas.billing import PilotRefundResponse
        fields = set(PilotRefundResponse.model_fields.keys())
        expected = {
            "refund_id",
            "charge_id",
            "refunded_amount_cents",
            "currency",
            "tenant_id",
            "deactivated_at",
        }
        assert expected.issubset(fields), (
            f"PilotRefundResponse missing fields: {expected - fields}"
        )

    def test_pilot_refund_response_validates_locked_payload(self):
        from datetime import datetime, timezone
        from app.schemas.billing import PilotRefundResponse

        resp = PilotRefundResponse(
            refund_id="re_test_abc",
            charge_id="ch_test_xyz",
            refunded_amount_cents=10000,
            currency="cad",
            tenant_id="t_pilot",
            deactivated_at=datetime.now(timezone.utc),
        )
        assert resp.refunded_amount_cents == 10000
        assert resp.currency == "cad"


# ---------------------------------------------------------------------
# Service surface
# ---------------------------------------------------------------------

class TestServiceShape:

    def test_billing_service_has_process_pilot_refund(self):
        from app.services.billing_service import BillingService
        assert hasattr(BillingService, "process_pilot_refund")
        assert callable(BillingService.process_pilot_refund)

    def test_billing_service_locks_refund_amount_to_100_cad(self):
        from app.services.billing_service import BillingService
        # The amount + currency are the canon-locked policy
        # (CANONICAL_RECAP §14 ¶273). Pin them as class attrs so a
        # future code change cannot silently drift the refund value.
        assert BillingService._PILOT_REFUND_AMOUNT_CENTS == 10000
        assert BillingService._PILOT_REFUND_CURRENCY == "cad"

    def test_error_classes_exist(self):
        from app.services import billing_service as bs
        assert hasattr(bs, "NotFirstTimePilotError")
        assert hasattr(bs, "PilotWindowExpiredError")
        assert hasattr(bs, "PilotChargeNotFoundError")
        # All three must inherit from Exception (route layer raises
        # HTTPException after catching them; if they were not Exception
        # subclasses Python would raise TypeError on the except clause).
        for name in (
            "NotFirstTimePilotError",
            "PilotWindowExpiredError",
            "PilotChargeNotFoundError",
        ):
            cls = getattr(bs, name)
            assert isinstance(cls, type) and issubclass(cls, Exception)


# ---------------------------------------------------------------------
# Audit constant
# ---------------------------------------------------------------------

class TestAuditShape:

    def test_pilot_refund_action_constant_value(self):
        from app.models.admin_audit_log import ACTION_SUBSCRIPTION_PILOT_REFUNDED
        # The exact string is locked: a search tool in the audit
        # dashboard filters on this value, and changing it would
        # invalidate the row_hash chain for any rows already written.
        assert ACTION_SUBSCRIPTION_PILOT_REFUNDED == "subscription_pilot_refunded"

    def test_pilot_refund_action_is_in_allowlist(self):
        from app.models.admin_audit_log import (
            ACTION_SUBSCRIPTION_PILOT_REFUNDED,
            ALLOWED_ACTIONS,
        )
        assert ACTION_SUBSCRIPTION_PILOT_REFUNDED in ALLOWED_ACTIONS

    def test_pilot_refund_action_fits_action_column(self):
        # admin_audit_logs.action is String(64) (widened from 30 in
        # step29x migration a1f29c7e4b08). The pilot-refund constant
        # is 27 chars; pin it so a future rename can't accidentally
        # bust the column.
        from app.models.admin_audit_log import ACTION_SUBSCRIPTION_PILOT_REFUNDED
        assert len(ACTION_SUBSCRIPTION_PILOT_REFUNDED) <= 64


# ---------------------------------------------------------------------
# Stripe client helpers required by process_pilot_refund
# ---------------------------------------------------------------------

class TestStripeClientShape:

    def test_stripe_client_has_find_intro_charge_id(self):
        from app.integrations.stripe.client import StripeClient
        assert hasattr(StripeClient, "find_intro_charge_id")
        assert callable(StripeClient.find_intro_charge_id)

    def test_stripe_client_has_refund_charge(self):
        from app.integrations.stripe.client import StripeClient
        assert hasattr(StripeClient, "refund_charge")
        assert callable(StripeClient.refund_charge)

    def test_stripe_client_has_cancel_subscription(self):
        from app.integrations.stripe.client import StripeClient
        assert hasattr(StripeClient, "cancel_subscription")
        assert callable(StripeClient.cancel_subscription)


# ---------------------------------------------------------------------
# Route -> error mapping (smoke; uses a fully-mocked service)
# ---------------------------------------------------------------------

class TestRouteErrorMapping:
    """Each service-layer error must surface as the documented HTTP code.

    We assemble the FastAPI app, plant a session cookie, and replace
    the BillingService dependency with a fake whose process_pilot_refund
    raises each error in turn. The status code on the response is the
    contract we are pinning.
    """

    def _client_with_cookied_user(self, monkeypatch, fake_service):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.api.v1 import billing as billing_module

        # Bypass auth: replace _resolve_cookied_user with a stub.
        fake_user = MagicMock()
        fake_user.id = "u_test"
        monkeypatch.setattr(
            billing_module,
            "_resolve_cookied_user",
            lambda *, db, session_cookie: fake_user,
        )
        # Replace the service factory so the route picks up our fake.
        monkeypatch.setattr(billing_module, "_service", lambda db: fake_service)
        return TestClient(app)

    def test_not_first_time_returns_403(self, monkeypatch):
        from app.services.billing_service import NotFirstTimePilotError
        fake = MagicMock()
        fake.process_pilot_refund.side_effect = NotFirstTimePilotError("nope")
        client = self._client_with_cookied_user(monkeypatch, fake)
        resp = client.post("/api/v1/billing/pilot-refund")
        assert resp.status_code == 403, resp.text

    def test_window_expired_returns_409(self, monkeypatch):
        from app.services.billing_service import PilotWindowExpiredError
        fake = MagicMock()
        fake.process_pilot_refund.side_effect = PilotWindowExpiredError("closed")
        client = self._client_with_cookied_user(monkeypatch, fake)
        resp = client.post("/api/v1/billing/pilot-refund")
        assert resp.status_code == 409, resp.text

    def test_charge_not_found_returns_404(self, monkeypatch):
        from app.services.billing_service import PilotChargeNotFoundError
        fake = MagicMock()
        fake.process_pilot_refund.side_effect = PilotChargeNotFoundError("gone")
        client = self._client_with_cookied_user(monkeypatch, fake)
        resp = client.post("/api/v1/billing/pilot-refund")
        assert resp.status_code == 404, resp.text

    def test_not_configured_returns_501(self, monkeypatch):
        from app.services.billing_service import BillingNotConfiguredError
        fake = MagicMock()
        fake.process_pilot_refund.side_effect = BillingNotConfiguredError("no stripe")
        client = self._client_with_cookied_user(monkeypatch, fake)
        resp = client.post("/api/v1/billing/pilot-refund")
        assert resp.status_code == 501, resp.text

    def test_no_subscription_returns_404(self, monkeypatch):
        fake = MagicMock()
        fake.process_pilot_refund.side_effect = LookupError("no sub")
        client = self._client_with_cookied_user(monkeypatch, fake)
        resp = client.post("/api/v1/billing/pilot-refund")
        assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------
# /me pilot-signal surface (Step 30a.2-pilot Commit 3c-backend)
# ---------------------------------------------------------------------

class TestMePilotSignal:
    """Pin the new `is_pilot` + `pilot_window_end` fields on
    SubscriptionStatusResponse and the derivation in the /me handler.

    The Account UI uses these to decide whether to render the
    self-serve refund button; if the schema regresses, the button
    either disappears (false negative) or appears for non-pilots
    (false positive). Both are user-visible bugs.
    """

    def test_status_response_has_pilot_fields(self):
        from app.schemas.billing import SubscriptionStatusResponse
        fields = set(SubscriptionStatusResponse.model_fields.keys())
        assert "is_pilot" in fields, "SubscriptionStatusResponse missing is_pilot"
        assert "pilot_window_end" in fields, "SubscriptionStatusResponse missing pilot_window_end"

    def test_status_response_pilot_fields_default_safe(self):
        # Defaults must be the "not a pilot" shape so a hypothetical
        # older builder that omits these fields still produces a
        # well-formed (non-pilot) response.
        from app.schemas.billing import SubscriptionStatusResponse
        from datetime import datetime, timezone
        resp = SubscriptionStatusResponse(
            tenant_id="t_x", tier="individual", status="active",
            active=True, is_entitled=True,
            current_period_start=datetime.now(timezone.utc),
            current_period_end=None, trial_end=None,
            cancel_at_period_end=False, canceled_at=None,
            customer_email="x@y.com",
            billing_cadence="monthly", instance_count_cap=1,
        )
        assert resp.is_pilot is False
        assert resp.pilot_window_end is None

    def test_me_derives_is_pilot_from_snapshot_metadata(self, monkeypatch):
        """End-to-end derivation: pilot metadata + trial_end → is_pilot=True."""
        from datetime import datetime, timedelta, timezone
        from fastapi.testclient import TestClient
        from app.main import app
        from app.api.v1 import billing as billing_api
        from app.core.config import settings

        future = datetime.now(timezone.utc) + timedelta(days=45)
        fake_sub = MagicMock()
        fake_sub.tenant_id = "t_pilot"
        fake_sub.tier = "individual"
        fake_sub.status = "trialing"
        fake_sub.active = True
        fake_sub.is_entitled = True
        fake_sub.current_period_start = datetime.now(timezone.utc)
        fake_sub.current_period_end = future
        fake_sub.trial_end = future
        fake_sub.cancel_at_period_end = False
        fake_sub.canceled_at = None
        fake_sub.customer_email = "pilot@example.com"
        fake_sub.billing_cadence = "monthly"
        fake_sub.instance_count_cap = 1
        fake_sub.provider_snapshot = {"metadata": {"luciel_intro_applied": "true"}}

        fake_user = MagicMock()
        fake_user.id = 42
        fake_svc = MagicMock()
        fake_svc.get_active_subscription_for_user.return_value = fake_sub

        monkeypatch.setattr(billing_api, "_resolve_cookied_user", lambda **kw: fake_user)
        monkeypatch.setattr(billing_api, "_service", lambda db: fake_svc)

        client = TestClient(app)
        client.cookies.set(settings.session_cookie_name, "x", domain="testserver")
        resp = client.get("/api/v1/billing/me")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["is_pilot"] is True
        assert body["pilot_window_end"] is not None

    def test_me_returns_is_pilot_false_for_regular_trial(self, monkeypatch):
        """Regular trialing subscription (no luciel_intro_applied) → is_pilot=False."""
        from datetime import datetime, timedelta, timezone
        from fastapi.testclient import TestClient
        from app.main import app
        from app.api.v1 import billing as billing_api
        from app.core.config import settings

        future = datetime.now(timezone.utc) + timedelta(days=10)
        fake_sub = MagicMock()
        fake_sub.tenant_id = "t_normal"
        fake_sub.tier = "individual"
        fake_sub.status = "trialing"
        fake_sub.active = True
        fake_sub.is_entitled = True
        fake_sub.current_period_start = datetime.now(timezone.utc)
        fake_sub.current_period_end = future
        fake_sub.trial_end = future
        fake_sub.cancel_at_period_end = False
        fake_sub.canceled_at = None
        fake_sub.customer_email = "regular@example.com"
        fake_sub.billing_cadence = "monthly"
        fake_sub.instance_count_cap = 1
        # No luciel_intro_applied — a regular 14-day trial.
        fake_sub.provider_snapshot = {"metadata": {}}

        fake_user = MagicMock()
        fake_user.id = 7
        fake_svc = MagicMock()
        fake_svc.get_active_subscription_for_user.return_value = fake_sub

        monkeypatch.setattr(billing_api, "_resolve_cookied_user", lambda **kw: fake_user)
        monkeypatch.setattr(billing_api, "_service", lambda db: fake_svc)

        client = TestClient(app)
        client.cookies.set(settings.session_cookie_name, "x", domain="testserver")
        resp = client.get("/api/v1/billing/me")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["is_pilot"] is False
        assert body["pilot_window_end"] is None
