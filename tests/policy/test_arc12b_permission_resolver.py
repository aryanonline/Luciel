"""Arc 12b — unified permission resolver behaviour.

Covers:

  R1   Resolver returns PLATFORM_ADMIN_ALL for a platform_admin caller;
       any permission key satisfies the membership check.
  R2   With no admin_id bound, resolver returns the empty set (fail-closed).
  R3   Resolver returns the locked-role set for a caller whose
       scope_assignments carry exactly that role (zero behavioural change
       — the set matches LOCKED_ROLE_PERMISSIONS_FALLBACK for that role).
  R4   instance_operator role contributes its permissions ONLY when the
       call targets the bound instance_id; admin-scoped calls and calls
       targeting a different Instance get nothing from it.
  R5   admin_owner / admin_manager / read_only_viewer contribute across
       all Instances under the bound Admin (no instance scoping).
  R6   Pre-resolved single-role fast path (``request.state.role``) is
       respected.
  R7   ``enforce_role_on_instance`` continues to accept the same role
       sets it accepted before Arc 12b (zero behavioural change on
       knowledge matrix + sibling-grant + admin_tools role sets).
  R8   ``enforce_action`` honours the legacy transport-layer permissions
       tuple (``["admin","chat","sessions"]``) AND the resolver's
       permission set.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.models.scope_assignment import ScopeRole
from app.policy.permissions import (
    ALL_PERMISSIONS,
    LOCKED_ROLE_PERMISSIONS_FALLBACK,
    PERM_AUTHOR_CUSTOM_ROLES,
    PERM_AUTHOR_SIBLING_GRANTS,
    PERM_EDIT_KNOWLEDGE,
    PERM_VIEW_KNOWLEDGE,
    PERM_VIEW_TOOLS,
    PLATFORM_ADMIN_ALL,
    PermissionResolver,
    caller_holds_permission,
)
from app.policy.scope import (
    PLATFORM_ADMIN,
    ROLE_ADMIN_MANAGER,
    ROLE_ADMIN_OWNER,
    ROLE_INSTANCE_OPERATOR,
    ROLE_READ_ONLY_VIEWER,
    ScopePolicy,
)


def _fake_assignment(*, admin_id, role, active=True, ended_at=None):
    sa = types.SimpleNamespace(
        admin_id=admin_id,
        role=role,
        active=active,
        ended_at=ended_at,
    )
    return sa


def _fake_request(
    *,
    admin_id="adm-a",
    permissions=("admin",),
    scope_assignments=(),
    actor_user_id=None,
    luciel_instance_id=None,
    role=None,
):
    state = types.SimpleNamespace(
        admin_id=admin_id,
        permissions=list(permissions),
        scope_assignments=list(scope_assignments),
        actor_user_id=actor_user_id,
        luciel_instance_id=luciel_instance_id,
        role=role,
    )
    return types.SimpleNamespace(state=state)


def _fake_instance(*, instance_id=1, admin_id="adm-a"):
    return types.SimpleNamespace(id=instance_id, admin_id=admin_id)


# =====================================================================
# R1 — platform_admin returns PLATFORM_ADMIN_ALL
# =====================================================================


def test_resolver_platform_admin_returns_sentinel():
    req = _fake_request(permissions=(PLATFORM_ADMIN, "admin"))
    resolved = PermissionResolver.resolve(req, instance=_fake_instance())
    assert resolved is PLATFORM_ADMIN_ALL
    assert PERM_EDIT_KNOWLEDGE in resolved
    assert "anything_else" in resolved


# =====================================================================
# R2 — no admin_id bound → empty set
# =====================================================================


def test_resolver_no_admin_id_returns_empty():
    req = _fake_request(admin_id=None, permissions=())
    resolved = PermissionResolver.resolve(req, instance=_fake_instance())
    assert resolved == frozenset()


# =====================================================================
# R3 — locked-role set matches LOCKED_ROLE_PERMISSIONS_FALLBACK
# =====================================================================


@pytest.mark.parametrize(
    "role_str",
    ["admin_owner", "admin_manager", "read_only_viewer"],
)
def test_resolver_locked_role_yields_expected_set(role_str):
    """admin_owner / admin_manager / read_only_viewer hold scope at the
    Admin level; the resolved set equals the fallback's role mapping."""
    req = _fake_request(
        scope_assignments=[_fake_assignment(admin_id="adm-a", role=role_str)],
    )
    resolved = PermissionResolver.resolve(
        req, instance=_fake_instance(admin_id="adm-a")
    )
    assert resolved == LOCKED_ROLE_PERMISSIONS_FALLBACK[role_str]


