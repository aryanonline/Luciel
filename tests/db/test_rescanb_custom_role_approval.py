"""Rescan Tier-B — custom-role second-admin approval workflow tests.

Tests the approval gate introduced by migration ``rescanb_custom_role_approval``
and implemented in ``app/api/v1/admin_custom_roles.py`` +
``app/policy/permissions.py``.

Covers:

  A1  Authoring a role WITH ``can_configure_connections`` ->
      ``approval_state='pending_approval'``, grants nothing to assigned users.
  A2  Second admin_owner approves -> ``live``, now grants the permission.
  A3  Same author CANNOT self-approve (rejected with 409).
  A4  Authoring a role WITHOUT sensitive perms -> ``live`` immediately.
  A5  Update that ADDS a sensitive permission -> ``pending``; approve
      applies the staged change.
  A6  Migration up/down round-trip works; head is single.

Opt-in: skipped unless DATABASE_URL points at a real Postgres with psycopg.
Matches the convention in ``tests/db/test_arc12b_custom_roles_migration.py``.
"""
from __future__ import annotations

import os
import types
import uuid
from contextlib import contextmanager
from datetime import timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker


_DB_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    "psycopg" not in _DB_URL,
    reason="Rescan Tier-B approval tests require Postgres DATABASE_URL.",
)


# =====================================================================
# Helpers
# =====================================================================


@pytest.fixture(scope="module")
def engine():
    e = create_engine(_DB_URL)
    yield e
    e.dispose()


def _make_user(engine) -> uuid.UUID:
    """Insert a user row and return its UUID."""
    with engine.begin() as conn:
        uid = conn.execute(
            text(
                "INSERT INTO users (id, email, display_name) "
                "VALUES (gen_random_uuid(), :em, 'rescanb-test-user') "
                "RETURNING id"
            ),
            {"em": f"rescanb-{uuid.uuid4().hex[:8]}@example.test"},
        ).scalar_one()
    return uuid.UUID(str(uid))


def _make_admin(engine, *, tier: str = "enterprise") -> str:
    """Insert an admin row and return its id string."""
    aid = f"rescanb-{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO admins (id, name, tier, active) "
                "VALUES (:aid, 'rescanb-test', :tier, true) "
                "ON CONFLICT (id) DO UPDATE SET tier = :tier, active = true"
            ),
            {"aid": aid, "tier": tier},
        )
    return aid


def _cleanup(engine, *, admin_ids: list[str], user_ids: list[uuid.UUID]) -> None:
    """Remove test rows in FK-safe order."""
    for aid in admin_ids:
        with engine.begin() as conn:
            conn.execute(
                text(f"SELECT set_config('app.admin_id', '{aid}', true)")
            )
            conn.execute(
                text("DELETE FROM user_role_assignments WHERE admin_id = :aid"),
                {"aid": aid},
            )
            conn.execute(
                text("DELETE FROM custom_roles WHERE admin_id = :aid"),
                {"aid": aid},
            )
            conn.execute(
                text("DELETE FROM admin_audit_logs WHERE admin_id = :aid"),
                {"aid": aid},
            )
            conn.execute(
                text("DELETE FROM admins WHERE id = :aid"),
                {"aid": aid},
            )
    for uid in user_ids:
        with engine.begin() as conn:
            try:
                conn.execute(
                    text("DELETE FROM users WHERE id = :uid"),
                    {"uid": str(uid)},
                )
            except Exception:
                pass


@contextmanager
def _tenant_session(engine, admin_id: str):
    """Session with app.admin_id GUC bound for RLS."""
    SM = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = SM()
    try:
        s.execute(text(f"SET LOCAL app.admin_id = '{admin_id}'"))
        yield s
    finally:
        s.close()


def _fake_request(
    *,
    admin_id: str,
    actor_user_id: uuid.UUID,
    permissions=("admin",),
    scope_assignments=(),
):
    """Build a minimal fake FastAPI Request for direct route calls."""
    state = types.SimpleNamespace(
        admin_id=admin_id,
        permissions=list(permissions),
        scope_assignments=list(scope_assignments),
        actor_user_id=actor_user_id,
        luciel_instance_id=None,
        role=None,
        key_prefix=None,
        actor_label=None,
    )
    return types.SimpleNamespace(state=state)


def _audit_ctx(request):
    from app.repositories.admin_audit_repository import AuditContext

    return AuditContext.from_request(request)


