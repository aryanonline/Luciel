"""Arc 9 C5.1 shape tests -- RLS policy on messages (Wall 1 strict).

Guards the C3.2-shape Wall-1 RLS migration for the messages table.
Strict (no NULL carveout) because messages.admin_id is NOT NULL
after C5.0a's Phase 3 flip.

CONTRACT GUARDED:
    1. Chain integrity: down_revision = C5.0b
    2. ENABLE ROW LEVEL SECURITY on messages
    3. CREATE POLICY messages_tenant_isolation with USING + WITH CHECK
    4. Predicate compares admin_id = current_setting('app.admin_id', true)
    5. No NULL-permissive carveout (strict shape)
    6. Downgrade drops policy + disables RLS
    7. Module importable

RUN:
    python -m pytest tests/db/test_rls_c5_1_messages.py -v
"""

from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path


REV_ID = "arc9_c5_1_rls_messages"
EXPECTED_DOWN = "arc9_c5_0b_messages_instance_id"

VERSIONS_DIR = (
    Path(__file__).parent.parent.parent / "app" / "migrations" / "versions"
)
MIGRATION_PATH = VERSIONS_DIR / f"{REV_ID}.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        f"_c51_test_{REV_ID}", str(MIGRATION_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestC51MigrationShape(unittest.TestCase):

    def setUp(self):
        self.assertTrue(MIGRATION_PATH.exists())
        self.text = MIGRATION_PATH.read_text()
        self.text_lower = self.text.lower()

    def test_revision_id(self):
        self.assertIn(f'revision = "{REV_ID}"', self.text)

    def test_chain_links_to_c5_0b(self):
        self.assertIn(f'down_revision = "{EXPECTED_DOWN}"', self.text)

    def test_enables_rls_on_messages(self):
        self.assertIn(
            "alter table messages enable row level security",
            self.text_lower,
        )

    def test_creates_tenant_isolation_policy(self):
        self.assertIn("create policy messages_tenant_isolation", self.text_lower)
        self.assertIn("on messages", self.text_lower)

    def test_policy_is_permissive_all_public(self):
        self.assertIn("as permissive", self.text_lower)
        self.assertIn("for all", self.text_lower)
        self.assertIn("to public", self.text_lower)

    def test_using_clause(self):
        self.assertRegex(
            self.text_lower,
            r"using\s*\(\s*tenant_id\s*=\s*current_setting\(\s*'app\.admin_id'\s*,\s*true\s*\)\s*\)",
        )

    def test_with_check_clause(self):
        self.assertRegex(
            self.text_lower,
            r"with\s+check\s*\(\s*tenant_id\s*=\s*current_setting\(\s*'app\.admin_id'\s*,\s*true\s*\)\s*\)",
        )

    def test_no_null_permissive_carveout(self):
        # Strict shape -- no "OR admin_id IS NULL" in the policy.
        # If this fires, the migration accidentally adopted the C3.3
        # / C4.3 NULL-permissive shape on a NOT-NULL column.
        self.assertNotIn("is null", self.text_lower)

    def test_downgrade(self):
        self.assertIn(
            "drop policy if exists messages_tenant_isolation",
            self.text_lower,
        )
        self.assertIn(
            "alter table messages disable row level security",
            self.text_lower,
        )

    def test_module_importable(self):
        module = _load_migration()
        self.assertEqual(module.revision, REV_ID)
        self.assertEqual(module.down_revision, EXPECTED_DOWN)
        self.assertTrue(callable(module.upgrade))
        self.assertTrue(callable(module.downgrade))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
