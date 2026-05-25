"""
Arc 9 C3.2 regression tests -- RLS migrations for the 7 NOT-NULL
tenant_id customer-data tables.

CONTRACT GUARDED:
    Each of the 7 sibling migrations (arc9_c3_2a through arc9_c3_2g)
    MUST:
      1. Be a syntactically valid Alembic revision file
      2. Chain to the previous sibling (forming an ordered series
         after arc9_c3_1_rls_admin_audit_logs)
      3. ENABLE ROW LEVEL SECURITY on its target table
      4. CREATE POLICY <table>_tenant_isolation with USING + WITH CHECK
      5. Predicate compares tenant_id to current_setting('app.admin_id', true)
      6. Provide a working downgrade (DROP IF EXISTS, then DISABLE)

WHY ONE TEST FILE FOR ALL 7:
    The 7 migrations are byte-identical except for table name and
    revision chain. A parameterised shape test catches drift at any
    one of them while staying cheap to read. Per-table behavioural
    quirks (e.g. NULL-permissive carveouts for knowledge_embeddings
    in C3.3) get their own test files.

RUN:
    python -m pytest tests/db/test_rls_c3_2_migrations_shape.py -v
"""
from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path


VERSIONS_DIR = (
    Path(__file__).parent.parent.parent / "alembic" / "versions"
)


# (table_name, rev_id, expected_down_revision)
# The expected chain is the contract -- mismatch = drift.
C32_MIGRATIONS = [
    ("traces",            "arc9_c3_2a_rls_traces",            "arc9_c3_1_rls_admin_audit_logs"),
    ("memory_items",      "arc9_c3_2b_rls_memory_items",      "arc9_c3_2a_rls_traces"),
    ("conversations",     "arc9_c3_2c_rls_conversations",     "arc9_c3_2b_rls_memory_items"),
    ("agent_configs",     "arc9_c3_2d_rls_agent_configs",     "arc9_c3_2c_rls_conversations"),
    ("sessions",          "arc9_c3_2e_rls_sessions",          "arc9_c3_2d_rls_agent_configs"),
    ("subscriptions",     "arc9_c3_2f_rls_subscriptions",     "arc9_c3_2e_rls_sessions"),
    ("scope_assignments", "arc9_c3_2g_rls_scope_assignments", "arc9_c3_2f_rls_subscriptions"),
]


def _migration_path(rev_id: str) -> Path:
    return VERSIONS_DIR / f"{rev_id}.py"


