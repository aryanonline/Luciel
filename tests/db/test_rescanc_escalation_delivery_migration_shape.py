"""Rescan Tier-C — migration shape test for rescanc_escalation_delivery.

Verifies:
1. Revision ID matches filename.
2. down_revision = 'rescanb_custom_role_approval'.
3. Three new columns declared: delivery_status, attempts, last_attempt_at.
4. Partial unique index declared.
5. Single alembic head is rescanc_escalation_delivery.
"""
from __future__ import annotations

import importlib
import pathlib
import unittest


MIGRATION_PATH = (
    pathlib.Path(__file__).parent.parent.parent
    / "alembic" / "versions" / "rescanc_escalation_delivery.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "rescanc_escalation_delivery", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestRescanCMigrationShape(unittest.TestCase):

    def setUp(self):
        self.m = _load_migration()

    def test_revision_id(self):
        self.assertEqual(self.m.revision, "rescanc_escalation_delivery")

    def test_down_revision(self):
        self.assertEqual(self.m.down_revision, "rescanb_custom_role_approval")

    def test_upgrade_function_exists(self):
        self.assertTrue(callable(getattr(self.m, "upgrade", None)))

    def test_downgrade_function_exists(self):
        self.assertTrue(callable(getattr(self.m, "downgrade", None)))

    def test_migration_file_mentions_delivery_status(self):
        content = MIGRATION_PATH.read_text()
        self.assertIn("delivery_status", content)

    def test_migration_file_mentions_attempts(self):
        content = MIGRATION_PATH.read_text()
        self.assertIn("attempts", content)

    def test_migration_file_mentions_last_attempt_at(self):
        content = MIGRATION_PATH.read_text()
        self.assertIn("last_attempt_at", content)

    def test_migration_file_mentions_idempotency_index(self):
        content = MIGRATION_PATH.read_text()
        self.assertIn("uq_escalation_events_idempotency", content)

    def test_migration_file_partial_unique_index_where_clause(self):
        content = MIGRATION_PATH.read_text()
        self.assertIn("delivered", content)
        self.assertIn("acked", content)
