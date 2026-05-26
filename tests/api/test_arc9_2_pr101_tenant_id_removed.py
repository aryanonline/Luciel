"""Arc 9.2 PR #101 -- ``tenant_id`` collapsed into ``admin_id``.

After PR #101 (Option A complete), the ``tenant_id`` HTTP / schema /
column surface no longer exists.  ``admin_id`` is the sole identifier.

This test verifies the collapse end-to-end at the schema layer:

* ``SessionCreate`` accepts ``admin_id`` only -- no ``tenant_id`` field.
* ``SessionRead`` exposes ``admin_id`` only -- no ``tenant_id`` mirror.
* The ``app.schemas._tenant_admin_alias`` module is gone.
* The ``app.db.admin_id_dual_write`` module is gone.
"""
from __future__ import annotations

import importlib
import unittest
from datetime import datetime, timezone

from app.schemas.session import SessionCreate, SessionRead


class TestSessionCreateAdminIdOnly(unittest.TestCase):
    def test_admin_id_accepted(self) -> None:
        s = SessionCreate.model_validate({"admin_id": "acme"})
        self.assertEqual(s.admin_id, "acme")

    def test_tenant_id_field_absent(self) -> None:
        self.assertNotIn("tenant_id", SessionCreate.model_fields)
        # And admin_id is present.
        self.assertIn("admin_id", SessionCreate.model_fields)


class TestSessionReadAdminIdOnly(unittest.TestCase):
    def test_session_read_emits_admin_id_only(self) -> None:
        now = datetime.now(timezone.utc)
        s = SessionRead.model_validate({
            "id": "sess_1",
            "admin_id": "acme",
            "domain_id": "real_estate",
            "agent_id": None,
            "user_id": None,
            "channel": "widget",
            "status": "active",
            "created_at": now,
            "updated_at": now,
        })
        dumped = s.model_dump()
        self.assertEqual(dumped["admin_id"], "acme")
        self.assertNotIn("tenant_id", dumped)


class TestLegacyAliasModulesGone(unittest.TestCase):
    def test_tenant_admin_alias_module_removed(self) -> None:
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("app.schemas._tenant_admin_alias")

    def test_admin_id_dual_write_module_removed(self) -> None:
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("app.db.admin_id_dual_write")


if __name__ == "__main__":
    unittest.main()
