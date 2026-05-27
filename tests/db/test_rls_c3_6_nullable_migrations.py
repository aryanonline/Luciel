"""
Arc 9 C3.6 regression tests -- RLS migrations for the two remaining
NULL-permissive Wall-1 tables.

C3.6 covers the two tables that complete the C3 per-table RLS series:

  - retention_policies  (admin_id nullable, NULL = platform-wide policy)
  - deletion_logs       (admin_id nullable, NULL = platform-issued bulk delete)

Both use the C3.3 asymmetric NULL-permissive policy shape rather than
the strict-both-halves shape of C3.2 / C3.5. The shape is identical to
knowledge_embeddings (C3.3):

  USING       = admin_id IS NULL OR admin_id = current_setting()
  WITH CHECK  = (admin_id IS NULL AND current_setting() = 'platform')
                  OR admin_id = current_setting()

CONTRACT GUARDED (per migration):
    1. Syntactically valid Alembic revision file
    2. Chains to the previous sibling
    3. ENABLE ROW LEVEL SECURITY on its target table
    4. CREATE POLICY <table>_tenant_isolation
    5. USING has the NULL-permissive carveout (admin_id IS NULL OR ...)
    6. WITH CHECK gates NULL writes to the 'platform' sentinel
    7. USING and WITH CHECK are textually asymmetric (this is the
       key regression signal: a future refactor that 'simplifies'
       them to symmetric strict would lock platform admins out of
       writing platform-wide rows, and a refactor that 'simplifies'
       them to symmetric permissive would let any admin write NULL
       and forge platform-wide policies)
    8. Reversible downgrade (DROP IF EXISTS, then DISABLE)

WHY ONE TEST FILE FOR BOTH:
    The two migrations are byte-identical except for table name and
    revision chain. A parameterised shape test catches drift in
    either while staying cheap to read.

RUN:
    python -m pytest tests/db/test_rls_c3_6_nullable_migrations.py -v
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
C36_MIGRATIONS = [
    (
        "retention_policies",
        "arc9_c3_6a_rls_retention_policies",
        "arc9_c3_5e_rls_admin_widget_domains",
    ),
    (
        "deletion_logs",
        "arc9_c3_6b_rls_deletion_logs",
        "arc9_c3_6a_rls_retention_policies",
    ),
]


def _migration_path(rev_id: str) -> Path:
    return VERSIONS_DIR / f"{rev_id}.py"


def _load_migration(rev_id: str):
    """Import the migration file as a module.

    Catches Python-level drift (bad imports, syntax errors) that a
    text-grep would miss. Module name namespaced with _c36_test_ to
    avoid clashing with helpers in the C3.2 / C3.5 test files when
    pytest runs all of them in the same session.
    """
    path = _migration_path(rev_id)
    spec = importlib.util.spec_from_file_location(
        f"_c36_test_{rev_id}", str(path)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestC36MigrationsShape(unittest.TestCase):

    def _assert_migration_shape(self, table, rev_id, expected_down):
        """Common per-table NULL-permissive shape assertions."""
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

        # CREATE POLICY bound to the right table.
        self.assertIn(
            f"on {table}",
            text_lower,
            f"{rev_id}: policy not bound to table {table}",
        )

        # USING has the NULL-permissive carveout. The 'tenant_id IS
        # NULL OR ...' shape is the structural signal of asymmetry
        # on the read side -- if a future refactor accidentally
        # removes this, platform-wide rows become invisible to all
        # admins. Migration source uses pre-Arc-9.2 ``tenant_id``
        # name; the live column is now ``admin_id`` (PR #101 rename).
        self.assertRegex(
            text_lower,
            r"using\s*\(\s*tenant_id\s+is\s+null\s+or\s+tenant_id\s*=",
            f"{rev_id}: USING missing NULL-permissive carveout",
        )

        # WITH CHECK has the platform-sentinel branch. This is the
        # structural defence against any admin forging a NULL row.
        # We assert that the 'platform' literal appears inside a
        # WITH CHECK clause that also references 'admin_id IS NULL'.
        with_check_match = re.search(
            r"with\s+check\s*\((?P<body>.*?)\)\s*;",
            text_lower,
            re.DOTALL,
        )
        self.assertIsNotNone(
            with_check_match,
            f"{rev_id}: WITH CHECK clause missing or malformed",
        )
        body = with_check_match.group("body")
        self.assertIn(
            "tenant_id is null",
            body,
            f"{rev_id}: WITH CHECK missing NULL branch",
        )
        self.assertIn(
            "'platform'",
            body,
            f"{rev_id}: WITH CHECK missing 'platform' sentinel",
        )

        # USING and WITH CHECK are textually NOT identical. This
        # asymmetric requirement catches the 'simplification'
        # regression in either direction.
        using_match = re.search(
            r"using\s*\((?P<body>.*?)\)\s*\n", text_lower, re.DOTALL
        )
        self.assertIsNotNone(
            using_match,
            f"{rev_id}: USING clause missing or malformed",
        )
        using_body = using_match.group("body").strip()
        check_body = with_check_match.group("body").strip()
        self.assertNotEqual(
            using_body,
            check_body,
            f"{rev_id}: USING and WITH CHECK are identical -- "
            f"asymmetry lost",
        )

        # current_setting uses the missing-OK two-arg form.
        self.assertRegex(
            text_lower,
            r"current_setting\(\s*'app\.admin_id'\s*,\s*true\s*\)",
            f"{rev_id}: current_setting(..., true) missing",
        )

        # Downgrade drops policy and disables RLS.
        self.assertIn("drop policy if exists", text_lower, rev_id)
        self.assertIn(
            f"alter table {table} disable row level security",
            text_lower,
            f"{rev_id}: DISABLE RLS missing in downgrade",
        )

        # Importable.
        module = _load_migration(rev_id)
        self.assertEqual(module.revision, rev_id)
        self.assertEqual(module.down_revision, expected_down)
        self.assertTrue(callable(module.upgrade))
        self.assertTrue(callable(module.downgrade))

    def test_c36a_retention_policies(self):
        self._assert_migration_shape(
            "retention_policies",
            "arc9_c3_6a_rls_retention_policies",
            "arc9_c3_5e_rls_admin_widget_domains",
        )

    def test_c36b_deletion_logs(self):
        self._assert_migration_shape(
            "deletion_logs",
            "arc9_c3_6b_rls_deletion_logs",
            "arc9_c3_6a_rls_retention_policies",
        )


class TestC36ChainIntegrity(unittest.TestCase):
    """The 2 migrations form a contiguous chain after C3.5e.

    Same chain-integrity guard as C3.2 / C3.5 -- catches mid-chain
    inserts before staging.
    """

    def test_chain_links_to_c35e_at_head(self):
        module = _load_migration("arc9_c3_6a_rls_retention_policies")
        self.assertEqual(
            module.down_revision,
            "arc9_c3_5e_rls_admin_widget_domains",
        )

    def test_chain_is_contiguous(self):
        for i in range(1, len(C36_MIGRATIONS)):
            _, curr_rev, curr_down = C36_MIGRATIONS[i]
            _, prev_rev, _ = C36_MIGRATIONS[i - 1]
            self.assertEqual(
                curr_down,
                prev_rev,
                f"Chain break: {curr_rev} has down={curr_down}, "
                f"expected {prev_rev}",
            )


if __name__ == "__main__":
    sys.exit(
        0
        if unittest.main(exit=False).result.wasSuccessful()
        else 1
    )
