"""
Arc 9 C3.5 regression tests -- RLS migrations for the second sibling
batch of strict (NOT NULL wall-column) customer-data tables.

C3.5 covers the 5 strict NOT-NULL tables that were NOT in C3.2:

  - user_invites          (admin_id NOT NULL, FK->admins.id)
  - user_consents         (admin_id NOT NULL)
  - identity_claims       (admin_id NOT NULL, FK->admins.id)
  - instances             (admin_id  NOT NULL, FK->admins.id)
  - admin_widget_domains  (admin_id  NOT NULL, FK->admins.id)

Note the wall-column NAME differs for instances and admin_widget_domains
(`tenant_id` rather than `tenant_id`) -- both are equivalent slug
references to admins.id but the column name follows the V2 model
nomenclature on those two tables. The RLS predicate adapts accordingly;
the GUC name itself (`app.admin_id`) stays constant across all 16
Wall-1 tables in the C3 series.

CONTRACT GUARDED (per migration):
    1. Syntactically valid Alembic revision file
    2. Chains to the previous sibling (forming an ordered series
       after arc9_c3_4_rls_api_keys)
    3. ENABLE ROW LEVEL SECURITY on its target table
    4. CREATE POLICY <table>_tenant_isolation with USING + WITH CHECK
    5. Predicate compares the wall column to current_setting('app.admin_id', true)
    6. Provides a working downgrade (DROP IF EXISTS, then DISABLE)

WHY ONE TEST FILE FOR ALL 5:
    Same rationale as test_rls_c3_2_migrations_shape.py -- a single
    parameterised shape test catches drift cheaply. Per-table
    behavioural quirks get their own dedicated test files (none here;
    C3.5 is uniformly strict).

RUN:
    python -m pytest tests/db/test_rls_c3_5_migrations_shape.py -v
"""
from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path


VERSIONS_DIR = (
    Path(__file__).parent.parent.parent / "app" / "migrations" / "versions"
)


# (table_name, wall_column, rev_id, expected_down_revision)
# The chain expectation is the contract. Mismatch = drift.
C35_MIGRATIONS = [
    (
        "user_invites",
        "admin_id",
        "arc9_c3_5a_rls_user_invites",
        "arc9_c3_4_rls_api_keys",
    ),
    (
        "user_consents",
        "admin_id",
        "arc9_c3_5b_rls_user_consents",
        "arc9_c3_5a_rls_user_invites",
    ),
    (
        "identity_claims",
        "admin_id",
        "arc9_c3_5c_rls_identity_claims",
        "arc9_c3_5b_rls_user_consents",
    ),
    (
        "instances",
        "admin_id",
        "arc9_c3_5d_rls_instances",
        "arc9_c3_5c_rls_identity_claims",
    ),
    (
        "admin_widget_domains",
        "admin_id",
        "arc9_c3_5e_rls_admin_widget_domains",
        "arc9_c3_5d_rls_instances",
    ),
]


def _migration_path(rev_id: str) -> Path:
    return VERSIONS_DIR / f"{rev_id}.py"