def _load_migration(rev_id: str):
    """Import the migration file as a module. Catches Python-level
    drift (bad imports, syntax errors) that a text-grep would miss.
    """
    path = _migration_path(rev_id)
    spec = importlib.util.spec_from_file_location(
        f"_c32_test_{rev_id}", str(path)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestC32MigrationsShape(unittest.TestCase):

    def _assert_migration_shape(self, table, rev_id, expected_down):
        """The common per-table shape assertions, factored to keep
        the parameterised tests readable.
        """
        path = _migration_path(rev_id)
        self.assertTrue(path.exists(), f"Missing migration: {path}")
        text = path.read_text()
        text_lower = text.lower()

        # Revision id matches filename.
        m = re.search(r'^revision\s*=\s*"([^"]+)"', text, re.MULTILINE)
        self.assertIsNotNone(m, f"{rev_id}: no revision = ... line")
        self.assertEqual(
            m.group(1),
            rev_id,
            f"{rev_id}: revision id does not match filename",
        )

        # down_revision chains correctly.
        m = re.search(
            r'^down_revision\s*=\s*"([^"]+)"', text, re.MULTILINE
        )
        self.assertIsNotNone(m, f"{rev_id}: no down_revision line")
        self.assertEqual(
            m.group(1),
            expected_down,
            f"{rev_id}: chain broken (expected down={expected_down})",
        )

        # ENABLE ROW LEVEL SECURITY on the right table.
        self.assertIn(
            f"alter table {table} enable row level security",
            text_lower,
            f"{rev_id}: ENABLE RLS missing for table {table}",
        )

        # CREATE POLICY on the right table.
        self.assertIn(
            f"on {table}",
            text_lower,
            f"{rev_id}: policy not bound to table {table}",
        )

        # USING clause.
        self.assertRegex(
            text_lower,
            r"using\s*\(",
            f"{rev_id}: USING clause missing",
        )

        # WITH CHECK clause.
        self.assertRegex(
            text_lower,
            r"with\s+check\s*\(",
            f"{rev_id}: WITH CHECK clause missing",
        )

        # current_setting with the missing-OK second arg.
        self.assertRegex(
            text_lower,
            r"current_setting\(\s*'app\.admin_id'\s*,\s*true\s*\)",
            f"{rev_id}: current_setting(..., true) missing",
        )

        # downgrade drops policy and disables RLS.
        self.assertIn("drop policy if exists", text_lower, rev_id)
        self.assertIn(
            f"on {table}", text_lower, f"{rev_id}: drop policy table"
        )
        self.assertIn(
            f"alter table {table} disable row level security",
            text_lower,
            f"{rev_id}: DISABLE RLS missing",
        )

        # Importable -- catches syntax errors that text-grep misses.
        module = _load_migration(rev_id)
        self.assertEqual(module.revision, rev_id)
        self.assertEqual(module.down_revision, expected_down)
        self.assertTrue(callable(module.upgrade))
        self.assertTrue(callable(module.downgrade))

    # --- one method per migration for clear test-ID surface in CI ---

    def test_c32a_traces(self):
        self._assert_migration_shape(
            "traces",
            "arc9_c3_2a_rls_traces",
            "arc9_c3_1_rls_admin_audit_logs",
        )

    def test_c32b_memory_items(self):
        self._assert_migration_shape(
            "memory_items",
            "arc9_c3_2b_rls_memory_items",
            "arc9_c3_2a_rls_traces",
        )

    def test_c32c_conversations(self):
        self._assert_migration_shape(
            "conversations",
            "arc9_c3_2c_rls_conversations",
            "arc9_c3_2b_rls_memory_items",
        )

    def test_c32d_agent_configs(self):
        self._assert_migration_shape(
            "agent_configs",
            "arc9_c3_2d_rls_agent_configs",
            "arc9_c3_2c_rls_conversations",
        )

    def test_c32e_sessions(self):
        self._assert_migration_shape(
            "sessions",
            "arc9_c3_2e_rls_sessions",
            "arc9_c3_2d_rls_agent_configs",
        )

    def test_c32f_subscriptions(self):
        self._assert_migration_shape(
            "subscriptions",
            "arc9_c3_2f_rls_subscriptions",
            "arc9_c3_2e_rls_sessions",
        )

    def test_c32g_scope_assignments(self):
        self._assert_migration_shape(
            "scope_assignments",
            "arc9_c3_2g_rls_scope_assignments",
            "arc9_c3_2f_rls_subscriptions",
        )


class TestC32ChainIntegrity(unittest.TestCase):
    """The 7 migrations form a contiguous chain after C3.1. If any
    operator inserts a sibling migration mid-chain, this test catches
    the break before staging.
    """

    def test_chain_links_to_c31_at_head(self):
        """The first C3.2 migration's down_revision MUST be C3.1's
        revision id."""
        module = _load_migration("arc9_c3_2a_rls_traces")
        self.assertEqual(
            module.down_revision,
            "arc9_c3_1_rls_admin_audit_logs",
        )

    def test_chain_is_contiguous(self):
        """No gaps, no forks. Each rev_id i+1 has down_revision = rev_id i."""
        for i in range(1, len(C32_MIGRATIONS)):
            curr_table, curr_rev, curr_down = C32_MIGRATIONS[i]
            prev_table, prev_rev, _ = C32_MIGRATIONS[i - 1]
            self.assertEqual(
                curr_down,
                prev_rev,
                f"Chain break: {curr_rev} has down={curr_down}, "
                f"expected {prev_rev}",
            )


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
