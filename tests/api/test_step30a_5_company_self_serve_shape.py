"""Backend-free contract tests for Step 30a.5 -- Company-tier self-serve.

Step 30a.5 lands the dashboard surface a Company customer uses to build
their organization without founder involvement: a cookied self-serve
Domain create / list / deactivate route family, the
``UserInvite.role='department_lead'`` lifecycle leg layered on the Step
30a.4 invite primitive, per-tier Domain count caps, and the surfacing of
``active_role`` on /billing/me so the dashboard can gate CompanyTab on
tier + role together (the load-bearing Q5 decision in the design doc).

Coverage (AST + import only -- no Postgres, no FastAPI runtime, no SES
network), modelled on tests/api/test_step30a_4_invite_shape.py:

  * Per-tier Domain count cap constant -- the dict matches the design
    (Individual 0, Team 1, Company 50).
  * TierScopeViolationError -- the REASON_DOMAIN_CAP_EXCEEDED constant
    exists for the route to map onto error.code='domain_cap_reached'.
  * AdminAuditLog action constants -- ACTION_DOMAIN_CREATED and
    ACTION_DOMAIN_DEACTIVATED exist and are in ALLOWED_ACTIONS.
  * DomainConfigSelfServeCreate -- payload shape (slug regex pinned;
    display_name + description bounds pinned).
  * AdminService surface -- enforce_domain_cap +
    count_active_domains_for_tenant present with the documented
    signatures.
  * Admin router -- the three /admin/domains/self-serve routes are
    registered with the right HTTP verbs.
  * teammate_email overload removal -- LucielInstanceCreate no longer
    accepts the field; the deprecated invite-mode validator branch is
    gone from the schema.
  * SubscriptionStatusResponse -- active_role field surfaced so the
    dashboard can gate CompanyTab on tier + role (design \u00a711 Q5).

End-to-end correctness (cookied admin -> Domain create -> department
lead invite -> redeem -> agent invite -> session) lives in
tests/e2e/step_30a_5_live_e2e.py (env-gated on
STEP_30A_5_LIVE_E2E_ENABLED=1; defaults off in CI per design \u00a79.2).
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------
# 1. Per-tier Domain count cap (design \u00a73.5 / \u00a711 Q1)
# ---------------------------------------------------------------------


class TestDomainCountCap:
    def test_cap_constant_present(self):
        from app.models.subscription import DOMAIN_COUNT_CAP_BY_TIER
        assert isinstance(DOMAIN_COUNT_CAP_BY_TIER, dict)

    def test_cap_individual_zero(self):
        """Individual tier cannot create Domains at all -- the tier is
        single-scope by definition (design \u00a711 Q1)."""
        from app.models.subscription import (
            DOMAIN_COUNT_CAP_BY_TIER,
            TIER_INDIVIDUAL,
        )
        assert DOMAIN_COUNT_CAP_BY_TIER[TIER_INDIVIDUAL] == 0

    def test_cap_team_one(self):
        """Team tier is single-Domain by design -- a 'team' is one
        Domain (design \u00a73.5)."""
        from app.models.subscription import (
            DOMAIN_COUNT_CAP_BY_TIER,
            TIER_TEAM,
        )
        assert DOMAIN_COUNT_CAP_BY_TIER[TIER_TEAM] == 1

    def test_cap_company_fifty(self):
        """Company tier cap is 50, symmetric with
        TIER_INSTANCE_CAPS[TIER_COMPANY] (partner judgment 2026-05-18,
        design \u00a711 Q1).
        """
        from app.models.subscription import (
            DOMAIN_COUNT_CAP_BY_TIER,
            TIER_COMPANY,
        )
        assert DOMAIN_COUNT_CAP_BY_TIER[TIER_COMPANY] == 50


# ---------------------------------------------------------------------
# 2. TierScopeViolationError -- Domain-cap reason code
# ---------------------------------------------------------------------


class TestTierScopeViolationErrorReasons:
    def test_domain_cap_reason_present(self):
        """The route layer maps REASON_DOMAIN_CAP_EXCEEDED onto the
        public error.code='domain_cap_reached'. Removing the reason
        constant would break the route's mapping silently.
        """
        from app.services.luciel_instance_service import (
            TierScopeViolationError,
        )
        assert hasattr(TierScopeViolationError, "REASON_DOMAIN_CAP_EXCEEDED")
        assert (
            TierScopeViolationError.REASON_DOMAIN_CAP_EXCEEDED
            == "domain_cap_exceeded"
        )

    def test_other_reasons_still_present(self):
        """Regression guard -- the original three reasons must remain
        on the same exception class so the route's mapping table covers
        every reason.
        """
        from app.services.luciel_instance_service import (
            TierScopeViolationError,
        )
        for name in (
            "REASON_SCOPE_NOT_PERMITTED",
            "REASON_CAP_EXCEEDED",
            "REASON_NO_ACTIVE_SUBSCRIPTION",
        ):
            assert hasattr(TierScopeViolationError, name)


# ---------------------------------------------------------------------
# 3. AdminAuditLog action constants -- Domain self-serve verbs
# ---------------------------------------------------------------------


class TestAdminAuditDomainActions:
    def test_action_domain_created(self):
        from app.models.admin_audit_log import ACTION_DOMAIN_CREATED
        assert ACTION_DOMAIN_CREATED == "domain_created"

    def test_action_domain_deactivated(self):
        from app.models.admin_audit_log import ACTION_DOMAIN_DEACTIVATED
        assert ACTION_DOMAIN_DEACTIVATED == "domain_deactivated"

    def test_actions_in_allowed_set(self):
        """Without ALLOWED_ACTIONS membership the audit repo would
        reject writes from the new self-serve route.
        """
        from app.models.admin_audit_log import (
            ACTION_DOMAIN_CREATED,
            ACTION_DOMAIN_DEACTIVATED,
            ALLOWED_ACTIONS,
        )
        assert ACTION_DOMAIN_CREATED in ALLOWED_ACTIONS
        assert ACTION_DOMAIN_DEACTIVATED in ALLOWED_ACTIONS


# ---------------------------------------------------------------------
# 4. DomainConfigSelfServeCreate -- payload validation contract
# ---------------------------------------------------------------------


class TestDomainConfigSelfServeCreate:
    def test_required_fields(self):
        from app.schemas.admin import DomainConfigSelfServeCreate
        fields = DomainConfigSelfServeCreate.model_fields
        for name in ("domain_id", "display_name"):
            assert name in fields, (
                f"DomainConfigSelfServeCreate must require {name}"
            )

    def test_description_optional(self):
        from app.schemas.admin import DomainConfigSelfServeCreate
        fields = DomainConfigSelfServeCreate.model_fields
        assert "description" in fields
        # Optional means default is not PydanticUndefined.
        assert fields["description"].default is None

    # --- Slug regex validation ---

    def test_slug_valid_lowercase_digits_hyphen(self):
        from app.schemas.admin import DomainConfigSelfServeCreate
        # Happy: lowercase letters, digits, and internal hyphens.
        m = DomainConfigSelfServeCreate(
            domain_id="sales-team-2",
            display_name="Sales Team 2",
        )
        assert m.domain_id == "sales-team-2"

    def test_slug_rejects_uppercase(self):
        from app.schemas.admin import DomainConfigSelfServeCreate
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            DomainConfigSelfServeCreate(
                domain_id="Sales",
                display_name="Sales",
            )

    def test_slug_rejects_leading_hyphen(self):
        from app.schemas.admin import DomainConfigSelfServeCreate
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            DomainConfigSelfServeCreate(
                domain_id="-sales",
                display_name="Sales",
            )

    def test_slug_rejects_trailing_hyphen(self):
        from app.schemas.admin import DomainConfigSelfServeCreate
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            DomainConfigSelfServeCreate(
                domain_id="sales-",
                display_name="Sales",
            )

    def test_slug_rejects_underscore(self):
        """Underscores are not URL-safe in the way hyphens are -- the
        slug shows up in URLs and audit logs, so we reject them.
        """
        from app.schemas.admin import DomainConfigSelfServeCreate
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            DomainConfigSelfServeCreate(
                domain_id="sales_team",
                display_name="Sales",
            )

    # --- Length bounds ---

    def test_slug_min_length_two(self):
        """The regex requires two characters at minimum (first + last),
        which is the smallest slug that could meaningfully appear in
        a URL.
        """
        from app.schemas.admin import DomainConfigSelfServeCreate
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            DomainConfigSelfServeCreate(
                domain_id="a",
                display_name="A",
            )

    def test_slug_max_length_sixty_four(self):
        from app.schemas.admin import DomainConfigSelfServeCreate
        from pydantic import ValidationError
        too_long = "a" + "b" * 64  # 65 chars
        with pytest.raises(ValidationError):
            DomainConfigSelfServeCreate(
                domain_id=too_long,
                display_name="X",
            )

    def test_display_name_required_non_empty(self):
        from app.schemas.admin import DomainConfigSelfServeCreate
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            DomainConfigSelfServeCreate(
                domain_id="sales",
                display_name="",
            )

    def test_display_name_max_length(self):
        from app.schemas.admin import DomainConfigSelfServeCreate
        from pydantic import ValidationError
        # Implementation max is 120 (more permissive than the design
        # \u00a73.3 sketch which mentioned 64 -- the implementation wins).
        too_long = "x" * 121
        with pytest.raises(ValidationError):
            DomainConfigSelfServeCreate(
                domain_id="sales",
                display_name=too_long,
            )


# ---------------------------------------------------------------------
# 5. AdminService -- domain-cap + count helpers
# ---------------------------------------------------------------------


class TestAdminServiceDomainCapSurface:
    def test_enforce_domain_cap_signature(self):
        """The route calls enforce_domain_cap(tenant_id=...) before the
        slug-collision check, so the parameter must stay keyword-only.
        """
        from app.services.admin_service import AdminService
        sig = inspect.signature(AdminService.enforce_domain_cap)
        assert "tenant_id" in sig.parameters
        assert (
            sig.parameters["tenant_id"].kind
            == inspect.Parameter.KEYWORD_ONLY
        )

    def test_count_active_domains_signature(self):
        from app.services.admin_service import AdminService
        sig = inspect.signature(
            AdminService.count_active_domains_for_tenant
        )
        assert "tenant_id" in sig.parameters

    def test_create_domain_config_present(self):
        """create_domain_config is the underlying writer the self-serve
        route calls; pinning that it still exists.
        """
        from app.services.admin_service import AdminService
        assert hasattr(AdminService, "create_domain_config")
        assert callable(AdminService.create_domain_config)


# ---------------------------------------------------------------------
# 6. Admin router -- /admin/domains/self-serve surface
# ---------------------------------------------------------------------


class TestAdminDomainSelfServeRouter:
    def _routes_with_path(self, segment: str):
        from app.api.v1.admin import router
        return [
            r for r in router.routes
            if hasattr(r, "path") and segment in r.path
        ]

    def test_post_create_route(self):
        routes = self._routes_with_path("/domains/self-serve")
        post = [
            r for r in routes
            if r.path == "/admin/domains/self-serve"
            and "POST" in r.methods
        ]
        assert len(post) == 1, (
            "POST /admin/domains/self-serve must be registered exactly once"
        )

    def test_get_list_route(self):
        routes = self._routes_with_path("/domains/self-serve")
        get = [
            r for r in routes
            if r.path == "/admin/domains/self-serve"
            and "GET" in r.methods
        ]
        assert len(get) == 1, (
            "GET /admin/domains/self-serve must be registered exactly once"
        )

    def test_delete_route(self):
        routes = self._routes_with_path("/domains/self-serve")
        delete = [
            r for r in routes
            if r.path == "/admin/domains/self-serve/{domain_id}"
            and "DELETE" in r.methods
        ]
        assert len(delete) == 1, (
            "DELETE /admin/domains/self-serve/{domain_id} must be "
            "registered exactly once"
        )

    def test_route_source_maps_402_codes(self):
        """The route source MUST map all three TierScopeViolationError
        reasons onto stable error.code strings. The frontend toast logic
        in CompanyTab depends on this mapping; a silent rename would
        break the user-visible error copy without any other test
        failing.
        """
        admin_path = REPO_ROOT / "app" / "api" / "v1" / "admin.py"
        src = admin_path.read_text()
        for code in (
            '"domain_cap_reached"',
            '"tier_scope_not_allowed"',
            '"no_active_subscription"',
            '"domain_slug_taken"',
        ):
            assert code in src, (
                f"admin.py must map a TierScopeViolationError reason "
                f"onto {code}; the CompanyTab toast logic depends on "
                f"the exact string."
            )


# ---------------------------------------------------------------------
# 7. teammate_email overload removal (design \u00a78)
# ---------------------------------------------------------------------


class TestTeammateEmailOverloadRemoved:
    def test_schema_does_not_accept_teammate_email(self):
        """The Step 30a.1 invite-mode overload was removed in Step 30a.5
        (design \u00a78). LucielInstanceCreate must no longer carry the
        field; the first-class invite path is POST /admin/invites.
        """
        from app.schemas.luciel_instance import LucielInstanceCreate
        assert "teammate_email" not in LucielInstanceCreate.model_fields, (
            "LucielInstanceCreate must NOT carry teammate_email -- the "
            "Step 30a.1 invite-mode overload was removed in Step 30a.5 "
            "in favour of POST /admin/invites (Step 30a.4)."
        )

    def test_invite_teammate_helper_removed(self):
        """The legacy _invite_teammate helper that lived under the
        /admin/luciel-instances route file has been deleted. Confirming
        at the source-import level so a re-introduction can't sneak
        back in.
        """
        from app.api.v1 import admin as admin_module
        assert not hasattr(admin_module, "_invite_teammate"), (
            "_invite_teammate must remain removed; the invite-creation "
            "path is invite_service.create_invite (Step 30a.4)."
        )

    def test_route_source_documents_removal(self):
        """A source-level pin so a future refactor that deletes the
        explanatory comment also has to update this test, forcing the
        author to think about whether they're undoing the removal.
        """
        admin_path = REPO_ROOT / "app" / "api" / "v1" / "admin.py"
        src = admin_path.read_text()
        assert "teammate_email overload removed" in src, (
            "admin.py must keep the Step 30a.5 removal banner so future "
            "readers can find the rationale without spelunking git log."
        )


# ---------------------------------------------------------------------
# 8. SubscriptionStatusResponse -- active_role surfaced (design \u00a711 Q5)
# ---------------------------------------------------------------------


class TestSubscriptionStatusActiveRole:
    def test_active_role_field_present(self):
        """The CompanyTab tier+role gate reads active_role from the
        existing /billing/me payload. Without this field the gate
        falls open and a department_lead under a Company tenant sees
        every Domain in the tenant on first login (design \u00a711 Q5).
        """
        from app.schemas.billing import SubscriptionStatusResponse
        assert "active_role" in SubscriptionStatusResponse.model_fields

    def test_active_role_is_optional_string(self):
        """active_role is None for users without an active
        ScopeAssignment (legacy pre-Step-30a.4 sessions, edge cases
        during rollout). The frontend treats None the same as a role
        not in the allowed set -- the tab stays hidden.
        """
        from app.schemas.billing import SubscriptionStatusResponse
        field = SubscriptionStatusResponse.model_fields["active_role"]
        # Optional => default is None.
        assert field.default is None

    def test_billing_route_source_hydrates_active_role(self):
        """A source-level pin so the /billing/me handler can't be
        refactored to drop the active_role hydration without this
        test failing.
        """
        billing_path = REPO_ROOT / "app" / "api" / "v1" / "billing.py"
        src = billing_path.read_text()
        assert "active_role" in src, (
            "billing.py must populate active_role on the /billing/me "
            "response; CompanyTab visibility gate depends on it."
        )
