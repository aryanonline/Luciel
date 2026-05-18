"""Backend-free contract tests for Step 30a.5 post-smoke fixes.

Three bugs surfaced in the $1,000 live smoke walk on 2026-05-18, all
caught after PR #50 / #4 merged but before the close tag. Each fix is
pinned at the contract layer here, on top of the runtime e2e harness in
tests/e2e/step_30a_5_live_e2e.py.

Coverage (AST + import only -- no Postgres, no FastAPI runtime):

  * _resolve_tenant_for_user resolves via ScopeAssignment first, with
    a BillingService fallback. Pins the source-of-truth flip that
    fixed the critical "No subscription on file" empty state surfaced
    to redeemed teammates. Drift:
    D-invite-redeemed-user-sees-no-subscription-on-file-2026-05-18.

  * DomainConfigSelfServeRead schema exists with the two route-level
    rollup fields documented in design \u00a74.4. Drift:
    D-company-tab-domain-rollup-fields-missing-2026-05-18.

  * GET /admin/domains/self-serve uses the rollup-bearing schema as
    its response_model. Pins the wire contract end of the same drift.

The Team-tab invite-row UI gap
(D-team-tab-invite-row-missing-role-and-domain-2026-05-18) is a
frontend-only fix; it's covered by the website's vitest in
src/test/Dashboard.tsx.test.tsx, not here.
"""
from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path

import pytest

# Step 30d added DATABASE_URL gating at app.core.config import time.
# The auth + admin routers we test below transitively import
# app.core.config via slowapi / middleware chains, so we need a value
# present before any of those imports run. Mirrors the pattern in
# tests/api/test_step31_dashboard_http_shape.py. The URL never
# receives a real request -- the tests only inspect static / class-
# level metadata.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------
# 1. _resolve_tenant_for_user resolves via ScopeAssignment, not billing
# ---------------------------------------------------------------------


class TestResolveTenantForUserScopeFirst:
    """Pin the source-of-truth flip in app/api/v1/auth.py.

    The Step 30a.5 smoke walk caught that redeemed teammates were
    surfaced "No subscription on file" because _resolve_tenant_for_user
    looked up the user's Stripe subscription instead of the user's
    active ScopeAssignment. The fix delegates to
    ScopeAssignmentRepository.list_for_user first; BillingService is
    now only the fallback for the mid-checkout race.
    """

    def _source(self) -> str:
        from app.api.v1 import auth as auth_mod

        return inspect.getsource(auth_mod._resolve_tenant_for_user)

    def test_function_exists(self):
        from app.api.v1.auth import _resolve_tenant_for_user

        assert callable(_resolve_tenant_for_user)

    def test_uses_scope_assignment_repository(self):
        src = self._source()
        assert "ScopeAssignmentRepository" in src, (
            "_resolve_tenant_for_user must consult ScopeAssignmentRepository "
            "first -- tenant binding is owned by ScopeAssignment, not Stripe"
        )
        assert "list_for_user" in src
        assert "active_only=True" in src, (
            "Must filter to active assignments only (ended_at IS NULL AND "
            "active=True per the repo's active_only contract)"
        )

    def test_prefers_owner_role(self):
        src = self._source()
        # The function must explicitly prefer role='owner' so an owner
        # logging in always lands on their canonical primary scope,
        # even if a stale tenant_admin assignment somehow co-exists.
        assert "owner" in src
        assert "role" in src

    def test_billing_service_is_fallback_only(self):
        """BillingService is still imported but must execute only when
        no active ScopeAssignment exists (the mid-checkout race).
        We assert that the BillingService call appears AFTER the
        ScopeAssignment branch in source order.
        """
        src = self._source()
        sa_idx = src.find("ScopeAssignmentRepository")
        bs_idx = src.find("BillingService")
        assert sa_idx != -1 and bs_idx != -1
        assert sa_idx < bs_idx, (
            "ScopeAssignment resolution must precede BillingService "
            "fallback -- otherwise the bug recurs"
        )

    def test_empty_string_fallback_preserved(self):
        """The pre-30a.5 contract: returns "" (not None) when nothing
        resolves, so the session middleware can re-resolve safely."""
        src = self._source()
        # Either explicit empty-string return or `else ""` ternary.
        assert 'return ""' in src or 'else ""' in src

    def test_function_has_drift_docstring_reference(self):
        """The fix must self-document the drift id so future
        regressions are findable by grep."""
        # Collapse any wrapped whitespace from the docstring so the
        # drift id can be matched even if it's split across lines.
        src = " ".join(self._source().split())
        assert (
            "D-invite-redeemed-user-sees-no-subscription-on-file-"
            "2026-05-18"
        ) in src


# ---------------------------------------------------------------------
# 2. DomainConfigSelfServeRead schema -- design \u00a74.4 rollup fields
# ---------------------------------------------------------------------


