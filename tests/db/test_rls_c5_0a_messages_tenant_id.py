"""Arc 9 C5.0a shape tests -- messages.admin_id additive migration.

This is the SCHEMA-DELTA migration that prepares messages for Wall-4
RLS. Unlike the C3/C4 sibling shape tests (which guard policy-create
migrations), this test guards:

  1. Chain integrity: C5.0a's down_revision is the final C4.3 head.
  2. Three-phase structure: add nullable, backfill, NOT NULL.
  3. Composite index creation for (admin_id, session_id).
  4. Idempotency on Phase 1 (re-run friendly).
  5. Orphan guard in Phase 3 (refuses to NOT NULL with NULL rows).
  6. Downgrade drops index + column in reverse order.
  7. Python-level importability (no syntax / import drift).

RUN:
    python -m pytest tests/db/test_rls_c5_0a_messages_tenant_id.py -v
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


REV_ID = "arc9_c5_0a_messages_tenant_id"
EXPECTED_DOWN = "arc9_c4_3f_rls_instance_admin_audit_logs"

VERSIONS_DIR = (
    Path(__file__).parent.parent.parent / "alembic" / "versions"
)
MIGRATION_PATH = VERSIONS_DIR / f"{REV_ID}.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        f"_c50a_test_{REV_ID}", str(MIGRATION_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestC50aMigrationShape(unittest.TestCase):

    def setUp(self):
        self.assertTrue(
            MIGRATION_PATH.exists(),
            f"Missing migration: {MIGRATION_PATH}",
        )
        self.text = MIGRATION_PATH.read_text()
        self.text_lower = self.text.lower()

    def test_revision_id_matches_filename(self):
        self.assertIn(f'revision = "{REV_ID}"', self.text)

    def test_chain_links_to_c4_3f_head(self):
        self.assertIn(
            f'down_revision = "{EXPECTED_DOWN}"',
            self.text,
            "C5.0a must chain immediately after C4.3f (the final C4 head)",
        )

    def test_phase1_adds_nullable_column(self):
        # The add_column call must mark the new column as nullable=True
        # so existing rows survive the metadata-only ALTER.
        self.assertIn("op.add_column", self.text)
        self.assertIn('"admin_id"', self.text)
        self.assertIn("nullable=True", self.text)
        self.assertIn("sa.String(length=100)", self.text)

    def test_phase1_idempotent_on_rerun(self):
        # Phase 1 must check whether the column already exists before
        # adding it -- otherwise a partial-failure rerun blows up on
        # the second ALTER.
        self.assertIn("inspect", self.text_lower)
        self.assertIn('if "admin_id" not in cols', self.text)

    def test_phase2_chunked_backfill(self):
        # The backfill must:
        #   - Loop until UPDATE returns rowcount 0
        #   - Use a CTE-batched UPDATE with LIMIT
        #   - Pull admin_id from the JOINED sessions row
        #   - Use a bounded batch size (not whole table)
        self.assertIn("BACKFILL_BATCH_SIZE", self.text)
        self.assertIn("while True", self.text)
        self.assertIn("rowcount == 0", self.text)
        self.assertIn("WITH batch AS", self.text)
        self.assertIn("JOIN sessions", self.text)
        self.assertIn("LIMIT", self.text)

    def test_phase3_orphan_guard(self):
        # Refuse to set NOT NULL if any row still has NULL admin_id
        # after the backfill loop completes.
        self.assertIn("WHERE admin_id IS NULL", self.text)
        self.assertIn("RuntimeError", self.text)
        self.assertIn("backfill incomplete", self.text)

    def test_phase3_alters_to_not_null(self):
        self.assertIn("op.alter_column", self.text)
        self.assertIn("nullable=False", self.text)

    def test_phase4_creates_composite_index(self):
        # Composite index on (admin_id, session_id) supports RLS
        # predicate AND list_messages access pattern.
        self.assertIn("ix_messages_tenant_id_session_id", self.text)
        self.assertIn('["admin_id", "session_id"]', self.text)

    def test_downgrade_drops_index_then_column(self):
        # Reverse order: index first, then column. SQL would error
        # otherwise (cannot drop indexed column).
        downgrade_idx = self.text.find("def downgrade")
        self.assertGreater(downgrade_idx, 0)
        downgrade_body = self.text[downgrade_idx:]
        idx_drop = downgrade_body.find("drop_index")
        col_drop = downgrade_body.find("drop_column")
        self.assertGreater(idx_drop, 0, "downgrade missing drop_index")
        self.assertGreater(col_drop, 0, "downgrade missing drop_column")
        self.assertLess(
            idx_drop,
            col_drop,
            "downgrade must drop index BEFORE dropping column",
        )

    def test_module_importable(self):
        # Catches syntax errors and bad imports that text-grep misses.
        module = _load_migration()
        self.assertEqual(module.revision, REV_ID)
        self.assertEqual(module.down_revision, EXPECTED_DOWN)
        self.assertTrue(callable(module.upgrade))
        self.assertTrue(callable(module.downgrade))
        self.assertEqual(module.BACKFILL_BATCH_SIZE, 5000)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
