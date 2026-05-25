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
       ``request.state.tenant_id`` and binds it to the ContextVar,
       and clears it on exit.

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
import uuid
from contextvars import copy_context
from typing import Any
from unittest.mock import MagicMock, patch


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

    def test_set_get_round_trip(self):
        from app.db.tenant_context import (
            get_current_admin_id,
            set_current_admin_id,
        )
        admin_id = uuid.uuid4()
        set_current_admin_id(admin_id)
        self.assertEqual(get_current_admin_id(), admin_id)

    def test_clear_resets_to_none(self):
        from app.db.tenant_context import (
            clear_current_admin_id,
            get_current_admin_id,
            set_current_admin_id,
        )
        set_current_admin_id(uuid.uuid4())
        clear_current_admin_id()
        self.assertIsNone(get_current_admin_id())

    def test_reset_restores_previous_value(self):
        from app.db.tenant_context import (
            get_current_admin_id,
            reset_current_admin_id,
            set_current_admin_id,
        )
        first = uuid.uuid4()
        second = uuid.uuid4()
        set_current_admin_id(first)
        token = set_current_admin_id(second)
        self.assertEqual(get_current_admin_id(), second)
        reset_current_admin_id(token)
        # After reset, we're back to ``first``.
        self.assertEqual(get_current_admin_id(), first)

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

        admin_a = uuid.uuid4()
        admin_b = uuid.uuid4()
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
        admin_id = uuid.uuid4()
        calls = self._invoke_listener(admin_id, flag_enabled=False)
        self.assertEqual(
            calls,
            [],
            "Listener fired set_config() with flag off -- this is a "
            "regression that would add latency to every v1 request.",
        )

    def test_flag_on_with_admin_id_emits_uuid_string(self):
        admin_id = uuid.uuid4()
        calls = self._invoke_listener(admin_id, flag_enabled=True)
        self.assertEqual(len(calls), 1)
        sql, params = calls[0].args[0], calls[0].args[1]
        self.assertIn("set_config", sql)
        self.assertIn("app.admin_id", sql)
        # is_local=true (third positional in set_config) means SET LOCAL
        # semantics -- the GUC clears at transaction end.
        self.assertIn("true", sql.lower())
        # Value passed as a parameter, not interpolated into SQL.
        self.assertEqual(params, (str(admin_id),))

    def test_flag_on_with_no_admin_id_emits_empty_string(self):
        """No-context path (background job, health check). We MUST
        still issue the SET so any leftover GUC from a previous
        transaction on this connection is cleared."""
        calls = self._invoke_listener(admin_id=None, flag_enabled=True)
        self.assertEqual(len(calls), 1)
        sql, params = calls[0].args[0], calls[0].args[1]
        self.assertIn("set_config", sql)
        self.assertEqual(params, ("",))


class TestFastAPIDependency(unittest.TestCase):
    """``get_tenant_scoped_db`` reads request.state and binds the
    ContextVar; clears on exit.
    """

    def test_dep_binds_tenant_id_from_request_state(self):
        from app.db.tenant_context import get_current_admin_id

        admin_id = uuid.uuid4()
        # Synthetic request whose state has tenant_id.
        mock_request = MagicMock()
        mock_request.state.tenant_id = admin_id

        # Stub SessionLocal so we don't actually open a DB connection.
        with patch("app.api.deps.SessionLocal") as mock_session_local:
            mock_session_local.return_value = MagicMock()
            from app.api.deps import get_tenant_scoped_db
            gen = get_tenant_scoped_db(mock_request)
            db = next(gen)
            self.assertIsNotNone(db)
            # Inside the dep scope, ContextVar MUST hold the admin_id.
            self.assertEqual(get_current_admin_id(), admin_id)
            # Exit the generator (mimic FastAPI finishing the request).
            try:
                next(gen)
            except StopIteration:
                pass

        # After exit, ContextVar MUST be cleared (or restored to its
        # pre-dep value -- which was None for this test).
        self.assertIsNone(get_current_admin_id())

    def test_dep_missing_tenant_id_binds_none(self):
        """Health check / unauth path -- request.state has no
        tenant_id. The dep MUST bind None, not raise."""
        from app.db.tenant_context import get_current_admin_id

        mock_request = MagicMock()
        # spec=set([]) trick: any attribute access on .state that
        # ISN'T tenant_id would auto-create a MagicMock; we want
        # tenant_id to be missing entirely so getattr returns None.
        del mock_request.state.tenant_id

        with patch("app.api.deps.SessionLocal") as mock_session_local:
            mock_session_local.return_value = MagicMock()
            from app.api.deps import get_tenant_scoped_db
            gen = get_tenant_scoped_db(mock_request)
            next(gen)
            self.assertIsNone(get_current_admin_id())
            try:
                next(gen)
            except StopIteration:
                pass


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