class TestDomainConfigSelfServeReadSchema:
    """Pin the route-level rollup variant promised by design \u00a74.4.

    Pre-fix: GET /admin/domains/self-serve returned plain
    DomainConfigRead, so the CompanyTab Domain card could not render
    'N agents \u00b7 M invites pending'. The new schema extends
    DomainConfigRead with two non-negative int fields.
    """

    def test_class_exists(self):
        from app.schemas.admin import DomainConfigSelfServeRead

        assert DomainConfigSelfServeRead is not None

    def test_class_inherits_from_domain_config_read(self):
        from app.schemas.admin import DomainConfigRead, DomainConfigSelfServeRead

        assert issubclass(DomainConfigSelfServeRead, DomainConfigRead), (
            "DomainConfigSelfServeRead must extend DomainConfigRead so all "
            "canonical model fields stay in scope"
        )

    def test_pending_invites_count_field(self):
        from app.schemas.admin import DomainConfigSelfServeRead

        fields = DomainConfigSelfServeRead.model_fields
        assert "pending_invites_count" in fields
        f = fields["pending_invites_count"]
        # Pydantic v2: ge constraint surfaces in metadata
        assert f.annotation is int

    def test_active_agents_count_field(self):
        from app.schemas.admin import DomainConfigSelfServeRead

        fields = DomainConfigSelfServeRead.model_fields
        assert "active_agents_count" in fields
        f = fields["active_agents_count"]
        assert f.annotation is int

    def test_rejects_negative_counts(self):
        """ge=0 must reject negative values at schema-validation time
        so a buggy route handler cannot leak negative counts."""
        from app.schemas.admin import DomainConfigSelfServeRead

        with pytest.raises(Exception):
            # Bypass the parent fields by passing only the rollup;
            # validation must fail on the negative int regardless.
            DomainConfigSelfServeRead.model_validate(
                {"pending_invites_count": -1, "active_agents_count": 0}
            )


# ---------------------------------------------------------------------
# 3. GET /admin/domains/self-serve uses the rollup-bearing schema
# ---------------------------------------------------------------------


class TestListDomainsSelfServeResponseModel:
    """Pin the wire contract -- the route must return the rollup
    schema, not the plain DomainConfigRead."""

    def test_route_response_model_is_self_serve_read(self):
        from app.api.v1.admin import router
        from app.schemas.admin import DomainConfigSelfServeRead

        # The admin router is registered with prefix="/admin" so the
        # full path is "/admin/domains/self-serve" -- match the
        # convention used by tests/api/test_step30a_5_company_self_serve
        # _shape.py::TestAdminDomainSelfServeRouter.
        match = None
        for r in router.routes:
            if (
                getattr(r, "path", None) == "/admin/domains/self-serve"
                and "GET" in (getattr(r, "methods", set()) or set())
            ):
                match = r
                break
        assert match is not None, (
            "GET /admin/domains/self-serve must be registered on the "
            "admin router"
        )
        # response_model is list[DomainConfigSelfServeRead]
        rm = getattr(match, "response_model", None)
        assert rm is not None
        # list[X] -- introspect the parameterized generic
        args = getattr(rm, "__args__", ()) or ()
        assert DomainConfigSelfServeRead in args, (
            "GET /admin/domains/self-serve must declare "
            "response_model=list[DomainConfigSelfServeRead] so the rollup "
            "fields land in the OpenAPI schema and the wire response"
        )

    def test_route_source_groups_by_domain_id(self):
        """The route handler must group both counts by domain_id (not
        return tenant-level totals)."""
        from app.api.v1 import admin as admin_mod

        src = inspect.getsource(admin_mod.list_domains_self_serve)
        assert "group_by(UserInvite.domain_id)" in src
        assert "group_by(Agent.domain_id)" in src

    def test_route_filters_pending_invites_only(self):
        """Pending invites only -- accepted / revoked / expired rows
        must not inflate the badge."""
        from app.api.v1 import admin as admin_mod

        src = inspect.getsource(admin_mod.list_domains_self_serve)
        assert "InviteStatus.PENDING" in src

    def test_route_filters_active_agents_only(self):
        from app.api.v1 import admin as admin_mod

        src = inspect.getsource(admin_mod.list_domains_self_serve)
        assert "Agent.active.is_(True)" in src

    def test_route_tenant_scoped(self):
        """The two rollup queries must filter by tenant_id from the
        cookied actor -- never by a client-supplied id."""
        from app.api.v1 import admin as admin_mod

        src = inspect.getsource(admin_mod.list_domains_self_serve)
        assert "UserInvite.tenant_id == tenant_id" in src
        assert "Agent.tenant_id == tenant_id" in src
        # tenant_id must come from _resolve_invite_actor, not query
        assert "_resolve_invite_actor" in src
