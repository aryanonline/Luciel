"""Arc 12 WU4 — sibling-grant route-layer Wall-2 enforcement tests.

The full route stack (FastAPI + RLS + middleware) is exercised in
the live-Postgres integration suite; here we focus the unit tests
on the load-bearing Wall-2 invariant:

  * A user scoped to ONLY the caller Instance cannot author a grant
    that names a callee they don't own → 403.
  * A user scoped to ONLY the callee Instance cannot author a grant
    that names a caller they don't own → 403.
  * A user scoped to BOTH instances (admin_owner or admin_manager)
    CAN author the grant.

We exercise the route helpers directly with a synthesised Request
state because the goal is the ScopePolicy.enforce_role_on_instance
calls firing twice, not the FastAPI plumbing.

Additional route-level shape tests:
  * Routes register at the right paths + methods.
  * instance_operator role is NOT in the author/approve/reject/
    revoke allowed-role frozensets.
"""
from __future__ import annotations

import os
import uuid
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("OPENAI_API_KEY", "dummy")


def _fake_request(
    *,
    admin_id: str,
    actor_user_id: uuid.UUID,
    role: str | None = None,
    permissions: list[str] | None = None,
):
    """Build a MagicMock with the same surface ScopePolicy reads:
    request.state.admin_id, .actor_user_id, .role, .permissions,
    .scope_assignments, .luciel_instance_id.
    """
    request = MagicMock()
    request.state.admin_id = admin_id
    request.state.actor_user_id = actor_user_id
    request.state.role = role
    request.state.permissions = permissions or []
    request.state.scope_assignments = None
    request.state.luciel_instance_id = None
    return request


def _fake_instance(*, instance_id: int, admin_id: str):
    inst = MagicMock()
    inst.id = instance_id
    inst.admin_id = admin_id
    inst.active = True
    inst.instance_slug = f"inst-{instance_id}"
    return inst


# =====================================================================
# 1. Routes register at the right paths
# =====================================================================


def test_sibling_grant_routes_registered() -> None:
    from app.api.v1 import admin_sibling_grants

    paths = {(r.path, tuple(sorted(r.methods))) for r in admin_sibling_grants.router.routes}
    assert ("/admin/sibling-grants", ("POST",)) in paths
    assert ("/admin/sibling-grants", ("GET",)) in paths
    assert ("/admin/sibling-grants/{grant_id}/approve", ("POST",)) in paths
    assert ("/admin/sibling-grants/{grant_id}/reject", ("POST",)) in paths
    assert ("/admin/sibling-grants/{grant_id}/revoke", ("POST",)) in paths


def test_sibling_grants_router_mounted_in_api_router() -> None:
    """The router is included in app.api.router.api_router so the
    routes are exposed at /api/v1/admin/sibling-grants/*."""
    from app.api.router import api_router

    paths = {r.path for r in api_router.routes}
    assert "/admin/sibling-grants" in paths


# =====================================================================
# 2. Wall-2 role gate sets
# =====================================================================


def test_role_gates_exclude_operator_and_viewer() -> None:
    """instance_operator and read_only_viewer must not be able to
    author/approve/reject/revoke sibling grants — the binding spec
    locks these as owner/manager-level operations."""
    from app.api.v1.admin_sibling_grants import (
        _APPROVE_ROLES,
        _AUTHOR_ROLES,
        _REJECT_ROLES,
        _REVOKE_ROLES,
    )
    from app.policy.scope import (
        ROLE_ADMIN_MANAGER,
        ROLE_ADMIN_OWNER,
        ROLE_INSTANCE_OPERATOR,
        ROLE_READ_ONLY_VIEWER,
    )

    for gate in (_AUTHOR_ROLES, _APPROVE_ROLES, _REJECT_ROLES, _REVOKE_ROLES):
        assert ROLE_INSTANCE_OPERATOR not in gate, (
            f"instance_operator must NOT be allowed; got gate={gate}"
        )
        assert ROLE_READ_ONLY_VIEWER not in gate, (
            f"read_only_viewer must NOT be allowed; got gate={gate}"
        )

    # Approve narrows to owner only (per §3.3.4).
    assert _APPROVE_ROLES == frozenset({ROLE_ADMIN_OWNER})
    # Author/reject/revoke allow owner + manager.
    assert ROLE_ADMIN_OWNER in _AUTHOR_ROLES
    assert ROLE_ADMIN_MANAGER in _AUTHOR_ROLES


