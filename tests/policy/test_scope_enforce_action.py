"""Step 29.y gap-fix C3: ScopePolicy.enforce_action invariant.

Drift token: D-scope-policy-action-class-gap-2026-05-07.

Origin:
  ScopePolicy had enforce_tenant_scope / enforce_domain_scope /
  enforce_agent_scope / enforce_no_privilege_escalation /
  enforce_luciel_creation_scope / enforce_luciel_instance_scope.
  None of them are an action-class permission check. Action-class
  enforcement lived implicitly inside middleware and ad-hoc per-
  route asserts. A new route added outside middleware (internal
  worker entry, queued task with a request-like object) had no
  named primitive to call.

This module asserts:

  1. ScopePolicy.enforce_action exists and is a classmethod.
  2. Required permission is honoured: caller with the perm passes,
     caller without it gets HTTP 403.
  3. platform_admin satisfies any required_permission (precedent
     parity with enforce_tenant_scope).
  4. Empty / malformed inputs raise ValueError, not HTTPException.
  5. Forbidden characters in required_permission are rejected.
  6. AST assertion: explicit `in perms` membership tests, not
     truthiness short-circuits.
"""
from __future__ import annotations

import inspect

import pytest
from fastapi import HTTPException

from app.policy.scope import ScopePolicy, PLATFORM_ADMIN


class _State:
    def __init__(self, tenant_id=None, domain_id=None, agent_id=None, permissions=None):
        self.tenant_id = tenant_id
        self.domain_id = domain_id
        self.agent_id = agent_id
        self.permissions = permissions or []


class _Req:
    def __init__(self, state):
        self.state = state


def _req(perms):
    return _Req(_State(tenant_id="t1", permissions=list(perms)))


# 1. Method existence and shape

def test_enforce_action_exists_as_classmethod():
    assert hasattr(ScopePolicy, "enforce_action")
    raw = inspect.getattr_static(ScopePolicy, "enforce_action")
    assert isinstance(raw, classmethod)


def test_enforce_action_signature():
    sig = inspect.signature(ScopePolicy.enforce_action)
    params = list(sig.parameters.values())
    assert params[0].name == "request"
    kw = {p.name: p for p in params[1:]}
    assert "required_permission" in kw
    assert "action_label" in kw
    for k in ("required_permission", "action_label"):
        assert kw[k].kind == inspect.Parameter.KEYWORD_ONLY


# 2. Behaviour: pass / deny

def test_caller_with_required_permission_passes():
    ScopePolicy.enforce_action(
        _req(["worker"]),
        required_permission="worker",
        action_label="memory.extract",
    )


def test_caller_without_required_permission_403():
    with pytest.raises(HTTPException) as exc:
        ScopePolicy.enforce_action(
            _req(["chat"]),
            required_permission="worker",
            action_label="memory.extract",
        )
    assert exc.value.status_code == 403
    assert "worker" in str(exc.value.detail)
    assert "memory.extract" in str(exc.value.detail)


def test_caller_with_no_permissions_403():
    with pytest.raises(HTTPException) as exc:
        ScopePolicy.enforce_action(
            _req([]),
            required_permission="worker",
            action_label="memory.extract",
        )
    assert exc.value.status_code == 403


# 3. platform_admin satisfies any required_permission

def test_platform_admin_satisfies_any_required_permission():
    ScopePolicy.enforce_action(
        _req([PLATFORM_ADMIN]),
        required_permission="worker",
        action_label="memory.extract",
    )
    ScopePolicy.enforce_action(
        _req([PLATFORM_ADMIN]),
        required_permission="future_perm",
        action_label="future.action",
    )


def test_platform_admin_with_extra_perms_passes():
    ScopePolicy.enforce_action(
        _req([PLATFORM_ADMIN, "chat"]),
        required_permission="worker",
        action_label="memory.extract",
    )


# 4. Programmer-error inputs raise ValueError (or TypeError for None)

@pytest.mark.parametrize("bad", ["", "   ", None, 123])
def test_required_permission_must_be_non_empty_str(bad):
    with pytest.raises((ValueError, TypeError)):
        ScopePolicy.enforce_action(
            _req([PLATFORM_ADMIN]),
            required_permission=bad,
            action_label="x",
        )


@pytest.mark.parametrize("bad", ["", "   ", None, 123])
def test_action_label_must_be_non_empty_str(bad):
    with pytest.raises((ValueError, TypeError)):
        ScopePolicy.enforce_action(
            _req([PLATFORM_ADMIN]),
            required_permission="worker",
            action_label=bad,
        )


# 5. Forbidden characters in required_permission

@pytest.mark.parametrize(
    "bad",
    ["worker,admin", 'worker"x', "worker\\x", "worker\nx", "worker\tx"],
)
def test_forbidden_chars_in_required_permission(bad):
    with pytest.raises(ValueError):
        ScopePolicy.enforce_action(
            _req([PLATFORM_ADMIN]),
            required_permission=bad,
            action_label="x",
        )


# 6. AST: explicit membership tests, not truthiness short-circuits

def test_enforce_action_uses_explicit_membership_tests():
    """Code-shape pin against a bare truthiness short-circuit
    refactor (e.g. `if perms:`) that would silently let any caller
    with any permission satisfy any check.
    """
    src = inspect.getsource(ScopePolicy.enforce_action)
    assert "PLATFORM_ADMIN in perms" in src, (
        "enforce_action must explicitly check `PLATFORM_ADMIN in perms`."
    )
    assert "required_permission in perms" in src, (
        "enforce_action must explicitly check "
        "`required_permission in perms`."
    )