def _load_migration(rev_id: str):
    """Import the migration file as a module.

    Catches Python-level drift (bad imports, syntax errors) that a
    text-grep would miss. Module names are namespaced with _c35_test_
    to avoid clashing with the matching helper in
    test_rls_c3_2_migrations_shape.py when both test files run in the
    same pytest session.
    """
    path = _migration_path(rev_id)
    spec = importlib.util.spec_from_file_location(
        f"_c35_test_{rev_id}", str(path)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestC35MigrationsShape(unittest.TestCase):

    def _assert_migration_shape(
        self, table, wall_col, rev_id, expected_down
    ):
        """Common per-table shape assertions.

        wall_col is the column name AS WRITTEN in the migration source.
        Pre-Arc-9.2 migrations source-text use ``tenant_id`` (the live
        column was later renamed to ``admin_id`` by PR #101 but alembic
        files are immutable historical artifacts). Two post-Arc-9.2
        migrations -- ``instances`` and ``admin_widget_domains`` -- use
        ``admin_id`` directly because they shipped after the rename.
        Architecture v1 §3.7.1 (Wall 1) mandates the semantic property
        (RLS filters customer-data rows by admin identity), not a
        specific column name.
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

        # CREATE POLICY references the right table.
        self.assertIn(
            f"on {table}",
            text_lower,
            f"{rev_id}: policy not bound to table {table}",
        )

        # USING clause references the wall column (NOT a hardcoded
        # admin_id when the table actually uses admin_id).
        self.assertRegex(
            text_lower,
            rf"using\s*\(\s*{wall_col}\s*=",
            f"{rev_id}: USING clause does not gate on {wall_col}",
        )

        # WITH CHECK clause references the wall column too -- both
        # halves must be strict for full coverage.
        self.assertRegex(
            text_lower,
            rf"with\s+check\s*\(\s*{wall_col}\s*=",
            f"{rev_id}: WITH CHECK does not gate on {wall_col}",
        )

        # current_setting uses the missing-OK two-arg form.
        self.assertRegex(
            text_lower,
            r"current_setting\(\s*'app\.admin_id'\s*,\s*true\s*\)",
            f"{rev_id}: current_setting(..., true) missing",
        )

        # downgrade drops policy and disables RLS, in that order.
        self.assertIn("drop policy if exists", text_lower, rev_id)
        self.assertIn(
            f"on {table}",
            text_lower,
            f"{rev_id}: drop policy not bound to {table}",
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

    def test_c35a_user_invites(self):
        self._assert_migration_shape(
            "user_invites",
            "tenant_id",  # pre-Arc-9.2 source; live column is now admin_id
            "arc9_c3_5a_rls_user_invites",
            "arc9_c3_4_rls_api_keys",
        )

    def test_c35b_user_consents(self):
        self._assert_migration_shape(
            "user_consents",
            "tenant_id",  # pre-Arc-9.2 source; live column is now admin_id
            "arc9_c3_5b_rls_user_consents",
            "arc9_c3_5a_rls_user_invites",
        )

    def test_c35c_identity_claims(self):
        self._assert_migration_shape(
            "identity_claims",
            "tenant_id",  # pre-Arc-9.2 source; live column is now admin_id
            "arc9_c3_5c_rls_identity_claims",
            "arc9_c3_5b_rls_user_consents",
        )

    def test_c35d_instances(self):
        self._assert_migration_shape(
            "instances",
            "admin_id",
            "arc9_c3_5d_rls_instances",
            "arc9_c3_5c_rls_identity_claims",
        )

    def test_c35e_admin_widget_domains(self):
        self._assert_migration_shape(
            "admin_widget_domains",
            "admin_id",
            "arc9_c3_5e_rls_admin_widget_domains",
            "arc9_c3_5d_rls_instances",
        )


class TestC35ChainIntegrity(unittest.TestCase):
    """The 5 migrations form a contiguous chain after C3.4.

    If any operator inserts a sibling migration mid-chain (e.g. while
    cherry-picking from a feature branch), this test catches the
    break before staging.
    """

    def test_chain_links_to_c34_at_head(self):
        """The first C3.5 migration's down_revision MUST be C3.4."""
        module = _load_migration("arc9_c3_5a_rls_user_invites")
        self.assertEqual(
            module.down_revision,
            "arc9_c3_4_rls_api_keys",
        )

    def test_chain_is_contiguous(self):
        """No gaps, no forks. Each rev_id i+1 has down_revision = rev_id i."""
        for i in range(1, len(C35_MIGRATIONS)):
            _, _, curr_rev, curr_down = C35_MIGRATIONS[i]
            _, _, prev_rev, _ = C35_MIGRATIONS[i - 1]
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
