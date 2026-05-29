"""Arc 12b — /api/v1/billing/me surfaces resolved_permissions.

The dashboard gates the Custom Roles UI on the cookied caller's
resolved Wall-2 permission set. Pre-Arc-12b the dashboard guessed off
``active_role`` string-matching, which diverges from server enforcement
once custom roles enter the picture. This test pins:

  * ``SubscriptionStatusResponse.resolved_permissions`` exists with
    default ``[]`` and ``list[str]`` shape.
  * The /me handler populates it from
    ``PermissionResolver.resolve(request)`` (admin-scoped), serialized
    as a sorted concrete list — never the PLATFORM_ADMIN_ALL sentinel.
  * For a locked-role caller, the field contains the canonical
    locked-role permission set: ``admin_owner`` includes
    ``can_author_custom_roles``; ``read_only_viewer`` does not.

The frontend half of Arc 12b reads this field; if it regresses, the
Custom Roles UI either disappears for Enterprise owners or appears
incorrectly for viewers.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock


class TestResolvedPermissionsSchema:
    def test_field_present_with_safe_default(self):
        from app.schemas.billing import SubscriptionStatusResponse

        fields = SubscriptionStatusResponse.model_fields
        assert "resolved_permissions" in fields, (
            "SubscriptionStatusResponse missing resolved_permissions"
        )

        resp = SubscriptionStatusResponse(
            admin_id="t_x",
            tier="free",
            status="free",
            active=True,
            is_entitled=True,
            current_period_start=None,
            current_period_end=None,
            trial_end=None,
            cancel_at_period_end=False,
            canceled_at=None,
            customer_email="x@y.com",
            billing_cadence="none",
            instance_count_cap=1,
        )
        assert resp.resolved_permissions == []


def _stub_billing_me(monkeypatch, *, fake_resolved):
    """Wire common stubs for the /me route and return a TestClient.

    Stubs: cookied user resolution, the BillingService (no sub),
    ScopeAssignmentRepository (no assignments), JWT decode, and the
    PermissionResolver.resolve return value.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.v1 import billing as billing_api
    from app.repositories import scope_assignment_repository as sar_module
    from app.policy import permissions as permissions_module
    from app.core.config import settings

    fake_user = MagicMock()
    fake_user.id = 99
    fake_user.email = "owner@example.com"

    fake_svc = MagicMock()
    fake_svc.get_active_subscription_for_user.return_value = None  # Free admin path

    monkeypatch.setattr(billing_api, "_resolve_cookied_user", lambda **kw: fake_user)
    monkeypatch.setattr(billing_api, "_service", lambda db: fake_svc)

    fake_sar = MagicMock()
    fake_sar.list_for_user.return_value = []
    monkeypatch.setattr(
        sar_module, "ScopeAssignmentRepository", lambda db: fake_sar
    )
    monkeypatch.setattr(
        billing_api, "validate_session_token",
        lambda token: {"sub": "99", "admin_id": "t_resolved"},
    )

    # The /me handler calls PermissionResolver.resolve(request); patch
    # the classmethod so the route sees the locked-role permission set
    # we want to exercise without standing up middleware-populated
    # request.state.
    monkeypatch.setattr(
        permissions_module.PermissionResolver,
        "resolve",
        classmethod(lambda cls, request, **kw: fake_resolved),
    )

    client = TestClient(app)
    client.cookies.set(settings.session_cookie_name, "x", domain="testserver")
    return client


class TestResolvedPermissionsRoute:
    def test_admin_owner_includes_can_author_custom_roles(self, monkeypatch):
        from app.policy.permissions import LOCKED_ROLE_PERMISSIONS_FALLBACK

        owner_perms = LOCKED_ROLE_PERMISSIONS_FALLBACK["admin_owner"]
        client = _stub_billing_me(monkeypatch, fake_resolved=owner_perms)
        resp = client.get("/api/v1/billing/me")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "resolved_permissions" in body
        assert isinstance(body["resolved_permissions"], list)
        # Concrete sorted list — every element a plain string.
        assert all(isinstance(p, str) for p in body["resolved_permissions"])
        assert body["resolved_permissions"] == sorted(owner_perms)
        assert "can_author_custom_roles" in body["resolved_permissions"]

    def test_read_only_viewer_excludes_can_author_custom_roles(self, monkeypatch):
        from app.policy.permissions import LOCKED_ROLE_PERMISSIONS_FALLBACK

        viewer_perms = LOCKED_ROLE_PERMISSIONS_FALLBACK["read_only_viewer"]
        client = _stub_billing_me(monkeypatch, fake_resolved=viewer_perms)
        resp = client.get("/api/v1/billing/me")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["resolved_permissions"] == sorted(viewer_perms)
        assert "can_author_custom_roles" not in body["resolved_permissions"]

    def test_platform_admin_serializes_to_full_catalog_not_sentinel(self, monkeypatch):
        from app.policy.permissions import ALL_PERMISSIONS, PLATFORM_ADMIN_ALL

        client = _stub_billing_me(monkeypatch, fake_resolved=PLATFORM_ADMIN_ALL)
        resp = client.get("/api/v1/billing/me")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The sentinel must never leak; the route serializes it to the
        # full sorted catalog.
        assert body["resolved_permissions"] == sorted(ALL_PERMISSIONS)
        assert "can_author_custom_roles" in body["resolved_permissions"]
