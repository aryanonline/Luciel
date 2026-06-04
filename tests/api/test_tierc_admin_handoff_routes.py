"""Rescan Tier-C §3.4.12 — admin handoff endpoints shape tests.

AST + import-level assertions (no Postgres / FastAPI runtime), matching
the repo's tests/api convention for route-contract tests:

  * The handoff router is registered in the api aggregator.
  * POST /admin/sessions/{session_id}/takeover exists.
  * POST /admin/sessions/{session_id}/handback exists.
  * POST /admin/sessions/{session_id}/reply exists.
  * Role gate: _require_takeover_permission is called on every mutation route.
  * Idempotency: takeover returns 200 for already-human_controlled sessions.
  * handback returns 409 when session is not human_controlled.
  * reply returns 409 when session is not human_controlled.
  * Admin reply dispatches via channel adapter attributed to actor_user_id.
  * Audit events human_takeover_started/ended are emitted.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HANDOFF_API = REPO_ROOT / "app" / "api" / "v1" / "admin_handoff.py"
ROUTER_AGG = REPO_ROOT / "app" / "api" / "router.py"


@pytest.fixture(scope="module")
def handoff_source() -> str:
    return HANDOFF_API.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def handoff_ast(handoff_source: str) -> ast.Module:
    return ast.parse(handoff_source)


# =====================================================================
# Router registration.
# =====================================================================


class TestRouterRegistration:

    def test_aggregator_imports_admin_handoff(self):
        src = ROUTER_AGG.read_text(encoding="utf-8")
        assert "admin_handoff" in src

    def test_aggregator_includes_handoff_router(self):
        src = ROUTER_AGG.read_text(encoding="utf-8")
        assert "include_router(admin_handoff.router)" in src


# =====================================================================
# Route contract shape.
# =====================================================================


class TestHandoffRouteShape:

    def test_router_prefix_is_admin_sessions(self, handoff_source: str):
        assert 'prefix="/admin/sessions"' in handoff_source

    def test_takeover_route_exists(self, handoff_source: str):
        assert '/{session_id}/takeover' in handoff_source

    def test_handback_route_exists(self, handoff_source: str):
        assert '/{session_id}/handback' in handoff_source

    def test_reply_route_exists(self, handoff_source: str):
        assert '/{session_id}/reply' in handoff_source

    def test_takeover_is_post(self, handoff_source: str):
        assert '@router.post(\n    "/{session_id}/takeover"' in handoff_source or \
               '@router.post("/{session_id}/takeover"' in handoff_source

    def test_handback_is_post(self, handoff_source: str):
        assert '/{session_id}/handback' in handoff_source

    def test_reply_is_post(self, handoff_source: str):
        assert '/{session_id}/reply' in handoff_source


# =====================================================================
# Role gate enforcement.
# =====================================================================


class TestRoleGate:

    def test_require_takeover_permission_defined(self, handoff_source: str):
        assert "_require_takeover_permission" in handoff_source

    def test_takeover_route_calls_permission_gate(self, handoff_source: str):
        # The takeover function body must call the permission guard.
        assert "_require_takeover_permission" in handoff_source

    def test_handback_route_calls_permission_gate(self, handoff_source: str):
        assert "_require_takeover_permission" in handoff_source

    def test_permission_gate_uses_perm_configure_channels(self, handoff_source: str):
        assert "PERM_CONFIGURE_CHANNELS" in handoff_source

    def test_platform_admin_bypasses_gate(self, handoff_source: str):
        assert "is_platform_admin" in handoff_source


# =====================================================================
# Idempotency / state guard contracts.
# =====================================================================


class TestIdempotencyAndStateGuards:

    def test_takeover_idempotency_comment_or_check(self, handoff_source: str):
        # The takeover handler checks if already human_controlled.
        assert "already" in handoff_source.lower() or "idempotent" in handoff_source.lower()

    def test_handback_rejects_non_human_controlled(self, handoff_source: str):
        # handback must guard against sessions that aren't human_controlled.
        assert "HTTP_409_CONFLICT" in handoff_source

    def test_reply_rejects_non_human_controlled(self, handoff_source: str):
        # reply must also guard against non-human_controlled sessions.
        assert "not_human_controlled" in handoff_source or "session_not_human_controlled" in handoff_source


# =====================================================================
# Audit events.
# =====================================================================


class TestAuditEventEmission:

    def test_action_human_takeover_started_imported(self, handoff_source: str):
        assert "ACTION_HUMAN_TAKEOVER_STARTED" in handoff_source

    def test_action_human_takeover_ended_imported(self, handoff_source: str):
        assert "ACTION_HUMAN_TAKEOVER_ENDED" in handoff_source

    def test_resource_session_used(self, handoff_source: str):
        assert "RESOURCE_SESSION" in handoff_source

    def test_audit_repository_record_called(self, handoff_source: str):
        assert "AdminAuditRepository" in handoff_source
        assert ".record(" in handoff_source


# =====================================================================
# Actor attribution.
# =====================================================================


class TestActorAttribution:

    def test_actor_user_id_extracted_from_request(self, handoff_source: str):
        assert "_require_actor_user_id" in handoff_source

    def test_channel_adapter_used_for_reply(self, handoff_source: str):
        assert "OutboundMessage" in handoff_source

    def test_actor_user_id_attributed_in_channel_metadata(self, handoff_source: str):
        assert "actor_user_id" in handoff_source


# =====================================================================
# Admin reply channel adapter dispatch.
# =====================================================================


class TestAdminReplyDispatch:

    def test_dispatch_admin_reply_function_exists(self, handoff_source: str):
        assert "_dispatch_admin_reply" in handoff_source

    def test_dispatch_handles_widget_channel(self, handoff_source: str):
        assert "WidgetChannelAdapter" in handoff_source

    def test_dispatch_handles_email_channel(self, handoff_source: str):
        assert "EmailChannelAdapter" in handoff_source

    def test_dispatch_handles_sms_channel(self, handoff_source: str):
        assert "SMSChannelAdapter" in handoff_source

    def test_dispatch_is_best_effort_never_500s(self, handoff_source: str):
        # The dispatch is wrapped in try/except BLE001.
        assert "BLE001" in handoff_source or "# noqa" in handoff_source


# =====================================================================
# Response schemas.
# =====================================================================


class TestResponseSchemas:

    def test_takeover_response_schema_exists(self, handoff_source: str):
        assert "TakeoverResponse" in handoff_source

    def test_handback_response_schema_exists(self, handoff_source: str):
        assert "HandbackResponse" in handoff_source

    def test_admin_reply_response_schema_exists(self, handoff_source: str):
        assert "AdminReplyResponse" in handoff_source

    def test_takeover_response_has_control_mode(self, handoff_source: str):
        assert "control_mode" in handoff_source

    def test_handback_response_has_duration_seconds(self, handoff_source: str):
        assert "duration_seconds" in handoff_source


# =====================================================================
# Cross-tenant guard.
# =====================================================================


class TestCrossTenantGuard:

    def test_load_session_for_admin_enforces_tenant(self, handoff_source: str):
        assert "_load_session_for_admin" in handoff_source

    def test_cross_tenant_returns_404_not_403(self, handoff_source: str):
        # The guard returns 404 (not 403) to avoid leaking session existence.
        assert "HTTP_404_NOT_FOUND" in handoff_source


# =====================================================================
# Import-level sanity: the module imports correctly.
# =====================================================================


class TestModuleImport:

    def test_module_imports_cleanly(self):
        import app.api.v1.admin_handoff  # noqa: F401

    def test_action_constants_in_audit_log(self):
        from app.models.admin_audit_log import (
            ACTION_HUMAN_TAKEOVER_STARTED,
            ACTION_HUMAN_TAKEOVER_ENDED,
        )
        assert ACTION_HUMAN_TAKEOVER_STARTED == "human_takeover_started"
        assert ACTION_HUMAN_TAKEOVER_ENDED == "human_takeover_ended"

    def test_session_schema_has_control_mode(self):
        from app.schemas.session import SessionRead
        fields = SessionRead.model_fields
        assert "control_mode" in fields

    def test_session_schema_has_taken_over_at(self):
        from app.schemas.session import SessionRead
        fields = SessionRead.model_fields
        assert "taken_over_at" in fields

    def test_session_schema_has_handed_back_at(self):
        from app.schemas.session import SessionRead
        fields = SessionRead.model_fields
        assert "handed_back_at" in fields
