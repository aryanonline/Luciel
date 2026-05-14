"""Backend-free contract tests for Step 30a — subscription billing.

Step 30a lands a self-serve subscription surface on the Luciel backend
(`/api/v1/billing/*`) plus the Stripe-integration glue underneath it.
This file pins the *shape* of that surface so we catch unintentional
contract drift between the website client (Luciel-Website/src/lib/billing.ts)
and the backend.

Coverage (AST + import only — no Postgres, no FastAPI runtime, no Stripe
network):

  * Subscription model surface — table name, scope-bearing columns,
    Stripe identifier columns, status/tier constants, `is_entitled`
    property, composite indexes that back the two hot read paths.
  * Pydantic schemas — every request/response model the marketing site
    consumes has the documented field set.
  * Router registration — `app.api.v1.billing.router` exists with the
    seven documented routes at the documented HTTP verbs.
  * Webhook discipline — handler dispatch table covers the four event
    types we promise to react to + a fail-closed default branch.
  * Audit constants — RESOURCE_SUBSCRIPTION + the five billing actions
    are registered with `admin_audit_log` so the audit-before-commit
    invariant is enforceable.
  * Auth-middleware exemption — `/api/v1/billing` is in SKIP_AUTH_PATHS;
    route-level gates (Stripe signature / cookie / public-by-design)
    do the actual authorization work.
  * Config — every `settings.*` field the billing surface reads is
    declared on `app.core.config`.

End-to-end correctness (Stripe round-trip, webhook idempotency, mint
atomicity, cancel cascade) is covered by tests/e2e/step_30a_live_e2e.py.
This file is the surface-shape pin.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------
# 1. Subscription model — table, columns, indexes, constants
# ---------------------------------------------------------------------

class TestSubscriptionModel:
    def test_class_exists(self):
        from app.models.subscription import Subscription
        from app.models.base import Base
        assert issubclass(Subscription, Base)

    def test_table_name_is_subscriptions(self):
        from app.models.subscription import Subscription
        assert Subscription.__tablename__ == "subscriptions"

    def test_scope_binding_columns(self):
        from app.models.subscription import Subscription
        cols = {c.name for c in Subscription.__table__.columns}
        # The four "what does this row scope-bind?" columns.
        for required in ("tenant_id", "user_id", "customer_email"):
            assert required in cols, f"missing column {required!r}"

    def test_stripe_identifier_columns(self):
        from app.models.subscription import Subscription
        cols = {c.name for c in Subscription.__table__.columns}
        for required in (
            "stripe_customer_id",
            "stripe_subscription_id",
            "stripe_price_id",
        ):
            assert required in cols, f"missing column {required!r}"

    def test_stripe_subscription_id_is_unique(self):
        # Globally unique per Stripe — enforce with a UNIQUE constraint
        # so the webhook idempotency check is DB-level, not app-level.
        from app.models.subscription import Subscription
        col = Subscription.__table__.columns["stripe_subscription_id"]
        assert col.unique is True

    def test_billing_cycle_columns_nullable(self):
        # current_period_start/_end/trial_end/canceled_at are nullable
        # because Stripe doesn't always set them at row-create time
        # (e.g. incomplete subscriptions during card capture).
        from app.models.subscription import Subscription
        for c in ("current_period_start", "current_period_end", "trial_end", "canceled_at"):
            assert Subscription.__table__.columns[c].nullable is True, f"{c} must be nullable"

    def test_status_and_tier_indexed(self):
        # The Account/billing UI filters by status; the webhook keys off tier.
        from app.models.subscription import Subscription
        assert Subscription.__table__.columns["status"].index is True
        assert Subscription.__table__.columns["tier"].index is True

    def test_soft_delete_active_flag(self):
        # Pattern E discipline — never DELETE.
        from app.models.subscription import Subscription
        col = Subscription.__table__.columns["active"]
        assert col.nullable is False
        assert col.default.arg is True

    def test_last_event_id_for_idempotency(self):
        # Webhook idempotency keys off this. Must be nullable so the
        # initial INSERT (pre-webhook) is legal.
        from app.models.subscription import Subscription
        col = Subscription.__table__.columns["last_event_id"]
        assert col.nullable is True

    def test_provider_snapshot_is_jsonb(self):
        # Forensic readers expect a structured payload, not a TEXT blob.
        from sqlalchemy.dialects.postgresql import JSONB
        from app.models.subscription import Subscription
        col = Subscription.__table__.columns["provider_snapshot"]
        assert isinstance(col.type, JSONB), f"provider_snapshot must be JSONB, got {type(col.type)}"

    def test_composite_indexes_for_hot_queries(self):
        # Two hot reads call for indexes:
        # 1. "is this tenant entitled?" -> (tenant_id, active)
        # 2. "Stripe customer -> last subscription" -> (stripe_customer_id)
        from app.models.subscription import Subscription
        idx_names = {ix.name for ix in Subscription.__table__.indexes}
        assert "ix_subscriptions_tenant_active" in idx_names
        assert "ix_subscriptions_stripe_customer" in idx_names

    def test_tier_constants(self):
        from app.models.subscription import (
            TIER_INDIVIDUAL,
            TIER_TEAM,
            TIER_COMPANY,
            ALLOWED_TIERS,
        )
        assert TIER_INDIVIDUAL == "individual"
        assert TIER_TEAM == "team"
        assert TIER_COMPANY == "company"
        assert set(ALLOWED_TIERS) == {"individual", "team", "company"}

    def test_status_constants_mirror_stripe(self):
        # If Stripe's status set ever grows, ALLOWED_STATUSES is advisory
        # so this just pins what we react to today.
        from app.models.subscription import ALLOWED_STATUSES
        assert set(ALLOWED_STATUSES) == {
            "incomplete",
            "incomplete_expired",
            "trialing",
            "active",
            "past_due",
            "canceled",
            "unpaid",
            "paused",
        }

    def test_entitled_statuses(self):
        # The cascade in ARCHITECTURE §4.5 only flips the tenant active
        # for trialing / active / past_due. Anything else deactivates.
        from app.models.subscription import ENTITLED_STATUSES
        assert ENTITLED_STATUSES == frozenset({"trialing", "active", "past_due"})

    def test_is_entitled_property(self):
        from app.models.subscription import Subscription
        # Real Subscription class (not an instance) — just verify the
        # property exists and is read-only.
        descriptor = inspect.getattr_static(Subscription, "is_entitled")
        assert isinstance(descriptor, property)


# ---------------------------------------------------------------------
# 2. Pydantic schemas — the website client's contract
# ---------------------------------------------------------------------

class TestBillingSchemas:
    def test_checkout_session_request_shape(self):
        from app.schemas.billing import CheckoutSessionRequest
        fields = CheckoutSessionRequest.model_fields
        assert "email" in fields
        assert "display_name" in fields
        assert "tier" in fields
        # tenant_id is reserved for Step 30a.1 upgrades, optional today.
        assert "tenant_id" in fields
        assert fields["tenant_id"].default is None

    def test_checkout_session_request_display_name_is_bounded(self):
        # Defends against zero-length names AND DoS via huge strings.
        from app.schemas.billing import CheckoutSessionRequest
        schema = CheckoutSessionRequest.model_json_schema()
        props = schema["properties"]["display_name"]
        assert props.get("minLength") == 1
        assert props.get("maxLength") == 200

    def test_checkout_session_response_shape(self):
        from app.schemas.billing import CheckoutSessionResponse
        fields = CheckoutSessionResponse.model_fields
        assert "checkout_url" in fields
        assert "session_id" in fields

    def test_onboarding_claim_request_shape(self):
        from app.schemas.billing import OnboardingClaimRequest
        fields = OnboardingClaimRequest.model_fields
        assert "session_id" in fields

    def test_onboarding_claim_response_state_field(self):
        # The marketing site branches on this enum.
        from app.schemas.billing import OnboardingClaimResponse
        fields = OnboardingClaimResponse.model_fields
        assert "state" in fields
        assert "email_sent_to" in fields
        # email_sent_to may be null when Stripe has no email on the session.
        assert fields["email_sent_to"].default is None

    def test_portal_session_response_shape(self):
        from app.schemas.billing import PortalSessionResponse
        fields = PortalSessionResponse.model_fields
        assert "portal_url" in fields

    def test_subscription_status_response_mirrors_model(self):
        # GET /me returns a slice of Subscription. The marketing site's
        # SubscriptionStatus interface in src/lib/billing.ts mirrors this
        # exactly; any addition here must be added there too.
        from app.schemas.billing import SubscriptionStatusResponse
        fields = SubscriptionStatusResponse.model_fields
        for name in (
            "tenant_id",
            "tier",
            "status",
            "active",
            "is_entitled",
            "current_period_start",
            "current_period_end",
            "trial_end",
            "cancel_at_period_end",
            "canceled_at",
            "customer_email",
        ):
            assert name in fields, f"missing field {name!r} on SubscriptionStatusResponse"

    def test_subscription_status_uses_from_attributes(self):
        # The response is built from the Subscription ORM row directly.
        from app.schemas.billing import SubscriptionStatusResponse
        assert SubscriptionStatusResponse.model_config.get("from_attributes") is True


# ---------------------------------------------------------------------
# 3. Router registration — the seven documented routes
# ---------------------------------------------------------------------

class TestBillingRouter:
    def test_router_exists_with_prefix(self):
        from app.api.v1.billing import router
        assert router.prefix == "/billing"
        assert "billing" in router.tags

    def test_seven_routes_registered(self):
        from app.api.v1.billing import router
        # Map (method, path) so we don't care about endpoint function names.
        registered = {
            (m, r.path)
            for r in router.routes
            # APIRouter mounts each route once per declared method.
            for m in getattr(r, "methods", set()) or set()
        }
        expected = {
            ("POST", "/billing/checkout"),
            ("POST", "/billing/webhook"),
            ("POST", "/billing/onboarding/claim"),
            ("GET", "/billing/login"),
            ("POST", "/billing/portal"),
            ("GET", "/billing/me"),
            ("POST", "/billing/logout"),
        }
        missing = expected - registered
        assert not missing, f"missing routes: {missing}"

    def test_router_registered_on_v1_aggregate(self):
        # If the api/router.py forgets to include this surface, the
        # whole feature is dark. This is the contract that protects it.
        from app.api.router import api_router
        paths = {r.path for r in api_router.routes}
        assert any("/billing/" in p for p in paths), (
            "billing router not registered on app.api.router.api_router"
        )


# ---------------------------------------------------------------------
# 4. Auth middleware exemption — route-level gates are authoritative
# ---------------------------------------------------------------------

class TestAuthMiddlewareExemption:
    def test_billing_path_is_in_skip_auth(self):
        # The webhook MUST not be gated by the standard API-key
        # middleware because the inbound caller is Stripe, not a tenant.
        from app.middleware.auth import SKIP_AUTH_PATHS
        # Allow either the exact prefix or any path starting with it.
        assert any(
            p == "/api/v1/billing" or p.startswith("/api/v1/billing")
            for p in SKIP_AUTH_PATHS
        ), f"/api/v1/billing not exempt; saw SKIP_AUTH_PATHS={SKIP_AUTH_PATHS!r}"


# ---------------------------------------------------------------------
# 5. Audit constants — billing has its own resource + 5 actions
# ---------------------------------------------------------------------

class TestAuditConstants:
    def test_resource_subscription_registered(self):
        from app.models.admin_audit_log import (
            RESOURCE_SUBSCRIPTION,
            ALLOWED_RESOURCE_TYPES,
        )
        assert RESOURCE_SUBSCRIPTION in ALLOWED_RESOURCE_TYPES

    def test_five_billing_actions_registered(self):
        from app.models.admin_audit_log import (
            ACTION_SUBSCRIPTION_CREATE,
            ACTION_SUBSCRIPTION_UPDATE,
            ACTION_SUBSCRIPTION_CANCEL,
            ACTION_SUBSCRIPTION_REACTIVATE,
            ACTION_BILLING_WEBHOOK_REPLAY_REJECTED,
            ALLOWED_ACTIONS,
        )
        for action in (
            ACTION_SUBSCRIPTION_CREATE,
            ACTION_SUBSCRIPTION_UPDATE,
            ACTION_SUBSCRIPTION_CANCEL,
            ACTION_SUBSCRIPTION_REACTIVATE,
            ACTION_BILLING_WEBHOOK_REPLAY_REJECTED,
        ):
            assert action in ALLOWED_ACTIONS, f"action {action!r} not in ALLOWED_ACTIONS"


# ---------------------------------------------------------------------
# 6. Config surface — every settings.* field the routes read is declared
# ---------------------------------------------------------------------

class TestBillingConfig:
    def test_stripe_secret_fields_present(self):
        from app.core.config import settings
        for f in (
            "stripe_secret_key",
            "stripe_publishable_key",
            "stripe_webhook_secret",
            "stripe_price_individual",
            "billing_success_url",
            "billing_cancel_url",
        ):
            assert hasattr(settings, f), f"settings.{f} not declared"

    def test_billing_trial_days_default(self):
        # Step 30a.2 retired the free-trial model in favour of a uniform
        # $100 CAD paid intro (90 days, first-time customers only). The
        # legacy ``billing_trial_days`` setting is preserved for
        # back-compat with external scripts but the application code
        # path no longer reads it; default flipped from 14 to 0 so a
        # mis-wired caller that DID read it would get "no trial"
        # rather than silently reintroducing the old 14-day free trial.
        # See app/services/billing_service.py:INTRO_TRIAL_DAYS for the
        # real source of truth.
        from app.core.config import settings
        assert settings.billing_trial_days == 0

    def test_session_cookie_fields_present(self):
        from app.core.config import settings
        for f in (
            "magic_link_secret",
            "magic_link_ttl_hours",
            "session_cookie_ttl_days",
            "session_cookie_name",
            "session_cookie_secure",
            "session_cookie_domain",
        ):
            assert hasattr(settings, f), f"settings.{f} not declared"

    def test_session_cookie_defaults(self):
        from app.core.config import settings
        # Long-lived session, host-only by default, secure flag on.
        assert settings.session_cookie_ttl_days == 30
        assert settings.session_cookie_secure is True
        assert settings.session_cookie_name == "luciel_session"


# ---------------------------------------------------------------------
# 7. Webhook discipline — handler covers the four documented events
# ---------------------------------------------------------------------

class TestBillingWebhookDispatch:
    """Static analysis of billing_webhook_service.py — we look at the
    source text rather than importing the class so this passes even
    when stripe / pyjwt aren't installed locally."""

    WEBHOOK_PATH = REPO_ROOT / "app" / "services" / "billing_webhook_service.py"

    def test_handles_checkout_session_completed(self):
        src = self.WEBHOOK_PATH.read_text()
        assert "checkout.session.completed" in src, (
            "billing_webhook_service.py must dispatch checkout.session.completed"
        )

    def test_handles_subscription_updated(self):
        src = self.WEBHOOK_PATH.read_text()
        assert "customer.subscription.updated" in src

    def test_handles_subscription_deleted(self):
        src = self.WEBHOOK_PATH.read_text()
        assert "customer.subscription.deleted" in src

    def test_handles_invoice_payment_failed(self):
        src = self.WEBHOOK_PATH.read_text()
        assert "invoice.payment_failed" in src

    def test_replay_dedup_uses_last_event_id(self):
        # Idempotency invariant: a duplicate evt_… must be a no-op.
        src = self.WEBHOOK_PATH.read_text()
        assert "last_event_id" in src, (
            "webhook handler must dedupe on last_event_id for replay safety"
        )

    def test_cancel_uses_existing_deactivate_cascade(self):
        # We MUST NOT roll our own tenant teardown — reuse the cascade.
        src = self.WEBHOOK_PATH.read_text()
        assert "deactivate_tenant_with_cascade" in src, (
            "cancel path must call AdminService.deactivate_tenant_with_cascade"
        )

    def test_mint_uses_onboarding_service(self):
        # Tenant minting MUST go through OnboardingService — that is the
        # single chokepoint where tenant_configs, scopes, and roles get
        # provisioned together.
        src = self.WEBHOOK_PATH.read_text()
        assert "onboard_tenant" in src, (
            "checkout.session.completed must call OnboardingService.onboard_tenant"
        )


