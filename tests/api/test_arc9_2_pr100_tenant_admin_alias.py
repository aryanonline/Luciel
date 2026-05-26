"""Arc 9.2 PR #100 -- input alias removed; output alias retained.

Verifies that ``SessionCreate`` no longer auto-copies ``admin_id`` <->
``tenant_id`` (callers have fully migrated to ``admin_id``), while
``SessionRead`` still emits BOTH keys via ``TenantAdminOutputAlias``.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.schemas.session import SessionCreate, SessionRead


class TestSessionCreateNoInputAlias(unittest.TestCase):
    def test_admin_id_alone_does_NOT_populate_tenant_id(self) -> None:
        s = SessionCreate.model_validate({"admin_id": "acme"})
        self.assertEqual(s.admin_id, "acme")
        self.assertIsNone(s.tenant_id)

    def test_tenant_id_alone_does_NOT_populate_admin_id(self) -> None:
        # Backward-compat read tolerance: tenant_id still accepted as a
        # field on input, but it is NOT mirrored to admin_id.  Callers
        # MUST send admin_id post-PR #100.
        s = SessionCreate.model_validate({"tenant_id": "acme"})
        self.assertEqual(s.tenant_id, "acme")
        self.assertIsNone(s.admin_id)

    def test_neither_key_is_fine(self) -> None:
        s = SessionCreate.model_validate({"channel": "web"})
        self.assertIsNone(s.tenant_id)
        self.assertIsNone(s.admin_id)


class TestSessionReadOutputAlias(unittest.TestCase):
    def _make(self, **overrides):
        now = datetime.now(timezone.utc)
        base = dict(
            id="sess-1",
            tenant_id="acme",
            domain_id="real-estate",
            agent_id=None,
            user_id=None,
            channel="web",
            status="active",
            created_at=now,
            updated_at=now,
        )
        base.update(overrides)
        return SessionRead(**base)

    def test_admin_id_mirrored_from_tenant_id(self) -> None:
        r = self._make()
        self.assertEqual(r.tenant_id, "acme")
        self.assertEqual(r.admin_id, "acme")

    def test_response_serialisation_contains_both_keys(self) -> None:
        r = self._make()
        dumped = r.model_dump()
        self.assertIn("tenant_id", dumped)
        self.assertIn("admin_id", dumped)
        self.assertEqual(dumped["tenant_id"], dumped["admin_id"])


class TestInputAliasClassRemoved(unittest.TestCase):
    def test_input_alias_class_not_importable(self) -> None:
        from app.schemas import _tenant_admin_alias as mod

        self.assertFalse(hasattr(mod, "TenantAdminInputAlias"))
        self.assertTrue(hasattr(mod, "TenantAdminOutputAlias"))


if __name__ == "__main__":
    unittest.main()
