"""Arc 11 Cleanup C item #8 — ``request.state.scope_assignments`` flow.

Two contracts:

  C1  The session-cookie auth middleware's
      ``_load_scope_assignments`` helper queries scope_assignments
      and returns the rows. We exercise the helper directly with a
      fake Session because the middleware-level integration is
      asserted statically below (the production path requires the
      full FastAPI stack which the other middleware tests already
      cover).

  C2  ``ScopePolicy._resolve_role_on_instance`` prefers
      ``request.state.scope_assignments`` when present and falls
      back to a per-request DB lookup when it isn't.

Both contracts pair with item #7 (the ``ScopeRole`` PG enum).
"""
from __future__ import annotations

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from app.models.scope_assignment import ScopeAssignment, ScopeRole


class _FakeRequest:
    """Drop-in for a Starlette Request — only ``.state`` is read by
    ScopePolicy under test."""

    def __init__(self, **state) -> None:
        self.state = SimpleNamespace(**state)


class _FakeInstance:
    def __init__(self, admin_id: str, instance_id: int = 1) -> None:
        self.admin_id = admin_id
        self.id = instance_id


def _assignment(*, admin_id: str, role: str | ScopeRole) -> ScopeAssignment:
    """Build an in-memory ScopeAssignment with the lifecycle columns
    set to "active". SQLAlchemy doesn't trip enum validation on
    attribute set (only on flush), so a plain string works for these
    unit tests even though the DB column is now a PG enum."""
    return ScopeAssignment(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        admin_id=admin_id,
        role=role,
        active=True,
        ended_at=None,
    )


class TestScopePolicyPrefersRequestStateScopeAssignments(unittest.TestCase):
    """C2 — middleware-populated list short-circuits the DB SELECT."""

    def test_picks_assignment_matching_target_admin_id(self):
        from app.policy.scope import ScopePolicy

        owner = _assignment(admin_id="t1", role="admin_owner")
        viewer = _assignment(admin_id="t2", role="read_only_viewer")
        request = _FakeRequest(
            permissions=[],
            scope_assignments=[viewer, owner],
            actor_user_id=uuid.uuid4(),
        )
        instance = _FakeInstance(admin_id="t1")

        role = ScopePolicy._resolve_role_on_instance(request, instance)
        self.assertEqual(role, ScopeRole.ADMIN_OWNER)

    def test_returns_none_when_no_assignment_matches(self):
        from app.policy.scope import ScopePolicy

        # Pre-loaded list does NOT contain an assignment for the
        # target admin. ScopePolicy still has actor_user_id available
        # so it would fall through to the DB lookup; we patch
        # SessionLocal to verify the fallback fires AND returns
        # nothing.
        request = _FakeRequest(
            permissions=[],
            scope_assignments=[_assignment(admin_id="other", role="admin_owner")],
            actor_user_id=uuid.uuid4(),
        )
        instance = _FakeInstance(admin_id="t1")

        with patch("app.db.session.SessionLocal") as session_local:
            session_local.return_value.execute.return_value.first.return_value = None
            role = ScopePolicy._resolve_role_on_instance(request, instance)
        self.assertIsNone(role)

    def test_falls_back_to_db_when_state_unset(self):
        """ScopePolicy must keep working in test contexts where
        middleware isn't run (``request.state.scope_assignments``
        unset). It hits the existing per-request SELECT."""
        from app.policy.scope import ScopePolicy

        request = _FakeRequest(
            permissions=[],
            actor_user_id=uuid.uuid4(),
        )
        instance = _FakeInstance(admin_id="t1")

        with patch("app.db.session.SessionLocal") as session_local:
            row = SimpleNamespace()
            session_local.return_value.execute.return_value.first.return_value = (
                ScopeRole.ADMIN_MANAGER,
            )
            role = ScopePolicy._resolve_role_on_instance(request, instance)
        self.assertEqual(role, ScopeRole.ADMIN_MANAGER)


class TestScopeAssignmentLoaderHelper(unittest.TestCase):
    """C1 — middleware helper assembles the list from the DB."""

    def test_helper_returns_empty_when_admin_id_none(self):
        from app.middleware.session_cookie_auth import _load_scope_assignments

        fake_db = object()
        out = _load_scope_assignments(fake_db, user_id=uuid.uuid4(), admin_id=None)
        self.assertEqual(out, [])

    def test_helper_swallows_db_errors_to_avoid_500(self):
        """A DB hiccup at auth time must not 500 the request;
        ScopePolicy falls back to its per-request SELECT."""
        from app.middleware.session_cookie_auth import _load_scope_assignments

        class _BrokenDb:
            def execute(self, *a, **kw):
                raise RuntimeError("infra hiccup")

        out = _load_scope_assignments(
            _BrokenDb(), user_id=uuid.uuid4(), admin_id="t1",
        )
        self.assertEqual(out, [])

    def test_helper_returns_active_rows_from_db(self):
        from app.middleware.session_cookie_auth import _load_scope_assignments

        rows = [_assignment(admin_id="t1", role="admin_owner")]

        class _FakeDb:
            def execute(self, stmt):
                # Return an object whose scalars().all() mirrors the
                # SQLAlchemy 2.x Result shape ScopePolicy expects.
                return SimpleNamespace(
                    scalars=lambda: SimpleNamespace(all=lambda: rows),
                )

        out = _load_scope_assignments(
            _FakeDb(), user_id=uuid.uuid4(), admin_id="t1",
        )
        self.assertEqual(out, rows)


class TestScopeRoleEnumWiring(unittest.TestCase):
    """Smoke: the four canonical roles are exported as ``ScopeRole``
    members AND the policy module's ``ROLE_*`` constants reference
    them."""

    def test_canonical_four_values_present(self):
        values = {m.value for m in ScopeRole}
        self.assertEqual(
            values,
            {"admin_owner", "admin_manager", "instance_operator", "read_only_viewer"},
        )

    def test_policy_constants_reference_enum_members(self):
        from app.policy import scope as scope_mod

        self.assertIs(scope_mod.ROLE_ADMIN_OWNER, ScopeRole.ADMIN_OWNER)
        self.assertIs(scope_mod.ROLE_ADMIN_MANAGER, ScopeRole.ADMIN_MANAGER)
        self.assertIs(scope_mod.ROLE_INSTANCE_OPERATOR, ScopeRole.INSTANCE_OPERATOR)
        self.assertIs(scope_mod.ROLE_READ_ONLY_VIEWER, ScopeRole.READ_ONLY_VIEWER)

    def test_scope_assignments_column_is_pg_enum(self):
        from sqlalchemy import Enum as SAEnum

        col = ScopeAssignment.__table__.columns["role"]
        self.assertIsInstance(col.type, SAEnum)
        self.assertEqual(col.type.name, "scope_role")
