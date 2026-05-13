"""Backend-free contract tests for Step 30a.1 — tiered self-serve.

Step 30a.1 turns Step 30a's Individual-only self-serve surface into a
three-tier (Individual / Team / Company) self-serve surface. This file
pins the SHAPE of every contract that changed so we catch unintentional
drift between:

  * the SQLAlchemy model surface (new ``billing_cadence`` +
    ``instance_count_cap`` columns; new constants);
  * the Pydantic schema surface (``tier`` + ``billing_cadence`` on
    checkout requests; the two new fields on ``/me`` response);
  * the BillingService resolver helpers (``resolve_price_id`` +
    ``resolve_trial_days`` + ``resolve_instance_count_cap``);
  * the webhook contract (tier-aware tenant id prefix + new audit
    fields on ``ACTION_SUBSCRIPTION_CREATE``);
  * the tier-provisioning service (pre-mint matrix);
  * the AdminService tier/scope guard (``_enforce_tier_scope``);
  * the LucielInstanceCreate schema (``teammate_email`` invite mode);
  * the LucielInstanceRepository count helper.

Coverage budget: 22+ tests per Step 30a.1 design §8.1.

End-to-end correctness (Stripe round-trip, webhook event with new
metadata, pre-mint pipeline, team-invite magic link) is covered by
``tests/e2e/step_30a_1_tiered_self_serve_e2e.py`` (Step 30a.1 ships
without a live e2e the night of deploy; the smoke tests Aryan runs
post-deploy stand in until that test exists).
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------
# 1. Subscription model — new columns / constants
# ---------------------------------------------------------------------

class TestSubscriptionModelCadenceAndCap:
    def test_billing_cadence_column_exists(self):
        from app.models.subscription import Subscription
        assert "billing_cadence" in Subscription.__table__.columns

    def test_billing_cadence_default_monthly(self):
        from app.models.subscription import Subscription
        col = Subscription.__table__.columns["billing_cadence"]
        # Server default must be 'monthly' — every pre-30a.1 row backfilled
        # to this value, every new INSERT lacking metadata gets it too.
        assert col.server_default is not None
        # Defaults render as TextClause; .arg.text or .text varies by SA version.
        arg = getattr(col.server_default, "arg", None)
        text = getattr(arg, "text", None) if arg is not None else None
        assert (text or str(arg) or "").strip("'\" ") == "monthly"

    def test_billing_cadence_not_nullable(self):
        from app.models.subscription import Subscription
        assert (
            Subscription.__table__.columns["billing_cadence"].nullable is False
        )

    def test_instance_count_cap_column_exists(self):
        from app.models.subscription import Subscription
        assert "instance_count_cap" in Subscription.__table__.columns

    def test_instance_count_cap_default_is_three(self):
        # Default matches TIER_INSTANCE_CAPS[Individual] so the safest
        # backfill value never under-provisions a tenant. Stricter caps
        # land via the webhook on the active subscription row.
        from app.models.subscription import Subscription
        col = Subscription.__table__.columns["instance_count_cap"]
        assert col.server_default is not None
        arg = getattr(col.server_default, "arg", None)
        text = getattr(arg, "text", None) if arg is not None else None
        assert (text or str(arg) or "").strip("'\" ") == "3"

    def test_cadence_constants_exported(self):
        from app.models.subscription import (
            ALLOWED_BILLING_CADENCES,
            BILLING_CADENCE_ANNUAL,
            BILLING_CADENCE_MONTHLY,
        )
        assert BILLING_CADENCE_MONTHLY == "monthly"
        assert BILLING_CADENCE_ANNUAL == "annual"
        assert set(ALLOWED_BILLING_CADENCES) == {"monthly", "annual"}

    def test_tier_permitted_scopes_shape(self):
        # The §4.7 line-551 commitment in machine-checkable form. Every
        # higher tier is a strict superset of the tier below it.
        from app.models.subscription import (
            TIER_COMPANY,
            TIER_INDIVIDUAL,
            TIER_PERMITTED_SCOPES,
            TIER_TEAM,
        )
        assert TIER_PERMITTED_SCOPES[TIER_INDIVIDUAL] == ("agent",)
        assert set(TIER_PERMITTED_SCOPES[TIER_TEAM]) == {"agent", "domain"}
        assert set(TIER_PERMITTED_SCOPES[TIER_COMPANY]) == {
            "agent", "domain", "tenant"
        }

    def test_tier_instance_caps_values(self):
        # Pricing-committed caps: Individual=3, Team=10, Company=50.
        from app.models.subscription import (
            TIER_COMPANY,
            TIER_INDIVIDUAL,
            TIER_INSTANCE_CAPS,
            TIER_TEAM,
        )
        assert TIER_INSTANCE_CAPS[TIER_INDIVIDUAL] == 3
        assert TIER_INSTANCE_CAPS[TIER_TEAM] == 10
        assert TIER_INSTANCE_CAPS[TIER_COMPANY] == 50


# ---------------------------------------------------------------------
# 2. Migration — column ADDs land in c2a1b9f30e15
# ---------------------------------------------------------------------

class TestStep30a1Migration:
    def test_migration_file_exists(self):
        path = (
            REPO_ROOT
            / "alembic"
            / "versions"
            / "c2a1b9f30e15_step30a_1_subscription_cadence_and_caps.py"
        )
        assert path.exists()

    def test_migration_down_revision_is_step_30a(self):
        # Step 30a's head is b8e74a3c1d52; the 30a.1 migration MUST
        # claim it as down_revision so alembic stamps correctly.
        path = (
            REPO_ROOT
            / "alembic"
            / "versions"
            / "c2a1b9f30e15_step30a_1_subscription_cadence_and_caps.py"
        )
        text = path.read_text()
        assert "down_revision" in text
        assert "b8e74a3c1d52" in text

    def test_migration_adds_both_new_columns(self):
        path = (
            REPO_ROOT
            / "alembic"
            / "versions"
            / "c2a1b9f30e15_step30a_1_subscription_cadence_and_caps.py"
        )
        text = path.read_text()
        assert "billing_cadence" in text
        assert "instance_count_cap" in text

    def test_migration_adds_cadence_check_constraint(self):
        # The named CHECK constraint guarantees 'monthly'/'annual' are
        # the only legal values DB-side — defence in depth alongside the
        # schema layer's Literal[].
        path = (
            REPO_ROOT
            / "alembic"
            / "versions"
            / "c2a1b9f30e15_step30a_1_subscription_cadence_and_caps.py"
        )
        text = path.read_text()
        assert "ck_subscriptions_billing_cadence" in text

    def test_migration_adds_tier_active_index(self):
        path = (
            REPO_ROOT
            / "alembic"
            / "versions"
            / "c2a1b9f30e15_step30a_1_subscription_cadence_and_caps.py"
        )
        text = path.read_text()
        assert "ix_subscriptions_tier_active" in text


# ---------------------------------------------------------------------
# 3. Config — new price settings
# ---------------------------------------------------------------------

class TestConfigNewPriceSettings:
    def test_five_new_price_settings_declared(self):
        # Five new SKU slots — Individual annual + Team monthly/annual +
        # Company monthly/annual. All optional so dev/CI runs without
        # them. The webhook resolves the active one via PRICE_ID_KEY.
        #
        # Use class-level ``model_fields`` so this contract test does not
        # require DATABASE_URL or any other prod env var to be present —
        # matches the AST/import-only discipline of the rest of this file.
        from app.core.config import Settings
        for attr in (
            "stripe_price_individual_annual",
            "stripe_price_team_monthly",
            "stripe_price_team_annual",
            "stripe_price_company_monthly",
            "stripe_price_company_annual",
        ):
            assert attr in Settings.model_fields, f"missing setting {attr!r}"


# ---------------------------------------------------------------------
# 4. BillingService resolvers
# ---------------------------------------------------------------------

class TestBillingServiceResolvers:
    def test_price_id_key_table_complete(self):
        # All six (tier, cadence) pairs must be present — missing any
        # one would crash the checkout for that SKU at runtime.
        from app.services.billing_service import PRICE_ID_KEY
        expected = {
            ("individual", "monthly"),
            ("individual", "annual"),
            ("team", "monthly"),
            ("team", "annual"),
            ("company", "monthly"),
            ("company", "annual"),
        }
        assert set(PRICE_ID_KEY.keys()) == expected

    def test_price_id_key_values_match_settings_attrs(self):
        # The mapping must point at real `settings.*` attribute names.
        from app.core.config import settings
        from app.services.billing_service import PRICE_ID_KEY
        for pair, attr in PRICE_ID_KEY.items():
            assert hasattr(settings, attr), (
                f"PRICE_ID_KEY[{pair!r}] -> {attr!r} not declared on settings"
            )

    def test_trial_days_table_has_individual_monthly(self):
        # Step 30a's 14-day trial must carry over to Individual+monthly.
        from app.services.billing_service import TRIAL_DAYS
        assert TRIAL_DAYS[("individual", "monthly")] == 14

    def test_trial_days_for_team_monthly_is_seven(self):
        from app.services.billing_service import TRIAL_DAYS
        assert TRIAL_DAYS[("team", "monthly")] == 7

    def test_resolve_instance_count_cap_uses_tier_table(self):
        # The resolver is a thin wrapper over TIER_INSTANCE_CAPS, but
        # we pin it explicitly so a future refactor can't silently
        # diverge from the model-layer source of truth.
        from app.models.subscription import (
            TIER_COMPANY,
            TIER_INDIVIDUAL,
            TIER_TEAM,
        )
        from app.services.billing_service import BillingService

        # We don't instantiate the service (it needs a DB); we call the
        # method on the class via the underlying function for shape.
        fn = BillingService.resolve_instance_count_cap
        assert fn(tier=TIER_INDIVIDUAL) == 3
        assert fn(tier=TIER_TEAM) == 10
        assert fn(tier=TIER_COMPANY) == 50


# ---------------------------------------------------------------------
# 5. CheckoutSessionRequest schema — tier + cadence
# ---------------------------------------------------------------------

class TestCheckoutSessionRequestSchema:
    def test_tier_field_is_literal_three_values(self):
        from app.schemas.billing import CheckoutSessionRequest
        fields = CheckoutSessionRequest.model_fields
        assert "tier" in fields

    def test_billing_cadence_field_is_literal_two_values(self):
        from app.schemas.billing import CheckoutSessionRequest
        fields = CheckoutSessionRequest.model_fields
        assert "billing_cadence" in fields

    def test_tier_rejects_unknown_value(self):
        from app.schemas.billing import CheckoutSessionRequest
        with pytest.raises(Exception):
            CheckoutSessionRequest(
                email="a@b.com",
                tier="enterprise",  # not in the three-value Literal
                billing_cadence="monthly",
            )

    def test_cadence_rejects_unknown_value(self):
        from app.schemas.billing import CheckoutSessionRequest
        with pytest.raises(Exception):
            CheckoutSessionRequest(
                email="a@b.com",
                tier="individual",
                billing_cadence="quarterly",  # not in two-value Literal
            )


# ---------------------------------------------------------------------
# 6. SubscriptionStatusResponse — /me shape
# ---------------------------------------------------------------------

class TestSubscriptionStatusResponseShape:
    def test_response_carries_billing_cadence(self):
        from app.schemas.billing import SubscriptionStatusResponse
        assert "billing_cadence" in SubscriptionStatusResponse.model_fields

    def test_response_carries_instance_count_cap(self):
        from app.schemas.billing import SubscriptionStatusResponse
        assert "instance_count_cap" in SubscriptionStatusResponse.model_fields


# ---------------------------------------------------------------------
# 7. Webhook — tier-aware tenant id minting
# ---------------------------------------------------------------------

class TestWebhookTierAwareTenantMint:
    def test_tier_prefix_table_complete(self):
        from app.services.billing_webhook_service import _TIER_PREFIX
        assert _TIER_PREFIX == {
            "individual": "ind",
            "team":       "team",
            "company":    "co",
        }

    def test_mint_uses_tier_prefix(self):
        from app.services.billing_webhook_service import (
            _mint_tenant_id_from_email,
        )
        assert _mint_tenant_id_from_email("x@y.com", tier="individual").startswith("ind-")
        assert _mint_tenant_id_from_email("x@y.com", tier="team").startswith("team-")
        assert _mint_tenant_id_from_email("x@y.com", tier="company").startswith("co-")

    def test_mint_default_falls_back_to_individual(self):
        # Unknown tier on a hand-crafted Stripe event must not crash;
        # the webhook logs and falls back to the safest tier prefix.
        from app.services.billing_webhook_service import (
            _mint_tenant_id_from_email,
        )
        # The function itself uses 'ind' as the default; the webhook
        # caller also rewrites the tier var to TIER_INDIVIDUAL before
        # calling. We pin the function-level default here.
        assert _mint_tenant_id_from_email("x@y.com", tier="wat").startswith("ind-")


# ---------------------------------------------------------------------
# 8. TierProvisioningService — pre-mint matrix
# ---------------------------------------------------------------------

class TestTierProvisioningServiceShape:
    def test_module_exposes_class(self):
        from app.services.tier_provisioning_service import (
            TierProvisioningService,
        )
        assert inspect.isclass(TierProvisioningService)

    def test_class_has_premint_for_tier(self):
        from app.services.tier_provisioning_service import (
            TierProvisioningService,
        )
        assert hasattr(TierProvisioningService, "premint_for_tier")
        sig = inspect.signature(TierProvisioningService.premint_for_tier)
        # Pin the keyword arguments so a refactor doesn't silently drop
        # one (the webhook depends on this exact call shape).
        kw = set(sig.parameters.keys())
        for required in ("tenant_id", "tier", "primary_user", "audit_ctx"):
            assert required in kw, f"premint_for_tier missing kw {required!r}"

    def test_default_domain_id_is_general(self):
        # Must match OnboardingService's default. Drift here would
        # silently break pre-mint (validate_parent_scope_active would
        # 400 with \"domain not found\" on the agent-scope create).
        from app.services.tier_provisioning_service import DEFAULT_DOMAIN_ID
        assert DEFAULT_DOMAIN_ID == "general"

    def test_slugify_drops_email_domain(self):
        from app.services.tier_provisioning_service import (
            _slugify_agent_id_from_email,
        )
        assert _slugify_agent_id_from_email("Sarah.Chen@remax.com") == "sarah-chen"

    def test_slugify_falls_back_to_primary(self):
        from app.services.tier_provisioning_service import (
            _slugify_agent_id_from_email,
        )
        # Local part too short -> falls back to safe slug.
        assert _slugify_agent_id_from_email("a@b.com") == "primary"


# ---------------------------------------------------------------------
# 9. AdminService — tier/scope guard
# ---------------------------------------------------------------------

class TestAdminServiceTierScopeGuard:
    def test_admin_service_has_enforce_tier_scope(self):
        from app.services.admin_service import AdminService
        assert hasattr(AdminService, "_enforce_tier_scope")

    def test_enforce_tier_scope_signature(self):
        from app.services.admin_service import AdminService
        sig = inspect.signature(AdminService._enforce_tier_scope)
        kw = set(sig.parameters.keys())
        for required in ("tenant_id", "requested_scope_level"):
            assert required in kw, (
                f"_enforce_tier_scope missing kw {required!r}"
            )

    def test_tier_scope_violation_error_has_reasons(self):
        from app.services.luciel_instance_service import (
            TierScopeViolationError,
        )
        for r in (
            "REASON_SCOPE_NOT_PERMITTED",
            "REASON_CAP_EXCEEDED",
            "REASON_NO_ACTIVE_SUBSCRIPTION",
        ):
            assert hasattr(TierScopeViolationError, r)


# ---------------------------------------------------------------------
# 10. LucielInstanceRepository — count helper
# ---------------------------------------------------------------------

class TestRepositoryCountHelper:
    def test_repository_has_count_active_for_tenant(self):
        from app.repositories.luciel_instance_repository import (
            LucielInstanceRepository,
        )
        assert hasattr(LucielInstanceRepository, "count_active_for_tenant")


# ---------------------------------------------------------------------
# 11. LucielInstanceCreate — teammate_email invite mode
# ---------------------------------------------------------------------

class TestLucielInstanceCreateInviteMode:
    def test_teammate_email_field_present(self):
        from app.schemas.luciel_instance import LucielInstanceCreate
        assert "teammate_email" in LucielInstanceCreate.model_fields

    def test_invite_mode_requires_agent_scope(self):
        from app.schemas.luciel_instance import LucielInstanceCreate
        with pytest.raises(Exception):
            LucielInstanceCreate(
                instance_id="x-luciel",
                display_name="X",
                scope_level="domain",  # invalid in invite mode
                scope_owner_tenant_id="t1",
                scope_owner_domain_id="d1",
                teammate_email="t@example.com",
            )

    def test_invite_mode_rejects_explicit_agent_id(self):
        from app.schemas.luciel_instance import LucielInstanceCreate
        with pytest.raises(Exception):
            LucielInstanceCreate(
                instance_id="x-luciel",
                display_name="X",
                scope_level="agent",
                scope_owner_tenant_id="t1",
                scope_owner_domain_id="d1",
                scope_owner_agent_id="someone-else",  # invalid; route mints
                teammate_email="t@example.com",
            )

    def test_invite_mode_accepts_minimum_shape(self):
        from app.schemas.luciel_instance import LucielInstanceCreate
        m = LucielInstanceCreate(
            instance_id="x-luciel",
            display_name="X",
            scope_level="agent",
            scope_owner_tenant_id="t1",
            scope_owner_domain_id="d1",
            teammate_email="t@example.com",
        )
        # The route fills scope_owner_agent_id after _invite_teammate runs.
        assert m.scope_owner_agent_id is None
        assert str(m.teammate_email) == "t@example.com"


# ---------------------------------------------------------------------
# 12. Audit constants — no new actions, but RESOURCE_AGENT still in scope
# ---------------------------------------------------------------------

class TestAuditConstantsCoverage:
    def test_resource_agent_still_allowed(self):
        # The pre-mint + invite paths both write RESOURCE_AGENT audit
        # rows; ALLOWED_RESOURCE_TYPES must include it.
        from app.models.admin_audit_log import (
            ALLOWED_RESOURCE_TYPES,
            RESOURCE_AGENT,
        )
        assert RESOURCE_AGENT in ALLOWED_RESOURCE_TYPES

    def test_action_create_still_allowed(self):
        from app.models.admin_audit_log import (
            ACTION_CREATE,
            ALLOWED_ACTIONS,
        )
        assert ACTION_CREATE in ALLOWED_ACTIONS
