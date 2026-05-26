"""
Arc 9 C2 regression tests — in-app RLS connection-pool wrapper.

CONTRACT GUARDED:
    1. The ContextVar in app.db.tenant_context isolates admin_id
       across asyncio tasks and threads.
    2. set / get / clear / reset round-trip correctly.
    3. The SQLAlchemy ``after_begin`` listener:
       a. No-ops when settings.rls_tenant_context_enabled is False
          (the v1 default).
       b. Issues ``set_config('app.admin_id', '<uuid>', true)`` on
          every transaction begin when the flag is True and a
          ContextVar value is set.
       c. Issues ``set_config('app.admin_id', '', true)`` when the
          flag is True but no ContextVar value is set (no-context
          path, expected to deny customer-data reads at RLS).
    4. The FastAPI dep ``get_tenant_scoped_db`` reads
       ``request.state.admin_id`` and binds it to the admin_id
       ContextVar, AND reads ``request.state.luciel_instance_id``
       and binds it to the instance_id ContextVar (C4.2). Both are
       cleared on exit, and the resets are independent so a failure
       to reset one MUST NOT prevent the other from being reset.

WHY UNIT (not DB-backed):
    The mechanism under test is pure in-process plumbing:
    ContextVar + SQLAlchemy event hook + FastAPI dependency. The
    actual ``set_config()`` call's SQL effect is verified by the
    Arc 9 C3 RLS-policy tests against a real Postgres. Here we
    assert that the right SQL is GENERATED with the right value
    at the right time. We do that by intercepting the connection's
    ``exec_driver_sql`` call.

RUN:
    python -m pytest tests/db/test_tenant_context.py -v
    OR:
    python tests/db/test_tenant_context.py
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from contextvars import copy_context  # noqa: F401  (kept for future tests)
from typing import Any
from unittest.mock import MagicMock, patch


# Arc 9 C2 hot-fix: admin_id is a STRING SLUG (e.g. "acme-corp"), not
# a UUID. The model column is String(100) on every customer-data
# table. Tests use slug-style fixtures that match real production
# data. We DO include a uuid-like string in one test to confirm the
# system handles both shapes gracefully (Stripe-generated admins
# tend to use UUID-ish slugs, hand-provisioned ones use kebab-case).
_SLUG_A = "acme-corp"
_SLUG_B = "globex-industries"
_SLUG_UUIDLIKE = "a1b2c3d4-5e6f-7a8b-9c0d-1e2f3a4b5c6d"


class TestTenantContextVar(unittest.TestCase):
    """ContextVar round-trip + isolation across asyncio tasks."""

    def setUp(self):
        # Always start from a clean context. Tests run in sequence;
        # without this an earlier test's value would leak.
        from app.db.tenant_context import clear_current_admin_id
        clear_current_admin_id()

    def test_default_is_none(self):
        from app.db.tenant_context import get_current_admin_id
        self.assertIsNone(get_current_admin_id())

    def test_set_get_round_trip_slug(self):
        from app.db.tenant_context import (
            get_current_admin_id,
            set_current_admin_id,
        )
        set_current_admin_id(_SLUG_A)
        self.assertEqual(get_current_admin_id(), _SLUG_A)

    def test_set_get_round_trip_uuidlike_slug(self):
        """Stripe-flow admins sometimes have uuid-shaped slug ids.
        Confirm the plumbing handles both."""
        from app.db.tenant_context import (
            get_current_admin_id,
            set_current_admin_id,
        )
        set_current_admin_id(_SLUG_UUIDLIKE)
        self.assertEqual(get_current_admin_id(), _SLUG_UUIDLIKE)

    def test_set_get_round_trip_platform_sentinel(self):
        """admin_audit_logs uses 'platform' literal for system actions.
        ContextVar must accept it without coercion."""
        from app.db.tenant_context import (
            get_current_admin_id,
            set_current_admin_id,
        )
        set_current_admin_id("platform")
        self.assertEqual(get_current_admin_id(), "platform")

    def test_clear_resets_to_none(self):
        from app.db.tenant_context import (
            clear_current_admin_id,
            get_current_admin_id,
            set_current_admin_id,
        )
        set_current_admin_id(_SLUG_A)
        clear_current_admin_id()
        self.assertIsNone(get_current_admin_id())

    def test_reset_restores_previous_value(self):
        from app.db.tenant_context import (
            get_current_admin_id,
            reset_current_admin_id,
            set_current_admin_id,
        )
        set_current_admin_id(_SLUG_A)
        token = set_current_admin_id(_SLUG_B)
        self.assertEqual(get_current_admin_id(), _SLUG_B)
        reset_current_admin_id(token)
        # After reset, we're back to slug_a.
        self.assertEqual(get_current_admin_id(), _SLUG_A)

    def test_isolation_across_asyncio_tasks(self):
        """The whole point of ContextVar over threading.local.

        Two coroutines started with create_task should see
        independent admin_id values even though they interleave on
        the same event loop. If they leaked into each other, that
        would be a tenant-leak vector.
        """
        from app.db.tenant_context import (
            get_current_admin_id,
            set_current_admin_id,
        )

        admin_a = _SLUG_A
        admin_b = _SLUG_B
        observed: dict[str, Any] = {}

        async def task_a():
            set_current_admin_id(admin_a)
            # Yield to the loop so task_b runs in between.
            await asyncio.sleep(0)
            observed["a"] = get_current_admin_id()

        async def task_b():
            set_current_admin_id(admin_b)
            await asyncio.sleep(0)
            observed["b"] = get_current_admin_id()

        async def main():
            await asyncio.gather(task_a(), task_b())

        asyncio.run(main())
        self.assertEqual(observed["a"], admin_a)
        self.assertEqual(observed["b"], admin_b)


class TestAfterBeginListener(unittest.TestCase):
    """The SQLAlchemy ``after_begin`` listener that issues SET LOCAL.

    We don't spin up a real Postgres here. Instead we synthesise the
    arguments the SQLAlchemy event system would pass and assert on
    the SQL emitted to the connection. The actual end-to-end SET +
    RLS-policy verification belongs in the Arc 9 C3 integration
    suite against a real DB.
    """

    def _invoke_listener(self, admin_id, flag_enabled):
        """Helper: import the listener directly, call it with a
        mocked connection, return what SQL it tried to execute.
        """
        from app.db.tenant_context import (
            clear_current_admin_id,
            set_current_admin_id,
        )

        clear_current_admin_id()
        if admin_id is not None:
            set_current_admin_id(admin_id)

        # Mock the connection's exec_driver_sql so we can observe it.
        mock_connection = MagicMock()

        # We have to monkey-patch settings.rls_tenant_context_enabled
        # BEFORE the listener checks it.
        with patch(
            "app.db.session.settings.rls_tenant_context_enabled",
            flag_enabled,
        ):
            # Re-import the listener function (it's a module-level
            # event handler so it's exposed under its name).
            from app.db.session import _set_tenant_context_on_begin
            # Signature: (session, transaction, connection)
            _set_tenant_context_on_begin(
                session=MagicMock(),
                transaction=MagicMock(),
                connection=mock_connection,
            )

        clear_current_admin_id()
        return mock_connection.exec_driver_sql.call_args_list

    def test_flag_off_is_noop(self):
        """When rls_tenant_context_enabled is False, the listener
        MUST NOT touch the DB connection. Zero traffic added."""
        calls = self._invoke_listener(_SLUG_A, flag_enabled=False)
        self.assertEqual(
            calls,
            [],
            "Listener fired set_config() with flag off -- this is a "
            "regression that would add latency to every v1 request.",
        )

    # NOTE on call count (Arc 9 C4.1 update):
    #
    # Before C4.1 the listener issued exactly ONE set_config() call
    # per BEGIN (for app.admin_id). C4.1 added a second set_config()
    # call alongside it for app.instance_id. Tests below now expect
    # 2 calls when the flag is on. The admin_id call is verified by
    # filtering the call list to the call whose SQL mentions
    # 'app.admin_id' -- the instance_id call is covered separately
    # in test_instance_context.py.

    def _admin_id_call(self, calls):
        """Return the single call_args whose SQL is the app.admin_id
        set_config. Fails fast if not exactly one match."""
        matches = [
            c for c in calls if "app.admin_id" in c.args[0]
        ]
        self.assertEqual(
            len(matches),
            1,
            f"Expected exactly 1 app.admin_id set_config call, "
            f"got {len(matches)}. All calls: {calls}",
        )
        return matches[0]

    def test_flag_on_with_slug_emits_slug_string(self):
        calls = self._invoke_listener(_SLUG_A, flag_enabled=True)
        # C4.1: now expect 2 set_config calls (admin_id + instance_id).
        self.assertEqual(len(calls), 2)
        admin_call = self._admin_id_call(calls)
        sql, params = admin_call.args[0], admin_call.args[1]
        self.assertIn("set_config", sql)
        self.assertIn("app.admin_id", sql)
        # is_local=true (third positional in set_config) means SET LOCAL
        # semantics -- the GUC clears at transaction end.
        self.assertIn("true", sql.lower())
        # Value passed as a parameter, not interpolated into SQL.
        self.assertEqual(params, (_SLUG_A,))

    def test_flag_on_with_platform_sentinel_passes_through(self):
        """admin_audit_logs writes admin_id='platform' for system
        actions. The listener MUST pass the literal through unchanged
        so platform-tier RLS policies can match it."""
        calls = self._invoke_listener("platform", flag_enabled=True)
        self.assertEqual(len(calls), 2)  # C4.1: admin_id + instance_id
        admin_call = self._admin_id_call(calls)
        _, params = admin_call.args[0], admin_call.args[1]
        self.assertEqual(params, ("platform",))

    def test_flag_on_with_no_admin_id_emits_empty_string(self):
        """No-context path (background job, health check). We MUST
        still issue the SET so any leftover GUC from a previous
        transaction on this connection is cleared."""
        calls = self._invoke_listener(admin_id=None, flag_enabled=True)
        self.assertEqual(len(calls), 2)  # C4.1: admin_id + instance_id
        admin_call = self._admin_id_call(calls)
        sql, params = admin_call.args[0], admin_call.args[1]
        self.assertIn("set_config", sql)
        self.assertEqual(params, ("",))


class TestFastAPIDependency(unittest.TestCase):
    """``get_tenant_scoped_db`` reads request.state and binds the
    ContextVar; clears on exit.
    """

    def test_dep_binds_tenant_id_from_request_state(self):
        from app.db.tenant_context import get_current_admin_id
        from app.db.instance_context import get_current_instance_id

        admin_id = _SLUG_A
        instance_id = 42
        # Synthetic request whose state has BOTH admin_id and
        # luciel_instance_id (the normal authenticated path).
        mock_request = MagicMock()
        mock_request.state.admin_id = admin_id
        mock_request.state.luciel_instance_id = instance_id

        # Stub SessionLocal so we don't actually open a DB connection.
        with patch("app.api.deps.SessionLocal") as mock_session_local:
            mock_session_local.return_value = MagicMock()
            from app.api.deps import get_tenant_scoped_db
            gen = get_tenant_scoped_db(mock_request)
            db = next(gen)
            self.assertIsNotNone(db)
            # Inside the dep scope, BOTH ContextVars MUST hold the
            # request-state values (C4.2 dual binding).
            self.assertEqual(get_current_admin_id(), admin_id)
            self.assertEqual(get_current_instance_id(), instance_id)
            # Exit the generator (mimic FastAPI finishing the request).
            try:
                next(gen)
            except StopIteration:
                pass

        # After exit, BOTH ContextVars MUST be cleared (or restored
        # to pre-dep value -- which was None for this test).
        self.assertIsNone(get_current_admin_id())
        self.assertIsNone(get_current_instance_id())

    def test_dep_missing_tenant_id_binds_none(self):
        """Health check / unauth path -- request.state has no
        admin_id NOR luciel_instance_id. The dep MUST bind both to
        None, not raise."""
        from app.db.tenant_context import get_current_admin_id
        from app.db.instance_context import get_current_instance_id

        mock_request = MagicMock()
        # spec=set([]) trick: any attribute access on .state that
        # ISN'T deleted would auto-create a MagicMock; we want both
        # admin_id and luciel_instance_id missing entirely so
        # getattr returns None for both.
        del mock_request.state.admin_id
        del mock_request.state.luciel_instance_id

        with patch("app.api.deps.SessionLocal") as mock_session_local:
            mock_session_local.return_value = MagicMock()
            from app.api.deps import get_tenant_scoped_db
            gen = get_tenant_scoped_db(mock_request)
            next(gen)
            self.assertIsNone(get_current_admin_id())
            self.assertIsNone(get_current_instance_id())
            try:
                next(gen)
            except StopIteration:
                pass

        # Both still None after exit.
        self.assertIsNone(get_current_admin_id())
        self.assertIsNone(get_current_instance_id())

    def test_dep_binds_instance_id_zero(self):
        """C4.2 regression canary: instance_id=0 is a LEGAL Integer
        primary key in Postgres. The dep MUST bind 0, NOT coerce
        it to None (which would degrade to NULL-permissive cross-
        tenant read at the RLS layer -- a leak).
        """
        from app.db.tenant_context import get_current_admin_id
        from app.db.instance_context import get_current_instance_id

        mock_request = MagicMock()
        mock_request.state.admin_id = _SLUG_A
        mock_request.state.luciel_instance_id = 0

        with patch("app.api.deps.SessionLocal") as mock_session_local:
            mock_session_local.return_value = MagicMock()
            from app.api.deps import get_tenant_scoped_db
            gen = get_tenant_scoped_db(mock_request)
            next(gen)
            # CRITICAL: must be 0, not None. Use assertEqual with an
            # explicit type check -- `assertEqual(0, None)` would
            # also pass under a buggy truthiness coercion.
            value = get_current_instance_id()
            self.assertEqual(value, 0)
            self.assertIsNotNone(value)
            self.assertIsInstance(value, int)
            try:
                next(gen)
            except StopIteration:
                pass

        self.assertIsNone(get_current_instance_id())

    def test_dep_admin_level_key_binds_admin_only(self):
        """Admin-level API key path: admin_id is set but no instance
        is bound. The dep MUST bind admin_id and leave instance_id as
        None. RLS Wall 3 policies are NULL-permissive (per C4.3
        doctrine) so an admin-level key can still read rows where
        luciel_instance_id IS NULL, while instance-scoped rows are
        invisible.
        """
        from app.db.tenant_context import get_current_admin_id
        from app.db.instance_context import get_current_instance_id

        mock_request = MagicMock()
        mock_request.state.admin_id = _SLUG_A
        del mock_request.state.luciel_instance_id

        with patch("app.api.deps.SessionLocal") as mock_session_local:
            mock_session_local.return_value = MagicMock()
            from app.api.deps import get_tenant_scoped_db
            gen = get_tenant_scoped_db(mock_request)
            next(gen)
            self.assertEqual(get_current_admin_id(), _SLUG_A)
            self.assertIsNone(get_current_instance_id())
            try:
                next(gen)
            except StopIteration:
                pass

        self.assertIsNone(get_current_admin_id())
        self.assertIsNone(get_current_instance_id())

    def test_dep_resets_instance_id_even_if_admin_reset_raises(self):
        """C4.2 independent-reset guarantee: if the admin_id reset
        raises (corrupt token, ContextVar machinery glitch), the
        instance_id reset MUST STILL run. Otherwise an exception
        during cleanup of one wall would leave the other wall's
        value lingering on the worker coroutine -- a leak window.

        We assert by patching ``reset_current_admin_id`` to raise,
        then verifying instance_id is still cleared after the dep
        exits.
        """
        from app.db.instance_context import get_current_instance_id

        mock_request = MagicMock()
        mock_request.state.admin_id = _SLUG_A
        mock_request.state.luciel_instance_id = 7

        with patch("app.api.deps.SessionLocal") as mock_session_local, \
             patch(
                 "app.api.deps.reset_current_admin_id",
                 side_effect=RuntimeError("simulated token corruption"),
             ):
            mock_session_local.return_value = MagicMock()
            from app.api.deps import get_tenant_scoped_db
            gen = get_tenant_scoped_db(mock_request)
            next(gen)
            self.assertEqual(get_current_instance_id(), 7)
            # Exhaust the generator. The dep's finally block catches
            # the simulated reset failure via clear_current_admin_id
            # and must STILL proceed to reset instance_id.
            try:
                next(gen)
            except StopIteration:
                pass

        # Even though admin reset blew up, instance_id MUST be clear.
        self.assertIsNone(get_current_instance_id())


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
