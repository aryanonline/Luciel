"""
Arc 9 C4.1 regression tests -- instance_id in-app RLS connection-pool
wrapper (Wall 3 / Layer 3).

CONTRACT GUARDED:
    1. The ContextVar in app.db.instance_context isolates instance_id
       across asyncio tasks and threads.
    2. set / get / clear / reset round-trip correctly with integer
       instance ids.
    3. The SQLAlchemy ``after_begin`` listener (now serving BOTH
       Walls in C4.1):
       a. No-ops when settings.rls_tenant_context_enabled is False.
       b. Issues ``set_config('app.instance_id', '<int>', true)``
          on every transaction begin when the flag is True AND an
          instance_id is bound to the ContextVar.
       c. Issues ``set_config('app.instance_id', '', true)`` when
          the flag is True but no instance_id is set (no-context
          path, RLS policies treat empty as NULL-permissive read).
    4. Coexistence: the listener fires the admin_id GUC AND the
       instance_id GUC in the same transaction. Setting one MUST
       NOT affect the other -- they are independent ContextVars
       gated by the same master flag.

WHY UNIT (not DB-backed):
    Same rationale as test_tenant_context.py. The actual SQL effect
    of set_config() on RLS policies is verified in the C4.3
    per-table RLS tests against a real Postgres. Here we assert
    that the right SQL is GENERATED with the right value.

RUN:
    python -m pytest tests/db/test_instance_context.py -v
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from typing import Any
from unittest.mock import MagicMock, patch


# instances.id is Integer. Realistic test fixtures use small ints
# that match the production sequence-assigned PKs (no auto-increment
# gaps in our schema).
_INST_A = 1001
_INST_B = 1002


class TestInstanceContextVar(unittest.TestCase):
    """ContextVar round-trip + isolation across asyncio tasks."""

    def setUp(self):
        # Start from a clean context. Tests run in sequence; without
        # this an earlier test's value would leak.
        from app.db.instance_context import clear_current_instance_id
        clear_current_instance_id()

    def test_default_is_none(self):
        from app.db.instance_context import get_current_instance_id
        self.assertIsNone(get_current_instance_id())

    def test_set_get_round_trip_int(self):
        from app.db.instance_context import (
            get_current_instance_id,
            set_current_instance_id,
        )
        set_current_instance_id(_INST_A)
        self.assertEqual(get_current_instance_id(), _INST_A)

    def test_set_get_round_trip_zero(self):
        """0 is a legal sequence value (theoretically). The
        ContextVar must round-trip it without coercing to None.
        Otherwise an instance with id=0 would be treated as 'no
        instance scope' and read other admin's NULL rows.
        """
        from app.db.instance_context import (
            get_current_instance_id,
            set_current_instance_id,
        )
        set_current_instance_id(0)
        # Must be exactly 0 -- NOT None.
        result = get_current_instance_id()
        self.assertEqual(result, 0)
        self.assertIsNotNone(result)

    def test_clear_resets_to_none(self):
        from app.db.instance_context import (
            clear_current_instance_id,
            get_current_instance_id,
            set_current_instance_id,
        )
        set_current_instance_id(_INST_A)
        clear_current_instance_id()
        self.assertIsNone(get_current_instance_id())

    def test_reset_restores_previous_value(self):
        from app.db.instance_context import (
            get_current_instance_id,
            reset_current_instance_id,
            set_current_instance_id,
        )
        set_current_instance_id(_INST_A)
        token = set_current_instance_id(_INST_B)
        self.assertEqual(get_current_instance_id(), _INST_B)
        reset_current_instance_id(token)
        # After reset, we're back to instance A.
        self.assertEqual(get_current_instance_id(), _INST_A)

    def test_isolation_across_asyncio_tasks(self):
        """The whole point of ContextVar over threading.local.

        Two coroutines started with create_task should see
        independent instance_id values even though they interleave
        on the same event loop. If they leaked into each other,
        that would be a Wall-3 leak vector.
        """
        from app.db.instance_context import (
            get_current_instance_id,
            set_current_instance_id,
        )

        observed: dict[str, Any] = {}

        async def task_a():
            set_current_instance_id(_INST_A)
            await asyncio.sleep(0)
            observed["a"] = get_current_instance_id()

        async def task_b():
            set_current_instance_id(_INST_B)
            await asyncio.sleep(0)
            observed["b"] = get_current_instance_id()

        async def main():
            await asyncio.gather(task_a(), task_b())

        asyncio.run(main())
        self.assertEqual(observed["a"], _INST_A)
        self.assertEqual(observed["b"], _INST_B)


class TestInstanceContextListener(unittest.TestCase):
    """The shared ``after_begin`` listener now emits BOTH GUCs.

    Same approach as test_tenant_context.py: invoke the listener
    directly against a mocked connection, assert on the SQL emitted.
    """

    def _invoke_listener(self, admin_id, instance_id, flag_enabled):
        """Helper: invoke the listener with both contexts set, return
        the list of exec_driver_sql calls.

        We deliberately set BOTH contexts here because the listener
        emits both GUCs and many tests want to assert independence
        of the two values.
        """
        from app.db.tenant_context import (
            clear_current_admin_id,
            set_current_admin_id,
        )
        from app.db.instance_context import (
            clear_current_instance_id,
            set_current_instance_id,
        )

        clear_current_admin_id()
        clear_current_instance_id()
        if admin_id is not None:
            set_current_admin_id(admin_id)
        if instance_id is not None:
            set_current_instance_id(instance_id)

        mock_connection = MagicMock()

        with patch(
            "app.db.session.settings.rls_tenant_context_enabled",
            flag_enabled,
        ):
            from app.db.session import _set_tenant_context_on_begin
            _set_tenant_context_on_begin(
                session=MagicMock(),
                transaction=MagicMock(),
                connection=mock_connection,
            )

        clear_current_admin_id()
        clear_current_instance_id()
        return mock_connection.exec_driver_sql.call_args_list

    def _instance_id_call(self, calls):
        """Return the single call whose SQL is the app.instance_id
        set_config. Fails fast if not exactly one match.
        """
        matches = [
            c for c in calls if "app.instance_id" in c.args[0]
        ]
        self.assertEqual(
            len(matches),
            1,
            f"Expected exactly 1 app.instance_id set_config call, "
            f"got {len(matches)}. All calls: {calls}",
        )
        return matches[0]

    def test_flag_off_is_noop(self):
        """When the master flag is False, NEITHER GUC is set. This
        is the v1-default path -- zero added DB traffic.
        """
        calls = self._invoke_listener(
            admin_id="acme-corp",
            instance_id=_INST_A,
            flag_enabled=False,
        )
        self.assertEqual(
            calls,
            [],
            "C4.1 listener fired set_config with flag off -- "
            "regression that would add latency to every v1 request.",
        )

    def test_flag_on_emits_both_gucs(self):
        """The C4.1 contract: both GUCs set in the same transaction.

        Order is not required (and not asserted) -- RLS policies
        read both via current_setting() and don't care which fired
        first. What matters is that BOTH appear in the call list.
        """
        calls = self._invoke_listener(
            admin_id="acme-corp",
            instance_id=_INST_A,
            flag_enabled=True,
        )
        self.assertEqual(
            len(calls),
            2,
            "C4.1 listener must fire exactly 2 set_config calls "
            "when flag is on (one per GUC).",
        )
        # Both GUCs by name in the emitted SQL.
        sqls = " | ".join(c.args[0] for c in calls)
        self.assertIn("app.admin_id", sqls)
        self.assertIn("app.instance_id", sqls)

    def test_flag_on_with_int_instance_emits_decimal_string(self):
        """instances.id is Integer -- the GUC value MUST be the
        decimal string form. The matching RLS policies (C4.3) cast
        the column with ::text before comparing, so the GUC string
        must be the canonical decimal form to match.
        """
        calls = self._invoke_listener(
            admin_id="acme-corp",
            instance_id=_INST_A,
            flag_enabled=True,
        )
        instance_call = self._instance_id_call(calls)
        sql, params = instance_call.args[0], instance_call.args[1]
        self.assertIn("set_config", sql)
        self.assertIn("app.instance_id", sql)
        # is_local=true semantics (transaction-scoped).
        self.assertIn("true", sql.lower())
        # Value passed as parameter, decimal string form of the int.
        self.assertEqual(params, (str(_INST_A),))

    def test_flag_on_with_zero_instance_emits_zero_string(self):
        """The 0-is-legal contract from the ContextVar tests must
        propagate to the listener: an instance_id of 0 emits the
        string '0', NOT the empty string. Otherwise the RLS predicate
        would treat the request as no-context and incorrectly match
        NULL-tagged rows.
        """
        calls = self._invoke_listener(
            admin_id="acme-corp",
            instance_id=0,
            flag_enabled=True,
        )
        instance_call = self._instance_id_call(calls)
        _, params = instance_call.args[0], instance_call.args[1]
        self.assertEqual(params, ("0",))

    def test_flag_on_with_no_instance_emits_empty_string(self):
        """No-context path (admin-level API key, admin dashboard
        request, background job spanning instances). The GUC clears
        to empty string so any leftover from a previous transaction
        on the same pooled connection is overwritten -- the SET
        LOCAL clear-on-COMMIT covers same-connection reuse, but the
        explicit empty-string write is defence-in-depth.
        """
        calls = self._invoke_listener(
            admin_id="acme-corp",
            instance_id=None,
            flag_enabled=True,
        )
        instance_call = self._instance_id_call(calls)
        sql, params = instance_call.args[0], instance_call.args[1]
        self.assertIn("set_config", sql)
        self.assertEqual(params, ("",))

    def test_instance_set_does_not_affect_admin_value(self):
        """Independence regression. Setting instance_id MUST NOT
        change what the listener emits for admin_id, and vice
        versa. A bug that conflated the two ContextVars would
        produce a tenant-leak vector.
        """
        calls = self._invoke_listener(
            admin_id="globex-industries",
            instance_id=_INST_B,
            flag_enabled=True,
        )
        # Find both calls and assert each carries its own value.
        admin_calls = [
            c for c in calls if "app.admin_id" in c.args[0]
        ]
        instance_calls = [
            c for c in calls if "app.instance_id" in c.args[0]
        ]
        self.assertEqual(len(admin_calls), 1)
        self.assertEqual(len(instance_calls), 1)
        self.assertEqual(admin_calls[0].args[1], ("globex-industries",))
        self.assertEqual(instance_calls[0].args[1], (str(_INST_B),))

    def test_one_context_unset_does_not_block_other(self):
        """A request that has admin_id but no instance_id (admin
        dashboard) MUST still emit both GUCs -- admin_id with the
        slug, instance_id with empty string. Otherwise the
        instance_id GUC could carry a stale value from a previous
        transaction on the same connection.
        """
        calls = self._invoke_listener(
            admin_id="acme-corp",
            instance_id=None,
            flag_enabled=True,
        )
        self.assertEqual(len(calls), 2)
        admin_calls = [
            c for c in calls if "app.admin_id" in c.args[0]
        ]
        instance_calls = [
            c for c in calls if "app.instance_id" in c.args[0]
        ]
        self.assertEqual(admin_calls[0].args[1], ("acme-corp",))
        self.assertEqual(instance_calls[0].args[1], ("",))


if __name__ == "__main__":
    sys.exit(
        0
        if unittest.main(exit=False).result.wasSuccessful()
        else 1
    )