# =====================================================================
# R4 — instance_operator scoping
# =====================================================================


def test_resolver_operator_contributes_on_bound_instance():
    req = _fake_request(
        scope_assignments=[
            _fake_assignment(admin_id="adm-a", role="instance_operator")
        ],
        luciel_instance_id=42,
    )
    resolved = PermissionResolver.resolve(
        req, instance=_fake_instance(instance_id=42, admin_id="adm-a")
    )
    assert resolved == LOCKED_ROLE_PERMISSIONS_FALLBACK["instance_operator"]
    assert PERM_VIEW_KNOWLEDGE in resolved
    assert PERM_VIEW_TOOLS in resolved


def test_resolver_operator_yields_empty_on_other_instance():
    req = _fake_request(
        scope_assignments=[
            _fake_assignment(admin_id="adm-a", role="instance_operator")
        ],
        luciel_instance_id=42,
    )
    resolved = PermissionResolver.resolve(
        req, instance=_fake_instance(instance_id=99, admin_id="adm-a")
    )
    assert resolved == frozenset()


def test_resolver_operator_yields_empty_on_admin_scoped_call():
    """Admin-wide call (no target Instance) — operator role does not
    contribute (operators have no Admin-wide authority).
    """
    req = _fake_request(
        scope_assignments=[
            _fake_assignment(admin_id="adm-a", role="instance_operator")
        ],
        luciel_instance_id=42,
    )
    resolved = PermissionResolver.resolve(req)
    assert resolved == frozenset()


# =====================================================================
# R5 — non-operator roles see every Instance under the Admin
# =====================================================================


def test_resolver_owner_contributes_on_any_instance():
    req = _fake_request(
        scope_assignments=[
            _fake_assignment(admin_id="adm-a", role="admin_owner")
        ],
    )
    for iid in (1, 2, 3, 99):
        resolved = PermissionResolver.resolve(
            req, instance=_fake_instance(instance_id=iid, admin_id="adm-a")
        )
        assert resolved == LOCKED_ROLE_PERMISSIONS_FALLBACK["admin_owner"]


# =====================================================================
# R6 — request.state.role fast path
# =====================================================================


def test_resolver_pre_resolved_role_fast_path():
    req = _fake_request(
        scope_assignments=(),
        role="admin_manager",
    )
    resolved = PermissionResolver.resolve(
        req, instance=_fake_instance(admin_id="adm-a")
    )
    assert resolved == LOCKED_ROLE_PERMISSIONS_FALLBACK["admin_manager"]


def test_resolver_pre_resolved_scoperole_enum_fast_path():
    req = _fake_request(
        scope_assignments=(),
        role=ScopeRole.ADMIN_OWNER,
    )
    resolved = PermissionResolver.resolve(
        req, instance=_fake_instance(admin_id="adm-a")
    )
    assert resolved == LOCKED_ROLE_PERMISSIONS_FALLBACK["admin_owner"]


# =====================================================================
# R7 — enforce_role_on_instance zero-behavioural-change preservation
# =====================================================================


def test_enforce_role_on_instance_owner_allowed():
    req = _fake_request(
        scope_assignments=[
            _fake_assignment(admin_id="adm-a", role="admin_owner")
        ],
    )
    ScopePolicy.enforce_role_on_instance(
        req,
        _fake_instance(admin_id="adm-a"),
        allowed_roles=frozenset({ROLE_ADMIN_OWNER, ROLE_ADMIN_MANAGER}),
    )