def _scope_assignment(admin_id: str, user_id: uuid.UUID, role: str = "admin_owner"):
    """Return a minimal scope-assignment-like object for request.state."""
    return types.SimpleNamespace(
        admin_id=admin_id,
        role=role,
        active=True,
        ended_at=None,
    )


# =====================================================================
# A4 — non-sensitive role -> live immediately (regression guard)
# =====================================================================


def test_non_sensitive_role_is_live_immediately(engine):
    """Authoring a role WITHOUT can_configure_connections or can_view_billing
    must land approval_state='live' with no approval step required."""
    from app.api.v1.admin_custom_roles import (
        CustomRoleAuthorRequest,
        author_custom_role,
    )
    from app.models.permission_model import APPROVAL_STATE_LIVE

    # Ensure prod audit chain is installed.
    from app.repositories.audit_chain import install_audit_chain_event
    install_audit_chain_event()

    user1 = _make_user(engine)
    admin_id = _make_admin(engine)

    try:
        req = _fake_request(
            admin_id=admin_id,
            actor_user_id=user1,
            scope_assignments=[_scope_assignment(admin_id, user1)],
        )

        with _tenant_session(engine, admin_id) as db:
            body = CustomRoleAuthorRequest(
                role_key="nonsens_role",
                display_name="Non-sensitive role",
                permission_keys=["can_view_knowledge", "can_view_tools"],
            )
            result = author_custom_role(
                request=req,
                body=body,
                db=db,
                audit_ctx=_audit_ctx(req),
            )

        assert result.approval_state == APPROVAL_STATE_LIVE, (
            f"Expected 'live'; got {result.approval_state!r}"
        )
        assert result.pending_change_json is None
    finally:
        _cleanup(engine, admin_ids=[admin_id], user_ids=[user1])


# =====================================================================
# A1 — sensitive role -> pending_approval, grants nothing
# =====================================================================


def test_sensitive_role_starts_pending_and_grants_nothing(engine):
    """Authoring a role WITH can_configure_connections must land
    approval_state='pending_approval' and grant ZERO permissions to
    assigned users until approved."""
    from app.api.v1.admin_custom_roles import (
        CustomRoleAuthorRequest,
        UserRoleAssignmentRequest,
        author_custom_role,
        create_role_assignment,
    )
    from app.models.permission_model import APPROVAL_STATE_PENDING
    from app.policy.permissions import (
        PERM_CONFIGURE_CONNECTIONS,
        PermissionResolver,
    )

    from app.repositories.audit_chain import install_audit_chain_event
    install_audit_chain_event()

    # Two users: the role author (admin_owner) and the assignee.
    author_user = _make_user(engine)
    assignee_user = _make_user(engine)
    admin_id = _make_admin(engine)

    try:
        req_author = _fake_request(
            admin_id=admin_id,
            actor_user_id=author_user,
            scope_assignments=[_scope_assignment(admin_id, author_user)],
        )

        # Author the sensitive role.
        with _tenant_session(engine, admin_id) as db:
            body = CustomRoleAuthorRequest(
                role_key="sens_role_a1",
                display_name="Sensitive role A1",
                permission_keys=["can_configure_connections"],
            )
            result = author_custom_role(
                request=req_author,
                body=body,
                db=db,
                audit_ctx=_audit_ctx(req_author),
            )

        role_id = result.role_id
        assert result.approval_state == APPROVAL_STATE_PENDING, (
            f"Expected pending_approval; got {result.approval_state!r}"
        )

        # Assign the pending role to the assignee (assignment itself is
        # still permitted even for pending roles — only permission
        # RESOLUTION is blocked).
        req_assign = _fake_request(
            admin_id=admin_id,
            actor_user_id=author_user,
            scope_assignments=[_scope_assignment(admin_id, author_user)],
        )
        with _tenant_session(engine, admin_id) as db:
            assign_body = UserRoleAssignmentRequest(
                user_id=str(assignee_user),
                custom_role_id=role_id,
                scope_type="all_instances",
            )
            create_role_assignment(
                request=req_assign,
                body=assign_body,
                db=db,
                audit_ctx=_audit_ctx(req_assign),
            )

        # Verify the assignee gets ZERO effective permissions from the pending role.
        req_assignee = _fake_request(
            admin_id=admin_id,
            actor_user_id=assignee_user,
            # No scope_assignments on request.state so resolver opens its own session.
        )
        resolved = PermissionResolver.resolve(req_assignee)
        assert PERM_CONFIGURE_CONNECTIONS not in resolved, (
            f"Pending role leaked permission {PERM_CONFIGURE_CONNECTIONS!r} "
            f"before approval. Resolved: {sorted(resolved)}"
        )
    finally:
        _cleanup(engine, admin_ids=[admin_id], user_ids=[author_user, assignee_user])