# ---------------------------------------------------------------------
# 8. Stripe client surface — what the rest of the code imports
# ---------------------------------------------------------------------

class TestStripeIntegration:
    def test_get_stripe_client_exported(self):
        from app.integrations.stripe import get_stripe_client
        assert callable(get_stripe_client)

    def test_signature_error_exported(self):
        from app.integrations.stripe import StripeSignatureError
        assert issubclass(StripeSignatureError, Exception)

    def test_client_source_pins_api_version(self):
        # We pin Stripe API version to insulate the backend from
        # silent breaking changes when Stripe rolls forward.
        src = (REPO_ROOT / "app" / "integrations" / "stripe" / "client.py").read_text()
        assert "2024-06-20" in src, "Stripe API version pin missing from client.py"


# ---------------------------------------------------------------------
# 9. Magic-link service surface
# ---------------------------------------------------------------------

class TestMagicLinkService:
    MAGIC_PATH = REPO_ROOT / "app" / "services" / "magic_link_service.py"

    def test_module_exports_four_helpers(self):
        # The route handlers import these names — make sure they exist
        # via AST so we don't need PyJWT installed.
        src = self.MAGIC_PATH.read_text()
        tree = ast.parse(src)
        defs = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for required in (
            "mint_magic_link_token",
            "mint_session_token",
            "consume_magic_link_token",
            "validate_session_token",
            "build_magic_link_url",
        ):
            assert required in defs, f"missing helper {required!r}"

    def test_module_defines_typed_error(self):
        src = self.MAGIC_PATH.read_text()
        tree = ast.parse(src)
        cls_defs = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        assert "MagicLinkError" in cls_defs


# ---------------------------------------------------------------------
# 10. Email service stable marker
# ---------------------------------------------------------------------

class TestEmailServiceMarker:
    def test_send_magic_link_email_emits_marker(self):
        # The e2e harness greps for this exact marker to confirm the
        # magic link was minted+enqueued. If the marker drifts, the
        # e2e harness silently regresses.
        src = (REPO_ROOT / "app" / "services" / "email_service.py").read_text()
        assert "[magic-link-email]" in src, (
            "send_magic_link_email must log a '[magic-link-email]' marker"
        )
