"""
Arc 9 C3.1 regression tests -- RLS migration shape for admin_audit_logs.

CONTRACT GUARDED:
    The Alembic migration ``arc9_c3_1_rls_admin_audit_logs`` MUST:
      1. Land on the current head (arc7_b_admins_last_signup_ip)
      2. ENABLE ROW LEVEL SECURITY on admin_audit_logs
      3. CREATE POLICY with BOTH a USING and WITH CHECK clause
         (read-side and write-side enforcement -- one without the
         other is a tenant-leak half-fix)
      4. Compare tenant_id to current_setting('app.admin_id', true)
         -- the second arg ``true`` is CRITICAL: it makes the function
         return empty string instead of raising when the GUC is unset
         (matches Arc 9 C2 listener behaviour for background paths)
      5. Provide a working downgrade that DROPs the policy AND
         DISABLES RLS (in that order)

THE BUGS THIS GUARDS AGAINST:
    - Author forgets WITH CHECK -> writes leak even though reads
      are gated. Insidious because a basic test of "can admin A see
      admin B's rows" still passes.
    - Author writes current_setting without ``, true`` -> background
      jobs that haven't bound an admin context crash with
      "unrecognized configuration parameter" instead of being denied.
    - Author writes the policy on the wrong table.

WHY UNIT (not DB-backed):
    The full DB-backed test of "admin A INSERTs a row with
    tenant_id='globex', does it succeed or fail" requires a live
    Postgres and is the responsibility of the G3 staging migration
    dry-run + C7 tenant-leak regression suite. THIS test catches
    structural drift in the migration text itself -- which is
    where the subtle one-token bugs happen.

RUN:
    python -m pytest tests/db/test_rls_admin_audit_logs_migration.py -v
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


MIGRATION_PATH = (
    Path(__file__).parent.parent.parent
    / "alembic"
    / "versions"
    / "arc9_c3_1_rls_admin_audit_logs.py"
)


class TestC31MigrationShape(unittest.TestCase):
    """Structural assertions on the migration's SQL text."""

    @classmethod
    def setUpClass(cls):
        cls.text = MIGRATION_PATH.read_text()
        # Lowercase the SQL bodies for case-insensitive matching while
        # preserving the original for line-number diagnostics.
        cls.text_lower = cls.text.lower()

    def test_migration_file_exists(self):
        self.assertTrue(
            MIGRATION_PATH.exists(),
            f"Migration file missing: {MIGRATION_PATH}",
        )

    def test_revision_id_matches_filename(self):
        m = re.search(r'^revision\s*=\s*"([^"]+)"', self.text, re.MULTILINE)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "arc9_c3_1_rls_admin_audit_logs")

    def test_down_revision_points_at_current_head(self):
        """Lands on arc7_b_admins_last_signup_ip -- the production head
        at Arc 9 C3.1 authoring time. If a later commit moves the head
        before this lands, the migration will fail to apply and CI
        will catch it."""
        m = re.search(
            r'^down_revision\s*=\s*"([^"]+)"',
            self.text,
            re.MULTILINE,
        )
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "arc7_b_admins_last_signup_ip")

    def test_upgrade_enables_rls_on_admin_audit_logs(self):
        self.assertIn(
            "alter table admin_audit_logs enable row level security",
            self.text_lower,
            "Migration MUST ENABLE ROW LEVEL SECURITY on admin_audit_logs.",
        )

    def test_upgrade_creates_policy_on_admin_audit_logs(self):
        self.assertIn("create policy", self.text_lower)
        self.assertIn("on admin_audit_logs", self.text_lower)

    def test_policy_has_using_clause(self):
        """USING gates SELECT/UPDATE/DELETE (read-side)."""
        self.assertRegex(
            self.text_lower,
            r"using\s*\(",
            "Policy MUST have a USING clause -- without it reads leak.",
        )

    def test_policy_has_with_check_clause(self):
        """WITH CHECK gates INSERT/UPDATE (write-side).

        This is the most common drift point: an author writes USING
        only and assumes that's sufficient. It is NOT -- an admin
        could still INSERT a row with someone else's tenant_id.
        """
        self.assertRegex(
            self.text_lower,
            r"with\s+check\s*\(",
            "Policy MUST have a WITH CHECK clause -- without it "
            "writes leak (admin A could INSERT a row with "
            "tenant_id='admin B').",
        )

    def test_policy_predicate_uses_current_setting_with_missing_ok(self):
        """current_setting('app.admin_id', true) -- the ``true`` arg
        makes the function return '' instead of raising when the GUC
        is unset (background-path safe).
        """
        self.assertRegex(
            self.text_lower,
            r"current_setting\(\s*'app\.admin_id'\s*,\s*true\s*\)",
            "Policy MUST call current_setting('app.admin_id', true) "
            "-- the ``true`` second arg is required for "
            "background-job paths that haven't bound an admin context "
            "to deny gracefully instead of raising.",
        )

    def test_policy_predicate_compares_tenant_id_column(self):
        """The compared column MUST be tenant_id (not admin_id, not
        owner_id, not anything else)."""
        self.assertIn("tenant_id = current_setting", self.text_lower)

    def test_downgrade_drops_policy_then_disables_rls(self):
        """Order matters: dropping the policy first leaves the table
        cleanly empty of RLS artifacts."""
        # Locate the downgrade() body.
        m = re.search(
            r"def downgrade\(\) -> None:(.*?)(?=\Z|\ndef )",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "downgrade() function missing")
        downgrade_body = m.group(1).lower()
        # Drop must appear BEFORE disable.
        drop_idx = downgrade_body.find("drop policy")
        disable_idx = downgrade_body.find("disable row level security")
        self.assertGreaterEqual(drop_idx, 0, "DROP POLICY missing")
        self.assertGreaterEqual(
            disable_idx, 0, "DISABLE ROW LEVEL SECURITY missing"
        )
        self.assertLess(
            drop_idx,
            disable_idx,
            "Downgrade MUST drop policy BEFORE disabling RLS so the "
            "table is left clean.",
        )

    def test_downgrade_uses_if_exists_for_idempotency(self):
        """DROP POLICY IF EXISTS makes the downgrade idempotent --
        safe to run twice without raising."""
        self.assertIn(
            "drop policy if exists",
            self.text_lower,
            "DROP should use IF EXISTS for idempotent rollback.",
        )


class TestC31MigrationAlembicResolution(unittest.TestCase):
    """Verify the migration can be parsed by Alembic without raising.

    Catches: malformed Python, missing imports, syntax errors that
    a pure text-grep would miss.
    """

    def test_migration_module_imports_cleanly(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "arc9_c3_1_rls_admin_audit_logs_test",
            str(MIGRATION_PATH),
        )
        module = importlib.util.module_from_spec(spec)
        # If this raises, the migration has a Python-level defect.
        spec.loader.exec_module(module)
        self.assertEqual(
            module.revision, "arc9_c3_1_rls_admin_audit_logs"
        )
        self.assertEqual(
            module.down_revision, "arc7_b_admins_last_signup_ip"
        )
        self.assertTrue(callable(module.upgrade))
        self.assertTrue(callable(module.downgrade))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
