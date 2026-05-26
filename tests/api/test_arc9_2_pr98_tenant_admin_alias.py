"""Arc 9.2 PR #98 -- HTTP boundary alias between tenant_id and admin_id.

Verifies the input/output Pydantic mixins in
``app.schemas._tenant_admin_alias`` accept and emit both keys.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.schemas.session import SessionCreate, SessionRead


class TestSessionCreateInputAlias(unittest.TestCase):
    def test_admin_id_alone_populates_tenant_id(self) -> None:
        s = SessionCreate.model_validate({"admin_id": "acme"})
        self.assertEqual(s.tenant_id, "acme")
        self.assertEqual(s.admin_id, "acme")

    def test_tenant_id_alone_populates_admin_id(self) -> None:
        s = SessionCreate.model_validate({"tenant_id": "acme"})
        self.assertEqual(s.tenant_id, "acme")
        self.assertEqual(s.admin_id, "acme")

    def test_both_keys_passes_through_unchanged(self) -> None:
        s = SessionCreate.model_validate(
            {"tenant_id": "acme", "admin_id": "acme"}
        )
        self.assertEqual(s.tenant_id, "acme")
        self.assertEqual(s.admin_id, "acme")

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


if __name__ == "__main__":
    unittest.main()
