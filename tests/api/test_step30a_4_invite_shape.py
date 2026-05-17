"""Backend-free contract tests for Step 30a.4 -- /admin/invites.

Step 30a.4 lands the first-class UserInvite lifecycle that the
/app/team UI calls (Team tier) and that the /app/company UI will call
(Step 30a.5, Company tier). This file pins the *shape* of that surface
so we catch unintentional contract drift between the marketing site's
React pages (Luciel-Website src/pages/AppTeam.tsx +
AppInviteAccept.tsx) and the backend.

Coverage (AST + import only -- no Postgres, no FastAPI runtime, no SES
network):

  * UserInvite model surface -- table name, columns, native enum,
    partial unique index on (tenant_id, LOWER(invited_email)).
  * Alembic migration -- the Step 30a.4 revision is present with the
    documented revision id and down_revision.
  * Audit constants -- the four ACTION_* + RESOURCE_USER_INVITE
    constants exist and are in ALLOWED_ACTIONS / ALLOWED_RESOURCE_TYPES.
  * UserInviteRepository -- the documented public methods exist with
    keyword-only signatures.
  * InviteService -- the four module-level functions exist with the
    documented signatures and exception classes.
  * Admin router -- the four /admin/invites routes are registered with
    the right HTTP verbs.
  * Auth router /set-password -- the route source contains the
    purpose=='invite' branch that calls invite_service.redeem_invite.
  * Schema -- UserInviteCreate, UserInviteRead, UserInviteResendResponse,
    UserInviteRevokeResponse exist with the documented fields.

End-to-end correctness (cookied admin -> invite -> redeem -> session)
is covered by tests/e2e/step_30a_4_team_invite_live_e2e.py.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------
# 1. UserInvite model surface
# ---------------------------------------------------------------------


class TestUserInviteModel:
    def test_table_name(self):
        from app.models.user_invite import UserInvite
        assert UserInvite.__tablename__ == "user_invites"

    def test_required_columns_present(self):
        from app.models.user_invite import UserInvite
        cols = {c.name for c in UserInvite.__table__.columns}
        # 15 columns total: PK + 11 invite-specific + created_at/updated_at + ...
        required = {
            "id",
            "tenant_id",
            "domain_id",
            "inviter_user_id",
            "invited_email",
            "role",
            "token_jti",
            "status",
            "expires_at",
            "accepted_at",
            "accepted_user_id",
            "revoked_at",
            "revoked_by_api_key_id",
            "created_at",
            "updated_at",
        }
        missing = required - cols
        assert not missing, f"UserInvite missing columns: {missing}"

    def test_token_jti_is_unique(self):
        from app.models.user_invite import UserInvite
        col = UserInvite.__table__.columns["token_jti"]
        assert col.unique is True, (
            "token_jti must carry a UNIQUE constraint -- the redemption "
            "path looks up by jti and a leaked token must not collide "
            "with a different invite"
        )

    def test_status_is_native_enum(self):
        from app.models.user_invite import InviteStatus, UserInvite
        col = UserInvite.__table__.columns["status"]
        # Native Postgres enum named user_invite_status
        from sqlalchemy import Enum
        assert isinstance(col.type, Enum), (
            "user_invites.status must be a SQLAlchemy Enum type"
        )
        assert col.type.name == "user_invite_status"

    def test_invite_status_values(self):
        from app.models.user_invite import InviteStatus
        assert InviteStatus.PENDING.value == "pending"
        assert InviteStatus.ACCEPTED.value == "accepted"
        assert InviteStatus.EXPIRED.value == "expired"
        assert InviteStatus.REVOKED.value == "revoked"

    def test_indexes_present(self):
        from app.models.user_invite import UserInvite
        index_names = {idx.name for idx in UserInvite.__table__.indexes}
        # Three named indexes are declared in __table_args__.
        for required in {
            "ix_user_invites_tenant_status_pending",
            "ix_user_invites_invited_email_lower",
            "uq_user_invites_tenant_email_pending",
        }:
            assert required in index_names, (
                f"UserInvite must declare index {required}; "
                f"declared: {sorted(index_names)}"
            )


# ---------------------------------------------------------------------
# 2. Alembic migration head
# ---------------------------------------------------------------------


class TestStep30a4Migration:
    def test_migration_file_exists(self):
        target = (
            REPO_ROOT
            / "alembic"
            / "versions"
            / "e7b2c9d4a18f_step30a_4_user_invites_table.py"
        )
        assert target.exists(), (
            "Step 30a.4 Alembic migration e7b2c9d4a18f must exist"
        )

    def test_migration_descends_from_step30a_3(self):
        target = (
            REPO_ROOT
            / "alembic"
            / "versions"
            / "e7b2c9d4a18f_step30a_4_user_invites_table.py"
        )
        text = target.read_text()
        assert 'revision = "e7b2c9d4a18f"' in text
        assert 'down_revision = "a3c1f08b9d42"' in text, (
            "Step 30a.4 migration must descend from Step 30a.3 head a3c1f08b9d42"
        )


# ---------------------------------------------------------------------
# 3. Audit constants whitelisted
# ---------------------------------------------------------------------


class TestAuditConstants:
    def test_action_constants_exist(self):
        from app.models.admin_audit_log import (
            ACTION_INVITE_REDEEMED,
            ACTION_INVITE_RESENT,
            ACTION_INVITE_REVOKED,
            ACTION_USER_INVITED,
        )
        assert ACTION_USER_INVITED == "user_invited"
        assert ACTION_INVITE_REDEEMED == "invite_redeemed"
        assert ACTION_INVITE_RESENT == "invite_resent"
        assert ACTION_INVITE_REVOKED == "invite_revoked"

    def test_resource_constant_exists(self):
        from app.models.admin_audit_log import RESOURCE_USER_INVITE
        assert RESOURCE_USER_INVITE == "user_invite"

    def test_actions_in_whitelist(self):
        from app.models.admin_audit_log import (
            ACTION_INVITE_REDEEMED,
            ACTION_INVITE_RESENT,
            ACTION_INVITE_REVOKED,
            ACTION_USER_INVITED,
            ALLOWED_ACTIONS,
        )
        for action in (
            ACTION_USER_INVITED,
            ACTION_INVITE_REDEEMED,
            ACTION_INVITE_RESENT,
            ACTION_INVITE_REVOKED,
        ):
            assert action in ALLOWED_ACTIONS, (
                f"{action!r} must be in ALLOWED_ACTIONS for audit-row "
                f"emission to be accepted"
            )

    def test_resource_in_whitelist(self):
        from app.models.admin_audit_log import (
            ALLOWED_RESOURCE_TYPES,
            RESOURCE_USER_INVITE,
        )
        assert RESOURCE_USER_INVITE in ALLOWED_RESOURCE_TYPES


# ---------------------------------------------------------------------
# 4. UserInviteRepository surface
# ---------------------------------------------------------------------


class TestUserInviteRepository:
    def test_repository_methods_exist(self):
        from app.repositories.user_invites import (
            INVITE_ROW_TTL,
            UserInviteRepository,
        )
        # Document constant
        from datetime import timedelta
        assert INVITE_ROW_TTL == timedelta(days=7), (
            "Closure-shape alpha: invite row TTL is 7 days"
        )

        for name in (
            "create",
            "get_by_pk",
            "get_by_jti",
            "get_pending_for_email",
            "list_for_tenant",
            "count_pending_for_tenant",
            "mark_accepted",
            "mark_revoked",
            "mark_expired",
            "rotate_token_jti",
        ):
            assert hasattr(UserInviteRepository, name), (
                f"UserInviteRepository must expose {name}"
            )

    def test_create_signature_keyword_only(self):
        from app.repositories.user_invites import UserInviteRepository
        sig = inspect.signature(UserInviteRepository.create)
        params = sig.parameters
        # `self` plus all-keyword args
        for name in (
            "tenant_id",
            "domain_id",
            "inviter_user_id",
            "invited_email",
            "role",
            "token_jti",
        ):
            assert name in params
            assert params[name].kind == inspect.Parameter.KEYWORD_ONLY, (
                f"UserInviteRepository.create.{name} must be keyword-only"
            )


# ---------------------------------------------------------------------
# 5. InviteService surface
# ---------------------------------------------------------------------


class TestInviteService:
    def test_module_level_functions_exist(self):
        from app.services import invite_service
        for name in (
            "create_invite",
            "redeem_invite",
            "resend_invite",
            "revoke_invite",
        ):
            fn = getattr(invite_service, name, None)
            assert callable(fn), (
                f"invite_service.{name} must be a module-level callable "
                f"(matches AuthService convention)"
            )

    def test_error_classes_exist(self):
        from app.services.invite_service import (
            DuplicatePendingInviteError,
            InviteError,
            InviteExpiredError,
            InviteNotFoundError,
            InviteNotPendingError,
            InvitePendingCapExceededError,
        )
        # All concrete errors descend from InviteError
        for cls in (
            DuplicatePendingInviteError,
            InviteExpiredError,
            InviteNotFoundError,
            InviteNotPendingError,
            InvitePendingCapExceededError,
        ):
            assert issubclass(cls, InviteError)

    def test_create_invite_signature(self):
        from app.services.invite_service import create_invite
        sig = inspect.signature(create_invite)
        for name in (
            "db",
            "tenant_id",
            "domain_id",
            "inviter_user_id",
            "inviter_email",
            "invited_email",
            "role",
            "audit_ctx",
        ):
            assert name in sig.parameters, (
                f"create_invite must accept {name}"
            )
            assert (
                sig.parameters[name].kind == inspect.Parameter.KEYWORD_ONLY
            ), f"create_invite.{name} must be keyword-only"

    def test_redeem_invite_signature(self):
        from app.services.invite_service import redeem_invite
        sig = inspect.signature(redeem_invite)
        for name in ("db", "token", "payload", "password", "audit_ctx"):
            assert name in sig.parameters
            assert (
                sig.parameters[name].kind == inspect.Parameter.KEYWORD_ONLY
            )


# ---------------------------------------------------------------------
# 6. Admin router /admin/invites surface
# ---------------------------------------------------------------------


class TestAdminInviteRouter:
    def _routes_with_path(self, segment: str):
        from app.api.v1.admin import router
        return [
            r for r in router.routes
            if hasattr(r, "path") and segment in r.path
        ]

    def test_post_admin_invites(self):
        routes = self._routes_with_path("/invites")
        # POST /admin/invites
        post_create = [
            r for r in routes
            if r.path == "/admin/invites" and "POST" in r.methods
        ]
        assert len(post_create) == 1, (
            "POST /admin/invites must be registered exactly once"
        )

    def test_get_admin_invites(self):
        routes = self._routes_with_path("/invites")
        get_list = [
            r for r in routes
            if r.path == "/admin/invites" and "GET" in r.methods
        ]
        assert len(get_list) == 1, (
            "GET /admin/invites must be registered exactly once"
        )

    def test_post_admin_invites_resend(self):
        routes = self._routes_with_path("/invites")
        post_resend = [
            r for r in routes
            if r.path == "/admin/invites/{invite_id}/resend"
            and "POST" in r.methods
        ]
        assert len(post_resend) == 1

    def test_delete_admin_invites(self):
        routes = self._routes_with_path("/invites")
        delete = [
            r for r in routes
            if r.path == "/admin/invites/{invite_id}"
            and "DELETE" in r.methods
        ]
        assert len(delete) == 1


# ---------------------------------------------------------------------
# 7. /auth/set-password invite-purpose branch
# ---------------------------------------------------------------------


class TestAuthSetPasswordInviteBranch:
    def test_invite_branch_present_in_source(self):
        """The set_password route MUST detect purpose=='invite' and route
        to invite_service.redeem_invite. This is a source-level pin so
        the branch cannot be silently removed without this test failing.
        """
        auth_path = REPO_ROOT / "app" / "api" / "v1" / "auth.py"
        src = auth_path.read_text()
        assert 'purpose == "invite"' in src, (
            "POST /auth/set-password must branch on purpose=='invite'"
        )
        assert "invite_service.redeem_invite" in src or "_invite_service.redeem_invite" in src, (
            "POST /auth/set-password must call invite_service.redeem_invite "
            "on the invite-purpose branch"
        )


# ---------------------------------------------------------------------
# 8. Schemas
# ---------------------------------------------------------------------


class TestInviteSchemas:
    def test_create_payload_fields(self):
        from app.schemas.invite import UserInviteCreate
        fields = UserInviteCreate.model_fields
        assert "invited_email" in fields
        assert fields["invited_email"].is_required() is True
        assert "tenant_id" in fields
        assert "domain_id" in fields
        assert "role" in fields

    def test_read_response_fields(self):
        from app.schemas.invite import UserInviteRead
        fields = UserInviteRead.model_fields
        required = {
            "id",
            "tenant_id",
            "domain_id",
            "invited_email",
            "role",
            "status",
            "inviter_user_id",
            "expires_at",
            "accepted_at",
            "accepted_user_id",
            "revoked_at",
            "created_at",
        }
        missing = required - set(fields.keys())
        assert not missing, (
            f"UserInviteRead missing fields: {missing}"
        )

    def test_resend_and_revoke_response_shapes(self):
        from app.schemas.invite import (
            UserInviteResendResponse,
            UserInviteRevokeResponse,
        )
        assert "invite" in UserInviteResendResponse.model_fields
        assert "invite_id" in UserInviteRevokeResponse.model_fields


# ---------------------------------------------------------------------
# 9. Deprecation marker for the old /admin/luciel-instances overload
# ---------------------------------------------------------------------


class TestStep30a1TeammateOverloadDeprecated:
    def test_admin_py_carries_deprecated_log_marker(self):
        """Step 30a.1 commit G's teammate_email overload is marked
        DEPRECATED with a stable log marker so any production traffic
        against the old path is visible in CloudWatch. Removal happens
        at Step 30a.5.
        """
        admin_src = (REPO_ROOT / "app" / "api" / "v1" / "admin.py").read_text()
        assert "[DEPRECATED] /admin/luciel-instances teammate_email overload" in admin_src

    def test_admin_py_uses_set_password_token_not_magic_link(self):
        """The drift was: minted mint_magic_link_token + sent
        send_magic_link_email. Fix: mint_set_password_token(purpose='invite')
        + send_welcome_set_password_email(purpose='invite').
        Pin both signals at source level.
        """
        admin_src = (REPO_ROOT / "app" / "api" / "v1" / "admin.py").read_text()
        # The DEPRECATED block uses set-password primitives.
        marker = "[DEPRECATED] /admin/luciel-instances teammate_email overload"
        idx = admin_src.find(marker)
        assert idx > 0, "DEPRECATED block must be present"
        # Scan the following 1200 chars for the corrected primitives.
        window = admin_src[idx : idx + 1200]
        assert "mint_set_password_token" in window
        assert "send_welcome_set_password_email" in window
        assert 'purpose="invite"' in window
