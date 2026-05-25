"""Arc 9 C5.0b shape tests -- messages.luciel_instance_id additive migration.

Guards:
  1. Chain integrity: C5.0b's down_revision is C5.0a.
  2. Column stays NULLABLE (Wall-3 doctrine -- NULL is legitimate).
  3. Backfill copies sessions.luciel_instance_id, including NULL.
  4. Composite index (luciel_instance_id, session_id) created.
  5. Idempotency on Phase 1 + Phase 2 re-run.
  6. NO NOT NULL flip (unlike C5.0a).
  7. Downgrade drops index + column in reverse order.
  8. Python-level importability.

RUN:
    python -m pytest tests/db/test_rls_c5_0b_messages_instance_id.py -v
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


REV_ID = "arc9_c5_0b_messages_instance_id"
EXPECTED_DOWN = "arc9_c5_0a_messages_tenant_id"

VERSIONS_DIR = (
    Path(__file__).parent.parent.parent / "alembic" / "versions"
)
MIGRATION_PATH = VERSIONS_DIR / f"{REV_ID}.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        f"_c50b_test_{REV_ID}", str(MIGRATION_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestC50bMigrationShape(unittest.TestCase):

    def setUp(self):
        self.assertTrue(MIGRATION_PATH.exists())
        self.text = MIGRATION_PATH.read_text()

    def test_revision_id(self):
        self.assertIn(f'revision = "{REV_ID}"', self.text)

    def test_chain_links_to_c5_0a(self):
        self.assertIn(f'down_revision = "{EXPECTED_DOWN}"', self.text)

    def test_phase1_adds_nullable_integer_column(self):
        self.assertIn("op.add_column", self.text)
        self.assertIn('"luciel_instance_id"', self.text)
        self.assertIn("sa.Integer()", self.text)
        self.assertIn("nullable=True", self.text)

    def test_phase1_idempotent_on_rerun(self):
        self.assertIn('if "luciel_instance_id" not in cols', self.text)

    def test_phase2_chunked_backfill(self):
        self.assertIn("BACKFILL_BATCH_SIZE", self.text)
        self.assertIn("while True", self.text)
        self.assertIn("rowcount == 0", self.text)
        self.assertIn("WITH batch AS", self.text)
        self.assertIn("JOIN sessions", self.text)

    def test_phase2_only_backfills_when_source_not_null(self):
        # The backfill should skip rows where the source session has
        # NULL luciel_instance_id -- copying NULL would still loop
        # forever since the WHERE clause is on m.luciel_instance_id
        # IS NULL.
        self.assertIn("s.luciel_instance_id IS NOT NULL", self.text)

    def test_no_not_null_flip(self):
        # Wall-3 NULL-permissive doctrine: column stays nullable.
        # NO alter_column to NOT NULL anywhere in the migration.
        self.assertNotIn("nullable=False", self.text)
        self.assertNotIn("op.alter_column", self.text)

    def test_phase3_composite_index(self):
        self.assertIn("ix_messages_luciel_instance_id_session_id", self.text)
        self.assertIn(
            '["luciel_instance_id", "session_id"]', self.text
        )

    def test_downgrade_order(self):
        downgrade_idx = self.text.find("def downgrade")
        body = self.text[downgrade_idx:]
        idx_drop = body.find("drop_index")
        col_drop = body.find("drop_column")
        self.assertGreater(idx_drop, 0)
        self.assertGreater(col_drop, 0)
        self.assertLess(idx_drop, col_drop)

    def test_module_importable(self):
        module = _load_migration()
        self.assertEqual(module.revision, REV_ID)
        self.assertEqual(module.down_revision, EXPECTED_DOWN)
        self.assertTrue(callable(module.upgrade))
        self.assertTrue(callable(module.downgrade))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
