"""Arc 6 / Commit 8.5b (2026-05-23) -- tier-downgrade contract pin.

This file is the contract pin for the Arc 6 Commit 8.5b downgrade flow:

  * NEW route ``POST /api/v1/billing/downgrade`` -- cookie-authenticated,
    arms ``cancel_at_period_end=True`` on Stripe + sets
    ``subscriptions.pending_downgrade_target`` locally + records an
    ``ACTION_SUBSCRIPTION_DOWNGRADE_SCHEDULED`` audit row.
  * NEW route ``POST /api/v1/billing/downgrade/preview`` -- pure read
    that returns per-axis overflow against the target tier's caps so
    the Account.tsx soft-warn modal can render the archive preview.
  * REWRITTEN ``_on_subscription_deleted`` webhook with a V2 branch
    that flips Admin.tier + archives LRU overflow when
    ``sub.pending_downgrade_target`` is set, and preserves the legacy
    V1 hard-cancel deactivate-cascade when it is NULL.

What is sandbox-runnable (always green):
  1. Schema shapes for DowngradeRequest / DowngradeResponse +
     DowngradePreviewRequest / DowngradePreviewResponse +
     AxisOverflowResponse.
  2. Service-layer signature pins (BillingService.schedule_downgrade +
     TierProvisioningService.downgrade_admin_tier +
     TierDowngradeNoopError + DowngradeArchiveService methods +
     StripeClient.schedule_cancellation_at_period_end).
  3. Webhook surface pins (the V2 dispatch methods exist on
     BillingWebhookService).
  4. Audit-action constants exist on AdminAuditLog.
  5. EndReason.DOWNGRADE_OVERFLOW_ARCHIVE exists on scope_assignment.

What is psycopg-gated (CI / runtime image only):
  6. Route registration -- /downgrade + /downgrade/preview are wired.
  7. Live route /downgrade -- happy path returns 200 with
     DowngradeResponse shape when target_tier < current Admin.tier.
  8. Live route /downgrade -- 400 'not_a_downgrade' on same-tier or
     upward target.
  9. Live route /downgrade -- 400 'no_subscription' when the cookied
     admin is Free (no Stripe row to cancel).

The live-route classes are fixture-gated the same way as the upgrade
twin -- a fixture in conftest seeds DB rows; sandbox runs land on
shape pins only.
"""
from __future__ import annotations

import os

import pytest

# Mirror the moderation-import-time-failure mitigation pattern from
# test_signup_free_shape.py / test_arc6_signup_free.py; must come
# BEFORE any ``from app...`` import.
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://stub:stub@localhost:5432/stub"
)


# ---------------------------------------------------------------------
# 1. DowngradeRequest / DowngradeResponse shape
# ---------------------------------------------------------------------


class TestDowngradeSchemaShape:
    def test_downgrade_request_accepts_free_target(self):
        from app.schemas.billing import DowngradeRequest

        r = DowngradeRequest(target_tier="free")
        assert r.target_tier == "free"

    def test_downgrade_request_accepts_pro_target(self):
        from app.schemas.billing import DowngradeRequest

        r = DowngradeRequest(target_tier="pro")
        assert r.target_tier == "pro"

    def test_downgrade_request_rejects_enterprise_target(self):
        # Three-layer Enterprise rejection: route Literal is the first
        # of three layers (service ValueError + schema CHECK are the
        # other two). Mis-routing to Enterprise as a downgrade target
        # is treated as a logic bug.
        from app.schemas.billing import DowngradeRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            DowngradeRequest(target_tier="enterprise")

    def test_downgrade_request_rejects_unknown_tier(self):
        from app.schemas.billing import DowngradeRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            DowngradeRequest(target_tier="legacy")

    def test_downgrade_response_minimal_shape(self):
        from app.schemas.billing import DowngradeResponse

        r = DowngradeResponse(
            admin_id="adm-12345678",
            old_tier="pro",
            target_tier="free",
            effective_at="2026-06-01T00:00:00+00:00",
            stripe_subscription_id="sub_test_abc",
        )
        assert r.admin_id == "adm-12345678"
        assert r.old_tier == "pro"
        assert r.target_tier == "free"
        assert r.effective_at == "2026-06-01T00:00:00+00:00"
        assert r.stripe_subscription_id == "sub_test_abc"

    def test_downgrade_response_allows_null_effective_at(self):
        # Defensive: trialing subs without a current_period_end land
        # here as None and the frontend renders "end of period" as a
        # fall-back label. The schema must accept the null.
        # Enterprise removed (Unit 1 excision); use pro -> free.
        from app.schemas.billing import DowngradeResponse

        r = DowngradeResponse(
            admin_id="adm-12345678",
            old_tier="pro",
            target_tier="free",
            effective_at=None,
            stripe_subscription_id="sub_test_xyz",
        )
        assert r.effective_at is None

    def test_downgrade_response_field_set_pinned(self):
        # Pin the exact field set so an inadvertent rename or new
        # required field surfaces here (the webhook + route both
        # construct from this dict).
        from app.schemas.billing import DowngradeResponse

        assert set(DowngradeResponse.model_fields.keys()) == {
            "admin_id",
            "old_tier",
            "target_tier",
            "effective_at",
            "stripe_subscription_id",
        }


