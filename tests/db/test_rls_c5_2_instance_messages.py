"""Arc 9 C5.2 shape tests -- Wall-3 RLS policy on messages.

Guards:
    1. Chain integrity: down_revision = C5.1
    2. ENABLE RLS on messages (idempotent self-contained guard)
    3. CREATE POLICY messages_instance_isolation with NULL-permissive shape
    4. USING: luciel_instance_id::text = current_setting OR IS NULL
    5. WITH CHECK: asymmetric (matching OR (NULL AND empty GUC))
    6. ``::text`` cast on luciel_instance_id (Integer column)
    7. current_setting('app.instance_id', true) on both sides
    8. Downgrade DROPs policy but does NOT disable RLS (Wall-1 sibling)
    9. Module importable

RUN:
    python -m pytest tests/db/test_rls_c5_2_instance_messages.py -v
"""

from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path


REV_ID = "arc9_c5_2_rls_instance_messages"
EXPECTED_DOWN = "arc9_c5_1_rls_messages"

VERSIONS_DIR = (
    Path(__file__).parent.parent.parent / "alembic" / "versions"
)
MIGRATION_PATH = VERSIONS_DIR / f"{REV_ID}.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        f"_c52_test_{REV_ID}", str(MIGRATION_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestC52MigrationShape(unittest.TestCase):

    def setUp(self):
        self.assertTrue(MIGRATION_PATH.exists())
        self.text = MIGRATION_PATH.read_text()
        self.text_lower = self.text.lower()

    def test_revision_id(self):
        self.assertIn(f'revision = "{REV_ID}"', self.text)

    def test_chain_links_to_c5_1(self):
        self.assertIn(f'down_revision = "{EXPECTED_DOWN}"', self.text)

    def test_enables_rls_idempotently(self):
        self.assertIn(
            "alter table messages enable row level security",
            self.text_lower,
        )

    def test_creates_instance_isolation_policy(self):
        self.assertIn(
            "create policy messages_instance_isolation", self.text_lower
        )

    def test_policy_distinct_from_c51(self):
        # Distinct policy name from C5.1 (messages_tenant_isolation)
        # so both can coexist on the same table.
        self.assertNotIn("messages_tenant_isolation", self.text_lower)

    def test_using_null_permissive(self):
        # USING permits matching instance OR NULL row
        self.assertRegex(
            self.text_lower,
            r"using\s*\(",
        )
        self.assertIn("luciel_instance_id::text", self.text_lower)
        self.assertIn("current_setting('app.instance_id', true)", self.text_lower)
        self.assertIn("or luciel_instance_id is null", self.text_lower)

    def test_with_check_asymmetric(self):
        # WITH CHECK is stricter: NULL writes only when GUC is empty.
        self.assertRegex(
            self.text_lower,
            r"with\s+check\s*\(",
        )
        self.assertIn(
            "and current_setting('app.instance_id', true) = ''",
            self.text_lower,
        )

    def test_integer_cast(self):
        # luciel_instance_id is Integer; must cast to text for compare
        self.assertIn("luciel_instance_id::text", self.text_lower)

    def test_downgrade_drops_policy_only(self):
        # CRITICAL: downgrade must NOT disable RLS -- the Wall-1
        # sibling C5.1 still depends on it.
        downgrade_idx = self.text.find("def downgrade")
        body = self.text[downgrade_idx:]
        self.assertIn("drop policy if exists messages_instance_isolation", body.lower())
        self.assertNotIn("disable row level security", body.lower())

    def test_module_importable(self):
        module = _load_migration()
        self.assertEqual(module.revision, REV_ID)
        self.assertEqual(module.down_revision, EXPECTED_DOWN)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
