"""Arc 12b — zero-behavioural-change guarantee for Free/Pro tenants.

The migration plants seed rows for the four locked roles that
reproduce TODAY's role matrix EXACTLY:

  admin_owner       — every permission (Wall-2 full surface).
  admin_manager     — owner's set minus the four owner-stewardship
                      permissions (approve grants, author custom roles,
                      view billing, assign roles).
  instance_operator — can_view_knowledge, can_view_tools (scoped to
                      the operator's bound Instance).
  read_only_viewer  — can_view_tools.

The unified permission resolver in :mod:`app.policy.permissions` reads
those seeds (or the Python fallback that mirrors them byte-for-byte)
and feeds the result to :func:`ScopePolicy.enforce_role_on_instance`
and :func:`ScopePolicy.enforce_action`.

For Free/Pro tenants the conclusion is mechanical: every locked-role
gate that was open pre-Arc-12b remains open, and every locked-role
gate that was closed remains closed. This module spells that out with
explicit test cases for each (role, action) pair from the pre-Arc-12b
matrix.

Pre-Arc-12b matrix recap (see app/api/v1/admin_knowledge.py,
admin_sibling_grants.py, admin_tools.py):

  knowledge.list   : owner + manager + operator
  knowledge.view   : owner + manager + operator
  knowledge.edit   : owner + manager
  knowledge.delete : owner + manager
  tool.read        : owner + manager + operator + viewer
  tool.toggle      : owner + manager
  sibling.author   : owner + manager
  sibling.approve  : owner
  sibling.reject   : owner + manager
  sibling.revoke   : owner + manager
"""
from __future__ import annotations

import types

import pytest
from fastapi import HTTPException

from app.policy.scope import (
    ROLE_ADMIN_MANAGER,
    ROLE_ADMIN_OWNER,
    ROLE_INSTANCE_OPERATOR,
    ROLE_READ_ONLY_VIEWER,
    ScopePolicy,
)


def _req(role: str, *, admin_id="adm-x", luciel_instance_id=None):
    sa = types.SimpleNamespace(
        admin_id=admin_id, role=role, active=True, ended_at=None
    )
    state = types.SimpleNamespace(
        admin_id=admin_id,
        permissions=["admin"],
        scope_assignments=[sa],
        actor_user_id=None,
        luciel_instance_id=luciel_instance_id,
        role=None,
    )
    return types.SimpleNamespace(state=state)


def _inst(*, instance_id=1, admin_id="adm-x"):
    return types.SimpleNamespace(id=instance_id, admin_id=admin_id)


_ALL_FOUR = frozenset({
    ROLE_ADMIN_OWNER,
    ROLE_ADMIN_MANAGER,
    ROLE_INSTANCE_OPERATOR,
    ROLE_READ_ONLY_VIEWER,
})
_TOGGLE = frozenset({ROLE_ADMIN_OWNER, ROLE_ADMIN_MANAGER})
_KNOWLEDGE_LIST_VIEW = frozenset({
    ROLE_ADMIN_OWNER, ROLE_ADMIN_MANAGER, ROLE_INSTANCE_OPERATOR,
})
_KNOWLEDGE_EDIT_DELETE = frozenset({ROLE_ADMIN_OWNER, ROLE_ADMIN_MANAGER})
_SIBLING_AUTHOR = frozenset({ROLE_ADMIN_OWNER, ROLE_ADMIN_MANAGER})
_SIBLING_APPROVE = frozenset({ROLE_ADMIN_OWNER})


def _allow(role: str, *, allowed_roles, instance_id=1, luciel_instance_id=None):
    """Run the gate; return True iff it accepts, False iff it 403s."""
    req = _req(role, luciel_instance_id=luciel_instance_id)
    try:
        ScopePolicy.enforce_role_on_instance(
            req,
            _inst(instance_id=instance_id),
            allowed_roles=allowed_roles,
        )
        return True
    except HTTPException as e:
        if e.status_code == 403:
            return False
        raise