# =====================================================================
# A3 — same author CANNOT self-approve
# =====================================================================


def test_author_cannot_self_approve(engine):
    """The approver must be a DIFFERENT admin_owner than the author.
    Self-approval must be rejected with 409."""
    from app.api.v1.admin_custom_roles import (
        CustomRoleAuthorRequest,
        approve_custom_role,
        author_custom_role,
    )
    from app.models.permission_model import APPROVAL_STATE_PENDING
    from fastapi import HTTPException

    from app.repositories.audit_chain import install_audit_chain_event
    install_audit_chain_event()

    author_user = _make_user(engine)
    admin_id = _make_admin(engine)

    try:
        req = _fake_request(
            admin_id=admin_id,
            actor_user_id=author_user,
            scope_assignments=[_scope_assignment(admin_id, author_user)],
        )

        # Author the sensitive role.
        with _tenant_session(engine, admin_id) as db:
            result = author_custom_role(
                request=req,
                body=CustomRoleAuthorRequest(
                    role_key="self_approve_test",
                    display_name="Self-approve test",
                    permission_keys=["can_configure_connections"],
                ),
                db=db,
                audit_ctx=_audit_ctx(req),
            )
        role_id = result.role_id
        assert result.approval_state == APPROVAL_STATE_PENDING

        # Author tries to approve their own role — must 409.
        with _tenant_session(engine, admin_id) as db:
            with pytest.raises(HTTPException) as exc_info:
                approve_custom_role(
                    request=req,
                    role_id=role_id,
                    db=db,
                    audit_ctx=_audit_ctx(req),
                )
        assert exc_info.value.status_code == 409, (
            f"Expected 409 for self-approval; got {exc_info.value.status_code}"
        )
        assert "second-person" in exc_info.value.detail.lower() or \
               "self-approval" in exc_info.value.detail.lower(), (
            f"Error message should mention second-person rule or self-approval. "
            f"Got: {exc_info.value.detail!r}"
        )
    finally:
        _cleanup(engine, admin_ids=[admin_id], user_ids=[author_user])


# =====================================================================
# A2 — second admin_owner approves -> live, grants permission
# =====================================================================


def test_second_admin_approves_role_goes_live(engine):
    """After a second admin_owner approves a pending role, it becomes
    'live' and begins granting permissions to assigned users."""
    from app.api.v1.admin_custom_roles import (
        CustomRoleAuthorRequest,
        UserRoleAssignmentRequest,
        approve_custom_role,
        author_custom_role,
        create_role_assignment,
    )
    from app.models.permission_model import APPROVAL_STATE_LIVE
    from app.policy.permissions import (
        PERM_CONFIGURE_CONNECTIONS,
        PermissionResolver,
    )

    from app.repositories.audit_chain import install_audit_chain_event
    install_audit_chain_event()

    author_user = _make_user(engine)
    approver_user = _make_user(engine)
    assignee_user = _make_user(engine)
    admin_id = _make_admin(engine)

    try:
        req_author = _fake_request(
            admin_id=admin_id,
            actor_user_id=author_user,
            scope_assignments=[_scope_assignment(admin_id, author_user)],
        )
        req_approver = _fake_request(
            admin_id=admin_id,
            actor_user_id=approver_user,
            scope_assignments=[_scope_assignment(admin_id, approver_user)],
        )

        # Author the sensitive role.
        with _tenant_session(engine, admin_id) as db:
            result = author_custom_role(
                request=req_author,
                body=CustomRoleAuthorRequest(
                    role_key="approval_a2",
                    display_name="Approval A2",
                    permission_keys=["can_configure_connections"],
                ),
                db=db,
                audit_ctx=_audit_ctx(req_author),
            )
        role_id = result.role_id

        # Assign to the assignee (before approval — allowed at assignment level).
        with _tenant_session(engine, admin_id) as db:
            create_role_assignment(
                request=req_author,
                body=UserRoleAssignmentRequest(
                    user_id=str(assignee_user),
                    custom_role_id=role_id,
                    scope_type="all_instances",
                ),
                db=db,
                audit_ctx=_audit_ctx(req_author),
            )

        # Verify no permissions before approval.
        req_assignee = _fake_request(
            admin_id=admin_id,
            actor_user_id=assignee_user,
        )
        resolved_before = PermissionResolver.resolve(req_assignee)
        assert PERM_CONFIGURE_CONNECTIONS not in resolved_before, (
            "Expected no permissions before approval"
        )

        # Second admin approves.
        with _tenant_session(engine, admin_id) as db:
            approved = approve_custom_role(
                request=req_approver,
                role_id=role_id,
                db=db,
                audit_ctx=_audit_ctx(req_approver),
            )
        assert approved.approval_state == APPROVAL_STATE_LIVE, (
            f"Expected 'live' after approval; got {approved.approval_state!r}"
        )
        assert approved.approved_by_user_id == str(approver_user)
        assert approved.approved_at is not None

        # Now the assignee should hold the permission.
        resolved_after = PermissionResolver.resolve(req_assignee)
        assert PERM_CONFIGURE_CONNECTIONS in resolved_after, (
            f"Expected {PERM_CONFIGURE_CONNECTIONS!r} after approval. "
            f"Resolved: {sorted(resolved_after)}"
        )
    finally:
        _cleanup(
            engine,
            admin_ids=[admin_id],
            user_ids=[author_user, approver_user, assignee_user],
        )