# =====================================================================
# 3. Wall-2 enforcement helper — both instances must pass the gate
# =====================================================================


def test_wall_2_helper_runs_role_check_on_both_instances() -> None:
    """The route's _enforce_wall2_both_instances helper must invoke
    ScopePolicy.enforce_role_on_instance TWICE (caller + callee) so
    a user scoped to one Instance fails on the other."""
    from unittest.mock import patch

    from app.api.v1.admin_sibling_grants import (
        _AUTHOR_ROLES,
        _enforce_wall2_both_instances,
    )

    admin_id = "admin-pro"
    user_id = uuid.uuid4()
    request = _fake_request(admin_id=admin_id, actor_user_id=user_id)
    caller = _fake_instance(instance_id=1, admin_id=admin_id)
    callee = _fake_instance(instance_id=2, admin_id=admin_id)

    with patch(
        "app.api.v1.admin_sibling_grants.ScopePolicy.enforce_role_on_instance"
    ) as mock_enforce:
        _enforce_wall2_both_instances(
            request=request,
            caller=caller,
            callee=callee,
            allowed_roles=_AUTHOR_ROLES,
        )
    # Called twice — once per Instance.
    assert mock_enforce.call_count == 2
    # First call is the caller, second is the callee.
    first_args = mock_enforce.call_args_list[0]
    second_args = mock_enforce.call_args_list[1]
    assert first_args.args[1] is caller
    assert second_args.args[1] is callee


def test_wall_2_user_scoped_to_only_caller_gets_403_on_callee() -> None:
    """The load-bearing Wall-2 property: if the second
    enforce_role_on_instance raises 403 (because the user has no
    scope on the callee), the helper propagates the 403."""
    from fastapi import HTTPException, status
    from unittest.mock import patch

    from app.api.v1.admin_sibling_grants import (
        _AUTHOR_ROLES,
        _enforce_wall2_both_instances,
    )

    request = _fake_request(admin_id="admin-pro", actor_user_id=uuid.uuid4())
    caller = _fake_instance(instance_id=1, admin_id="admin-pro")
    callee = _fake_instance(instance_id=2, admin_id="admin-pro")

    call_count = {"n": 0}

    def fake_enforce(request, instance, *, allowed_roles):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No scope on this instance",
            )

    with patch(
        "app.api.v1.admin_sibling_grants.ScopePolicy.enforce_role_on_instance",
        side_effect=fake_enforce,
    ):
        with pytest.raises(HTTPException) as exc:
            _enforce_wall2_both_instances(
                request=request,
                caller=caller,
                callee=callee,
                allowed_roles=_AUTHOR_ROLES,
            )
    assert exc.value.status_code == 403


def test_wall_2_user_scoped_to_only_callee_gets_403_on_caller() -> None:
    """Symmetric to the previous test: missing scope on the FIRST
    Instance (caller) raises 403 on the first call."""
    from fastapi import HTTPException, status
    from unittest.mock import patch

    from app.api.v1.admin_sibling_grants import (
        _AUTHOR_ROLES,
        _enforce_wall2_both_instances,
    )

    request = _fake_request(admin_id="admin-pro", actor_user_id=uuid.uuid4())
    caller = _fake_instance(instance_id=1, admin_id="admin-pro")
    callee = _fake_instance(instance_id=2, admin_id="admin-pro")

    def fake_enforce(request, instance, *, allowed_roles):
        # First call (caller) raises.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No scope on this instance",
        )

    with patch(
        "app.api.v1.admin_sibling_grants.ScopePolicy.enforce_role_on_instance",
        side_effect=fake_enforce,
    ):
        with pytest.raises(HTTPException) as exc:
            _enforce_wall2_both_instances(
                request=request,
                caller=caller,
                callee=callee,
                allowed_roles=_AUTHOR_ROLES,
            )
    assert exc.value.status_code == 403