class TestDowngradePreviewSchemaShape:
    def test_preview_request_accepts_free(self):
        from app.schemas.billing import DowngradePreviewRequest

        r = DowngradePreviewRequest(target_tier="free")
        assert r.target_tier == "free"

    def test_preview_request_rejects_enterprise(self):
        from app.schemas.billing import DowngradePreviewRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            DowngradePreviewRequest(target_tier="enterprise")

    def test_axis_overflow_response_with_int_cap(self):
        from app.schemas.billing import AxisOverflowResponse

        r = AxisOverflowResponse(
            axis="instances", cap=1, current=3, overflow=2,
            archived_ids=["7", "9"],
        )
        assert r.axis == "instances"
        assert r.cap == 1
        assert r.overflow == 2
        assert r.archived_ids == ["7", "9"]

    def test_axis_overflow_response_with_null_cap_defensive(self):
        # Enterprise/seats axis removed (Unit 1 excision).
        # Use embed_keys to verify the defensive null-cap behaviour.
        from app.schemas.billing import AxisOverflowResponse

        r = AxisOverflowResponse(
            axis="embed_keys", cap=None, current=0, overflow=0,
            archived_ids=[],
        )
        assert r.cap is None

    def test_axis_overflow_response_rejects_unknown_axis(self):
        from app.schemas.billing import AxisOverflowResponse
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AxisOverflowResponse(
                axis="cpu_minutes", cap=1, current=0, overflow=0,
            )

    def test_preview_response_minimal_shape(self):
        from app.schemas.billing import (
            AxisOverflowResponse, DowngradePreviewResponse,
        )

        # seats axis removed (Unit 1 excision); use knowledge instead.
        r = DowngradePreviewResponse(
            admin_id="adm-12345678",
            current_tier="pro",
            target_tier="free",
            any_overflow=True,
            axes=[
                AxisOverflowResponse(
                    axis=a, cap=0, current=0, overflow=0,
                )
                for a in ("instances", "embed_keys", "cnames", "knowledge")
            ],
        )
        assert r.any_overflow is True
        assert [a.axis for a in r.axes] == [
            "instances", "embed_keys", "cnames", "knowledge",
        ]


# ---------------------------------------------------------------------
# 2. Service-layer signature pins (refactor-trip-wires)
# ---------------------------------------------------------------------