def test_enforce_role_on_instance_manager_satisfies_owner_or_manager():
    req = _fake_request(
        scope_assignments=[
            _fake_assignment(admin_id="adm-a", role="admin_manager")
        ],
    )
    ScopePolicy.enforce_role_on_instance(
        req,
        _fake_instance(admin_id="adm-a"),
        allowed_roles=frozenset({ROLE_ADMIN_OWNER, ROLE_ADMIN_MANAGER}),
    )


def test_enforce_role_on_instance_viewer_rejected_from_manager_gate():
    """read_only_viewer cannot pass a (owner, manager) gate — viewer's
    permission set is can_view_tools only, which is a strict subset of
    manager's set, so neither role's full set is satisfied."""
    req = _fake_request(
        scope_assignments=[
            _fake_assignment(admin_id="adm-a", role="read_only_viewer")
        ],
    )
    with pytest.raises(HTTPException) as exc:
        ScopePolicy.enforce_role_on_instance(
            req,
            _fake_instance(admin_id="adm-a"),
            allowed_roles=frozenset({ROLE_ADMIN_OWNER, ROLE_ADMIN_MANAGER}),
        )
    assert exc.value.status_code == 403


def test_enforce_role_on_instance_operator_bound_to_other_instance_rejected():
    req = _fake_request(
        scope_assignments=[
            _fake_assignment(admin_id="adm-a", role="instance_operator")
        ],
        luciel_instance_id=99,
    )
    with pytest.raises(HTTPException) as exc:
        ScopePolicy.enforce_role_on_instance(
            req,
            _fake_instance(instance_id=42, admin_id="adm-a"),
            allowed_roles=frozenset({
                ROLE_ADMIN_OWNER,
                ROLE_ADMIN_MANAGER,
                ROLE_INSTANCE_OPERATOR,
            }),
        )
    assert exc.value.status_code == 403


def test_enforce_role_on_instance_operator_on_bound_instance_allowed():
    req = _fake_request(
        scope_assignments=[
            _fake_assignment(admin_id="adm-a", role="instance_operator")
        ],
        luciel_instance_id=42,
    )
    ScopePolicy.enforce_role_on_instance(
        req,
        _fake_instance(instance_id=42, admin_id="adm-a"),
        allowed_roles=frozenset({
            ROLE_ADMIN_OWNER,
            ROLE_ADMIN_MANAGER,
            ROLE_INSTANCE_OPERATOR,
        }),
    )


def test_enforce_role_on_instance_no_assignment_rejected():
    req = _fake_request(scope_assignments=())
    with pytest.raises(HTTPException) as exc:
        ScopePolicy.enforce_role_on_instance(
            req,
            _fake_instance(admin_id="adm-a"),
            allowed_roles=frozenset({ROLE_ADMIN_OWNER}),
        )
    assert exc.value.status_code == 403


def test_enforce_role_on_instance_platform_admin_bypass():
    req = _fake_request(permissions=(PLATFORM_ADMIN, "admin"))
    ScopePolicy.enforce_role_on_instance(
        req,
        _fake_instance(admin_id="some-other-admin"),
        allowed_roles=frozenset({ROLE_ADMIN_OWNER}),
    )


# =====================================================================
# R8 — enforce_action transport + resolver
# =====================================================================


def test_enforce_action_transport_permission_satisfies():
    """Legacy transport-layer permission strings (e.g. "admin") still
    satisfy enforce_action."""
    req = _fake_request(permissions=("admin",))
    ScopePolicy.enforce_action(
        req, required_permission="admin", action_label="dummy"
    )


def test_enforce_action_can_permission_via_resolver_owner():
    req = _fake_request(
        scope_assignments=[
            _fake_assignment(admin_id="adm-a", role="admin_owner")
        ],
    )
    # admin_owner holds can_author_custom_roles; resolver path satisfies.
    ScopePolicy.enforce_action(
        req,
        required_permission=PERM_AUTHOR_CUSTOM_ROLES,
        action_label="author_custom_role",
    )


