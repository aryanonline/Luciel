"""
Arc 9 C4.4 regression tests -- bind_tenant_scope helper for non-HTTP
callers (Celery workers, scheduled jobs, CLI tools).

CONTRACT GUARDED:
  1. Inside the with-block, BOTH ContextVars hold the bound values.
  2. After the with-block, BOTH ContextVars are restored to their
     pre-bind values (None in fresh tests).
  3. None bindings produce empty/None ContextVar values, matching the
     listener's empty-GUC posture (fail-closed at Wall 1).
  4. instance_id=0 is bound as 0, NOT coerced to None (canary against
     truthiness coercion of legal Integer PK).
  5. Reset is INDEPENDENT: if one ContextVar's reset raises, the
     other MUST still reset.
  6. Nested bindings restore correctly via Token semantics.

RUN:
    python -m pytest tests/db/test_tenant_scope.py -v
"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import patch


class TestBindTenantScope(unittest.TestCase):
    def test_binds_both_contextvars_in_with_block(self):
        from app.db.tenant_scope import bind_tenant_scope
        from app.db.tenant_context import get_current_admin_id
        from app.db.instance_context import get_current_instance_id

        with bind_tenant_scope(admin_id="acme", instance_id=42):
            self.assertEqual(get_current_admin_id(), "acme")
            self.assertEqual(get_current_instance_id(), 42)

    def test_resets_both_contextvars_after_with_block(self):
        from app.db.tenant_scope import bind_tenant_scope
        from app.db.tenant_context import get_current_admin_id
        from app.db.instance_context import get_current_instance_id

        with bind_tenant_scope(admin_id="acme", instance_id=42):
            pass

        self.assertIsNone(get_current_admin_id())
        self.assertIsNone(get_current_instance_id())

    def test_binds_none_admin_and_none_instance(self):
        """Health-check / unbound paths -- both None is legal and
        produces empty GUCs at the listener (fail-closed)."""
        from app.db.tenant_scope import bind_tenant_scope
        from app.db.tenant_context import get_current_admin_id
        from app.db.instance_context import get_current_instance_id

        with bind_tenant_scope(admin_id=None, instance_id=None):
            self.assertIsNone(get_current_admin_id())
            self.assertIsNone(get_current_instance_id())

    def test_binds_instance_id_zero_is_not_coerced(self):
        """C4.4 canary: 0 is a legal Integer PK and MUST be bound
        as 0, not coerced to None."""
        from app.db.tenant_scope import bind_tenant_scope
        from app.db.instance_context import get_current_instance_id

        with bind_tenant_scope(admin_id="acme", instance_id=0):
            value = get_current_instance_id()
            self.assertEqual(value, 0)
            self.assertIsNotNone(value)
            self.assertIsInstance(value, int)

    def test_binds_admin_id_with_no_instance(self):
        """Admin-level Celery task path: tenant scope bound but no
        instance -- the Wall-3 policy admits NULL-instance rows."""
        from app.db.tenant_scope import bind_tenant_scope
        from app.db.tenant_context import get_current_admin_id
        from app.db.instance_context import get_current_instance_id

        with bind_tenant_scope(admin_id="acme", instance_id=None):
            self.assertEqual(get_current_admin_id(), "acme")
            self.assertIsNone(get_current_instance_id())

    def test_nested_bindings_restore_correctly(self):
        """Token-based reset must restore the OUTER scope when the
        INNER with-block exits. This guards against the dangerous
        case where a side session opened inside a parent-scoped
        block accidentally widens scope on exit."""
        from app.db.tenant_scope import bind_tenant_scope
        from app.db.tenant_context import get_current_admin_id
        from app.db.instance_context import get_current_instance_id

        with bind_tenant_scope(admin_id="outer", instance_id=1):
            self.assertEqual(get_current_admin_id(), "outer")
            self.assertEqual(get_current_instance_id(), 1)
            with bind_tenant_scope(admin_id="inner", instance_id=2):
                self.assertEqual(get_current_admin_id(), "inner")
                self.assertEqual(get_current_instance_id(), 2)
            # Inner exited; outer scope MUST be restored.
            self.assertEqual(get_current_admin_id(), "outer")
            self.assertEqual(get_current_instance_id(), 1)

        # Both exited; back to None.
        self.assertIsNone(get_current_admin_id())
        self.assertIsNone(get_current_instance_id())

    def test_resets_instance_id_even_if_admin_reset_raises(self):
        """Independent-reset guarantee. If the admin-side reset
        raises, the instance-side reset MUST still run, otherwise
        a single faulty reset path would create a leak window on
        the worker coroutine."""
        from app.db.instance_context import get_current_instance_id

        with patch(
            "app.db.tenant_scope.reset_current_admin_id",
            side_effect=RuntimeError("simulated token corruption"),
        ):
            from app.db.tenant_scope import bind_tenant_scope
            with bind_tenant_scope(admin_id="acme", instance_id=7):
                self.assertEqual(get_current_instance_id(), 7)

        # Even though admin reset blew up, instance MUST be reset.
        self.assertIsNone(get_current_instance_id())

    def test_resets_admin_id_even_if_instance_reset_raises(self):
        """Symmetric independent-reset guarantee."""
        from app.db.tenant_context import get_current_admin_id

        with patch(
            "app.db.tenant_scope.reset_current_instance_id",
            side_effect=RuntimeError("simulated token corruption"),
        ):
            from app.db.tenant_scope import bind_tenant_scope
            with bind_tenant_scope(admin_id="acme", instance_id=7):
                self.assertEqual(get_current_admin_id(), "acme")

        self.assertIsNone(get_current_admin_id())

    def test_kwargs_are_required(self):
        """Both admin_id and instance_id MUST be required kwargs to
        force every caller to declare intent for both walls.
        Positional or missing -> TypeError."""
        from app.db.tenant_scope import bind_tenant_scope

        with self.assertRaises(TypeError):
            # Positional args forbidden (kwarg-only by *).
            bind_tenant_scope("acme", 42)  # type: ignore[misc]

        with self.assertRaises(TypeError):
            # Missing instance_id.
            bind_tenant_scope(admin_id="acme")  # type: ignore[call-arg]

        with self.assertRaises(TypeError):
            # Missing admin_id.
            bind_tenant_scope(instance_id=42)  # type: ignore[call-arg]


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