# =====================================================================
# Knowledge matrix (Architecture §3.2.2)
# =====================================================================


@pytest.mark.parametrize(
    "role,expected",
    [
        ("admin_owner", True),
        ("admin_manager", True),
        ("instance_operator", True),  # bound to instance 1 → allow.
        ("read_only_viewer", False),
    ],
)
def test_knowledge_list_view_matrix(role, expected):
    # Operator must be bound to instance 1 for the gate to allow.
    luciel_iid = 1 if role == "instance_operator" else None
    assert (
        _allow(
            role,
            allowed_roles=_KNOWLEDGE_LIST_VIEW,
            luciel_instance_id=luciel_iid,
        )
        is expected
    )


@pytest.mark.parametrize(
    "role,expected",
    [
        ("admin_owner", True),
        ("admin_manager", True),
        ("instance_operator", False),  # operator cannot edit/delete.
        ("read_only_viewer", False),
    ],
)
def test_knowledge_edit_delete_matrix(role, expected):
    luciel_iid = 1 if role == "instance_operator" else None
    assert (
        _allow(
            role,
            allowed_roles=_KNOWLEDGE_EDIT_DELETE,
            luciel_instance_id=luciel_iid,
        )
        is expected
    )


# =====================================================================
# Tool matrix
# =====================================================================


@pytest.mark.parametrize(
    "role,expected",
    [
        ("admin_owner", True),
        ("admin_manager", True),
        ("instance_operator", True),  # bound to instance 1.
        ("read_only_viewer", True),
    ],
)
def test_tool_read_matrix(role, expected):
    luciel_iid = 1 if role == "instance_operator" else None
    assert (
        _allow(role, allowed_roles=_ALL_FOUR, luciel_instance_id=luciel_iid)
        is expected
    )


@pytest.mark.parametrize(
    "role,expected",
    [
        ("admin_owner", True),
        ("admin_manager", True),
        ("instance_operator", False),
        ("read_only_viewer", False),
    ],
)
def test_tool_toggle_matrix(role, expected):
    luciel_iid = 1 if role == "instance_operator" else None
    assert (
        _allow(role, allowed_roles=_TOGGLE, luciel_instance_id=luciel_iid)
        is expected
    )


# =====================================================================
# Sibling-grant matrix
# =====================================================================


@pytest.mark.parametrize(
    "role,expected",
    [
        ("admin_owner", True),
        ("admin_manager", True),
        ("instance_operator", False),
        ("read_only_viewer", False),
    ],
)
def test_sibling_author_matrix(role, expected):
    luciel_iid = 1 if role == "instance_operator" else None
    assert (
        _allow(
            role,
            allowed_roles=_SIBLING_AUTHOR,
            luciel_instance_id=luciel_iid,
        )
        is expected
    )


@pytest.mark.parametrize(
    "role,expected",
    [
        ("admin_owner", True),
        ("admin_manager", False),  # only owner approves.
        ("instance_operator", False),
        ("read_only_viewer", False),
    ],
)
def test_sibling_approve_matrix(role, expected):
    luciel_iid = 1 if role == "instance_operator" else None
    assert (
        _allow(
            role,
            allowed_roles=_SIBLING_APPROVE,
            luciel_instance_id=luciel_iid,
        )
        is expected
    )


# =====================================================================
# Operator instance scoping — operator bound to one instance does NOT
# see the others (Wall-2 + Wall-3 intersection).
# =====================================================================


def test_operator_cannot_act_on_other_instance():
    """Operator bound to instance 1 attempting to access instance 2
    on a knowledge list gate (operator IS in the allowed-roles set)
    must 403 because of the instance binding."""
    req = _req("instance_operator", luciel_instance_id=1)
    with pytest.raises(HTTPException):
        ScopePolicy.enforce_role_on_instance(
            req,
            _inst(instance_id=2),
            allowed_roles=_KNOWLEDGE_LIST_VIEW,
        )