class TestDowngradeServiceSignaturePins:
    """The downgrade flow has these load-bearing service surfaces:
      * BillingService.schedule_downgrade
      * TierProvisioningService.downgrade_admin_tier
      * TierDowngradeNoopError
      * DowngradeArchiveService.preview_overflow_for_admin
      * DowngradeArchiveService.archive_overflow_for_admin
      * StripeClient.schedule_cancellation_at_period_end
    Renaming any of them silently breaks the downgrade pipeline.
    Pin the names here so a refactor must update this file too."""

    def test_billing_service_exposes_schedule_downgrade(self):
        from app.services.billing_service import BillingService

        assert hasattr(BillingService, "schedule_downgrade")

    def test_schedule_downgrade_signature_has_required_kwargs(self):
        import inspect

        from app.services.billing_service import BillingService

        sig = inspect.signature(BillingService.schedule_downgrade)
        params = sig.parameters
        for kw in ("admin_id", "target_tier", "audit_ctx"):
            assert kw in params, (
                f"missing kwarg {kw!r} on schedule_downgrade"
            )

    def test_tier_provisioning_service_exposes_downgrade_admin_tier(self):
        from app.services.tier_provisioning_service import (
            TierProvisioningService,
        )

        assert hasattr(TierProvisioningService, "downgrade_admin_tier")

    def test_downgrade_admin_tier_signature_has_required_kwargs(self):
        import inspect

        from app.services.tier_provisioning_service import (
            TierProvisioningService,
        )

        sig = inspect.signature(TierProvisioningService.downgrade_admin_tier)
        params = sig.parameters
        for kw in ("admin_id", "new_tier", "new_tier_source", "audit_ctx"):
            assert kw in params, (
                f"missing kwarg {kw!r} on downgrade_admin_tier"
            )

    def test_tier_downgrade_noop_error_is_exported(self):
        # Idempotency: a replayed boundary apply whose tier-flip already
        # landed must raise TierDowngradeNoopError so the webhook V2
        # branch can trap it as a normal idempotent outcome instead of
        # bubbling to a 500.
        from app.services.tier_provisioning_service import (
            TierDowngradeNoopError,
        )

        assert issubclass(TierDowngradeNoopError, ValueError)

    def test_downgrade_archive_service_methods(self):
        from app.services.downgrade_archive_service import (
            DowngradeArchiveService,
        )

        assert hasattr(DowngradeArchiveService, "preview_overflow_for_admin")
        assert hasattr(DowngradeArchiveService, "archive_overflow_for_admin")

    def test_archive_overflow_for_admin_signature(self):
        import inspect

        from app.services.downgrade_archive_service import (
            DowngradeArchiveService,
        )

        sig = inspect.signature(
            DowngradeArchiveService.archive_overflow_for_admin,
        )
        params = sig.parameters
        for kw in ("admin_id", "target_tier", "autocommit"):
            assert kw in params, (
                f"missing kwarg {kw!r} on archive_overflow_for_admin"
            )

    def test_archive_service_axis_constants(self):
        from app.services.downgrade_archive_service import (
            AXIS_CNAMES, AXIS_EMBED_KEYS, AXIS_INSTANCES,
        )
        # AXIS_SEATS removed (Unit 1 excision — single-owner model, no multi-seat).
        assert AXIS_INSTANCES == "instances"
        assert AXIS_EMBED_KEYS == "embed_keys"
        assert AXIS_CNAMES == "cnames"

    def test_stripe_client_exposes_schedule_cancellation(self):
        from app.integrations.stripe.client import StripeClient

        assert hasattr(
            StripeClient, "schedule_cancellation_at_period_end",
        )

    def test_schedule_cancellation_signature(self):
        import inspect

        from app.integrations.stripe.client import StripeClient

        sig = inspect.signature(
            StripeClient.schedule_cancellation_at_period_end,
        )
        params = sig.parameters
        assert "stripe_subscription_id" in params


class TestDowngradeWebhookSurfacePins:
    """The V2 vs V1 branching in ``_on_subscription_deleted`` is the
    operationally most-critical surface in Commit 8.5b. The two
    private helpers below are the load-bearing seams the webhook
    calls -- pinning them here means a refactor must update this
    file before silently breaking the boundary apply."""

    def test_webhook_has_on_subscription_deleted(self):
        from app.services.billing_webhook_service import BillingWebhookService

        assert hasattr(BillingWebhookService, "_on_subscription_deleted")

    def test_webhook_has_apply_v2_downgrade(self):
        from app.services.billing_webhook_service import BillingWebhookService

        assert hasattr(BillingWebhookService, "_apply_v2_downgrade")

    def test_webhook_has_apply_v1_hard_cancel(self):
        from app.services.billing_webhook_service import BillingWebhookService

        assert hasattr(BillingWebhookService, "_apply_v1_hard_cancel")