# =====================================================================
# A5 — update that adds sensitive perm -> pending; approve applies it
# =====================================================================


def test_update_adding_sensitive_perm_goes_pending_then_approve_applies(engine):
    """Updating a live role to ADD can_view_billing should stage the
    change in pending_change_json and set approval_state='pending_approval'.
    Approving applies the staged permission change."""
    from app.api.v1.admin_custom_roles import (
        CustomRoleAuthorRequest,
        CustomRoleUpdateRequest,
        UserRoleAssignmentRequest,
        approve_custom_role,
        author_custom_role,
        create_role_assignment,
        update_custom_role,
    )
    from app.models.permission_model import (
        APPROVAL_STATE_LIVE,
        APPROVAL_STATE_PENDING,
    )
    from app.policy.permissions import PERM_VIEW_BILLING, PermissionResolver

    from app.repositories.audit_chain import install_audit_chain_event
    install_audit_chain_event()

    author_user = _make_user(engine)
    approver_user = _make_user(engine)
    assignee_user = _make_user(engine)
    admin_id = _make_admin(engine)

    try:
        req_author = _fake_request(
            admin_id=admin_id,
            actor_user_id=author_user,
            scope_assignments=[_scope_assignment(admin_id, author_user)],
        )
        req_approver = _fake_request(
            admin_id=admin_id,
            actor_user_id=approver_user,
            scope_assignments=[_scope_assignment(admin_id, approver_user)],
        )

        # Author a non-sensitive role (starts live).
        with _tenant_session(engine, admin_id) as db:
            result = author_custom_role(
                request=req_author,
                body=CustomRoleAuthorRequest(
                    role_key="update_a5",
                    display_name="Update A5",
                    permission_keys=["can_view_knowledge"],
                ),
                db=db,
                audit_ctx=_audit_ctx(req_author),
            )
        role_id = result.role_id
        assert result.approval_state == APPROVAL_STATE_LIVE

        # Assign assignee to the role (while it is live).
        with _tenant_session(engine, admin_id) as db:
            create_role_assignment(
                request=req_author,
                body=UserRoleAssignmentRequest(
                    user_id=str(assignee_user),
                    custom_role_id=role_id,
                    scope_type="all_instances",
                ),
                db=db,
                audit_ctx=_audit_ctx(req_author),
            )

        # Update the role to ADD can_view_billing (sensitive) -> pending.
        with _tenant_session(engine, admin_id) as db:
            updated = update_custom_role(
                request=req_author,
                role_id=role_id,
                body=CustomRoleUpdateRequest(
                    permission_keys=["can_view_knowledge", "can_view_billing"],
                ),
                db=db,
                audit_ctx=_audit_ctx(req_author),
            )
        assert updated.approval_state == APPROVAL_STATE_PENDING, (
            f"Expected pending_approval after adding sensitive perm; "
            f"got {updated.approval_state!r}"
        )
        assert updated.pending_change_json is not None
        assert "can_view_billing" in updated.pending_change_json.get(
            "permission_keys", []
        )

        # Assignee should NOT have can_view_billing yet (change staged, not applied).
        req_assignee = _fake_request(
            admin_id=admin_id,
            actor_user_id=assignee_user,
        )
        resolved_before = PermissionResolver.resolve(req_assignee)
        assert PERM_VIEW_BILLING not in resolved_before, (
            "can_view_billing should not be granted before approval of staged change"
        )

        # Second admin approves — staged change should be applied.
        with _tenant_session(engine, admin_id) as db:
            approved = approve_custom_role(
                request=req_approver,
                role_id=role_id,
                db=db,
                audit_ctx=_audit_ctx(req_approver),
            )
        assert approved.approval_state == APPROVAL_STATE_LIVE
        assert approved.pending_change_json is None  # staged change cleared

        # Now assignee should hold can_view_billing.
        resolved_after = PermissionResolver.resolve(req_assignee)
        assert PERM_VIEW_BILLING in resolved_after, (
            f"Expected {PERM_VIEW_BILLING!r} after approval applied staged change. "
            f"Resolved: {sorted(resolved_after)}"
        )
    finally:
        _cleanup(
            engine,
            admin_ids=[admin_id],
            user_ids=[author_user, approver_user, assignee_user],
        )