def test_enforce_action_can_permission_denied_for_manager():
    """admin_manager does NOT hold can_author_custom_roles."""
    req = _fake_request(
        scope_assignments=[
            _fake_assignment(admin_id="adm-a", role="admin_manager")
        ],
    )
    with pytest.raises(HTTPException) as exc:
        ScopePolicy.enforce_action(
            req,
            required_permission=PERM_AUTHOR_CUSTOM_ROLES,
            action_label="author_custom_role",
        )
    assert exc.value.status_code == 403


# =====================================================================
# R9 — locked-role seed integrity guard.
# =====================================================================


def test_owner_set_is_full_catalog():
    assert LOCKED_ROLE_PERMISSIONS_FALLBACK["admin_owner"] == ALL_PERMISSIONS


def test_manager_set_is_owner_minus_stewardship():
    diff = (
        LOCKED_ROLE_PERMISSIONS_FALLBACK["admin_owner"]
        - LOCKED_ROLE_PERMISSIONS_FALLBACK["admin_manager"]
    )
    assert diff == frozenset(
        {
            "can_approve_sibling_grants",
            "can_author_custom_roles",
            "can_view_billing",
            "can_assign_roles",
        }
    )


def test_operator_set_is_view_only():
    assert LOCKED_ROLE_PERMISSIONS_FALLBACK["instance_operator"] == frozenset(
        {"can_view_knowledge", "can_view_tools"}
    )


def test_viewer_set_is_tool_view_only():
    assert LOCKED_ROLE_PERMISSIONS_FALLBACK["read_only_viewer"] == frozenset(
        {"can_view_tools"}
    )


# =====================================================================
# R10 — caller_holds_permission convenience.
# =====================================================================


def test_caller_holds_permission_owner_holds_author_custom_roles():
    req = _fake_request(
        scope_assignments=[
            _fake_assignment(admin_id="adm-a", role="admin_owner")
        ],
    )
    assert caller_holds_permission(
        req, permission_key=PERM_AUTHOR_CUSTOM_ROLES
    ) is True


def test_caller_holds_permission_viewer_lacks_edit():
    req = _fake_request(
        scope_assignments=[
            _fake_assignment(admin_id="adm-a", role="read_only_viewer")
        ],
    )
    assert caller_holds_permission(
        req, permission_key=PERM_EDIT_KNOWLEDGE
    ) is False


# =====================================================================
# R11 — Wall-2 (Arc 12) sibling-grant: both endpoints, both roles.
# =====================================================================


def test_sibling_grant_wall2_holds_under_resolver():
    """Manager scoped to admin A passes the (owner, manager) gate on
    Instance 1 AND Instance 2 under admin A; rejected on Instance 3
    under admin B."""
    req = _fake_request(
        scope_assignments=[
            _fake_assignment(admin_id="adm-a", role="admin_manager")
        ],
    )
    ScopePolicy.enforce_role_on_instance(
        req,
        _fake_instance(instance_id=1, admin_id="adm-a"),
        allowed_roles=frozenset({ROLE_ADMIN_OWNER, ROLE_ADMIN_MANAGER}),
    )
    ScopePolicy.enforce_role_on_instance(
        req,
        _fake_instance(instance_id=2, admin_id="adm-a"),
        allowed_roles=frozenset({ROLE_ADMIN_OWNER, ROLE_ADMIN_MANAGER}),
    )
    # Cross-admin probe: caller has no scope on adm-b — must 403.
    with pytest.raises(HTTPException):
        ScopePolicy.enforce_role_on_instance(
            req,
            _fake_instance(instance_id=3, admin_id="adm-b"),
            allowed_roles=frozenset({ROLE_ADMIN_OWNER, ROLE_ADMIN_MANAGER}),
        )