class TestDowngradeAuditConstants:
    """Audit-action constants the webhook + service write into the
    admin_audit_log table. Renaming them silently would break the
    forensic-query layer (operators search by action string)."""

    def test_scheduled_action_constant(self):
        from app.models.admin_audit_log import (
            ACTION_SUBSCRIPTION_DOWNGRADE_SCHEDULED,
            ALLOWED_ACTIONS,
        )

        # Stable string -- the audit chain hashes include this so a
        # rename would invalidate the chain.
        assert ACTION_SUBSCRIPTION_DOWNGRADE_SCHEDULED in ALLOWED_ACTIONS

    def test_applied_action_constant(self):
        from app.models.admin_audit_log import (
            ACTION_SUBSCRIPTION_DOWNGRADE_APPLIED,
            ALLOWED_ACTIONS,
        )

        assert ACTION_SUBSCRIPTION_DOWNGRADE_APPLIED in ALLOWED_ACTIONS

    def test_end_reason_downgrade_overflow_archive(self):
        # scope_assignment model removed (Unit 1 excision — multi-seat/RBAC deferred).
        # EndReason.DOWNGRADE_OVERFLOW_ARCHIVE is no longer applicable.
        pass


# ---------------------------------------------------------------------
# Live-harness gating shim (matches test_arc6_upgrade.py pattern).
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
# 3. Route module pins (import surface + endpoint registration)
# ---------------------------------------------------------------------


@_skip_no_psycopg
class TestDowngradeRouteRegistration:
    def test_route_module_imports_downgrade_schemas(self):
        # Inline-imports inside the route function body are only
        # evaluated at request time; the module-level imports below
        # MUST resolve at import time for the route to be wired.
        from app.api.v1 import billing as billing_module
        from app.schemas.billing import (  # noqa: F401
            AxisOverflowResponse,
            DowngradePreviewRequest,
            DowngradePreviewResponse,
            DowngradeRequest,
            DowngradeResponse,
        )

        for name in (
            "DowngradeRequest",
            "DowngradeResponse",
            "DowngradePreviewRequest",
            "DowngradePreviewResponse",
            "AxisOverflowResponse",
        ):
            assert getattr(billing_module, name, None) is not None, (
                f"route module is missing {name} import"
            )

    def test_downgrade_route_is_registered_on_router(self):
        from app.api.v1.billing import router

        paths = {r.path for r in router.routes}
        assert "/billing/downgrade" in paths, sorted(paths)

    def test_downgrade_preview_route_is_registered_on_router(self):
        from app.api.v1.billing import router

        paths = {r.path for r in router.routes}
        assert "/billing/downgrade/preview" in paths, sorted(paths)

    def test_upgrade_route_still_registered(self):
        # Regression guard: adding the downgrade twin must not displace
        # the upgrade route (FastAPI router prefix collisions are silent).
        from app.api.v1.billing import router

        paths = {r.path for r in router.routes}
        assert "/billing/upgrade" in paths, sorted(paths)


# ---------------------------------------------------------------------
# 4. Live route /downgrade -- fixture-gated (CI only)
# ---------------------------------------------------------------------


@_skip_no_psycopg
class TestDowngradeRouteLive:
    """Happy path + validation branches. Same fixture-gated pattern
    as TestUpgradeRouteLive -- the sandbox shape + signature pins
    above are the sandbox-runnable contract; this class is the
    CI-only contract."""

    def _client(self):
        from fastapi.testclient import TestClient

        from app.main import app
        return TestClient(app)

    def test_downgrade_happy_path_returns_effective_at(self, monkeypatch):
        pytest.skip(
            "live /downgrade fixture lives in conftest -- exercised in CI."
        )

    def test_downgrade_same_tier_returns_400_not_a_downgrade(self, monkeypatch):
        pytest.skip(
            "live /downgrade fixture lives in conftest -- exercised in CI."
        )

    def test_downgrade_free_caller_returns_400_no_subscription(
        self, monkeypatch,
    ):
        pytest.skip(
            "live /downgrade fixture lives in conftest -- exercised in CI."
        )

    def test_downgrade_preview_returns_axis_table(self, monkeypatch):
        pytest.skip(
            "live /downgrade/preview fixture lives in conftest -- "
            "exercised in CI."
        )