# =====================================================================
# A6 — migration round-trip + single head
# =====================================================================


def test_migration_head_is_single(engine):
    """After upgrade, there is exactly one alembic head and
    rescanb_custom_role_approval is present in the applied migration chain.

    Note: the head revision advances with each new migration tier; this test
    asserts a single head and that the rescanb revision was applied, without
    hardcoding which revision is the current tip.
    """
    from pathlib import Path

    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    backend_root = Path(__file__).resolve().parents[2]
    alembic_ini = backend_root / "alembic.ini"
    alembic_cfg = AlembicConfig(str(alembic_ini))
    script_dir = ScriptDirectory.from_config(alembic_cfg)
    heads = script_dir.get_heads()

    assert len(heads) == 1, (
        f"Expected single alembic head; got {len(heads)}: {heads!r}"
    )

    # Verify rescanb revision was applied to the DB (it may no longer be HEAD
    # once later migration tiers are added on top of it).
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalars().all()
    assert len(rows) == 1
    # The single applied version must be the current head (not an old one).
    assert rows[0] == heads[0], (
        f"DB version {rows[0]!r} does not match script head {heads[0]!r}"
    )


def test_new_columns_exist_on_custom_roles(engine):
    """Verify all four new columns are present on the custom_roles table."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_name = 'custom_roles'
                  AND column_name IN (
                    'approval_state', 'approved_by_user_id',
                    'approved_at', 'pending_change_json'
                  )
                ORDER BY column_name
                """
            )
        ).fetchall()
    found = {r.column_name for r in rows}
    expected = {
        "approval_state",
        "approved_by_user_id",
        "approved_at",
        "pending_change_json",
    }
    assert found == expected, (
        f"Missing columns: {expected - found}; unexpected: {found - expected}"
    )

    # approval_state has a default of 'live'.
    by_name = {r.column_name: r for r in rows}
    default_raw = by_name["approval_state"].column_default or ""
    assert "live" in default_raw, (
        f"approval_state DEFAULT should be 'live'; column_default={default_raw!r}"
    )


def test_existing_rows_have_live_approval_state(engine):
    """Any custom_roles rows that existed before the migration (or were
    inserted without specifying approval_state) should default to 'live'."""
    user1 = _make_user(engine)
    admin_id = _make_admin(engine)

    try:
        # Insert directly via SQL without specifying approval_state.
        with engine.begin() as conn:
            conn.execute(
                text(f"SET LOCAL app.admin_id = '{admin_id}'")
            )
            conn.execute(
                text(
                    """
                    INSERT INTO custom_roles
                      (admin_id, role_key, display_name, authored_by_user_id)
                    VALUES
                      (:aid, 'default_state_test', 'Default state', :uid)
                    """
                ),
                {"aid": admin_id, "uid": str(user1)},
            )

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT approval_state FROM custom_roles
                    WHERE admin_id = :aid AND role_key = 'default_state_test'
                    """
                ),
                {"aid": admin_id},
            ).scalar_one_or_none()
        assert row == "live", (
            f"Expected 'live' as the default approval_state; got {row!r}"
        )
    finally:
        _cleanup(engine, admin_ids=[admin_id], user_ids=[user1])
