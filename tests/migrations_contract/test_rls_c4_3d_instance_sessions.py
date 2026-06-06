"""
Arc 9 C4.3d regression tests -- shape of the Wall-3 RLS policy on sessions.

Asserts (via regex over the migration source, not against a live DB):
  1. Migration file exists with the expected name.
  2. revision id matches expected.
  3. down_revision points at the expected predecessor (chain link).
  4. Upgrade ENABLEs RLS on sessions.
  5. Upgrade CREATEs POLICY sessions_instance_isolation -- name MUST
     differ from any Wall-1 *_tenant_isolation policy on this table.
  6. USING clause is NULL-permissive (contains ``IS NULL``).
  7. WITH CHECK clause is strict (NULL write only when GUC = '').
  8. Policy predicate uses ``current_setting('app.instance_id', true)``
     with the missing_ok flag.
  9. Policy predicate casts ``luciel_instance_id::text``.
 10. Downgrade drops the policy and disables RLS.
 11. Migration module imports cleanly via Alembic ScriptDirectory.

Real-DB enforcement of these predicates is deferred to the C7
regression suite.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = REPO_ROOT / "app/migrations/versions/arc9_c4_3d_rls_instance_sessions.py"
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


class TestC43DMigrationShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = MIGRATION_PATH.read_text()

    def test_migration_file_exists(self):
        self.assertTrue(MIGRATION_PATH.exists(), MIGRATION_PATH)

    def test_revision_id(self):
        m = re.search(r'^revision\s*=\s*"([^"]+)"', self.src, re.M)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "arc9_c4_3d_rls_instance_sessions")

    def test_down_revision(self):
        m = re.search(r'^down_revision\s*=\s*"([^"]+)"', self.src, re.M)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "arc9_c4_3c_rls_instance_memory_items")

    def test_upgrade_enables_rls(self):
        self.assertRegex(
            self.src,
            r"ALTER\s+TABLE\s+sessions\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY",
        )

    def test_upgrade_creates_instance_policy(self):
        # Policy name MUST be the *_instance_isolation variant, never
        # the *_tenant_isolation variant (which is the C3 Wall-1 name).
        self.assertRegex(
            self.src,
            r"CREATE\s+POLICY\s+sessions_instance_isolation",
        )
        # Make sure we did NOT accidentally reuse the Wall-1 name.
        self.assertNotRegex(
            self.src,
            r"CREATE\s+POLICY\s+sessions_tenant_isolation",
        )

    def test_using_clause_is_null_permissive(self):
        # USING must contain ``IS NULL`` so NULL-instance rows are
        # visible (cross-instance / admin-level rows).
        # Use a simple substring search on the full source -- the
        # SQL block contains both USING and WITH CHECK, and both
        # have IS NULL, so we count occurrences instead.
        is_null_count = len(re.findall(r"IS\s+NULL", self.src, re.I))
        self.assertGreaterEqual(
            is_null_count, 2,
            "Expected IS NULL in both USING and WITH CHECK clauses",
        )

    def test_with_check_clause_gates_null_writes(self):
        # WITH CHECK must include the ``current_setting(...) = ''``
        # gate on NULL writes -- that is the asymmetric write
        # restriction. Without this gate, instance-A could inject
        # NULL rows visible to instance-B.
        self.assertRegex(
            self.src,
            r"current_setting\(\s*'app\.instance_id'\s*,\s*true\s*\)\s*=\s*''",
        )

    def test_predicate_uses_missing_ok(self):
        # Every reference to the GUC MUST pass the missing_ok=true
        # second arg so an unset GUC returns '' instead of raising.
        refs = re.findall(
            r"current_setting\(\s*'app\.instance_id'\s*,\s*(\w+)\s*\)",
            self.src,
        )
        self.assertGreater(len(refs), 0)
        for r in refs:
            self.assertEqual(r, "true")

    def test_predicate_casts_instance_id_to_text(self):
        # instances.id is Integer; current_setting returns text.
        # The cast is explicit to avoid implicit-cast surprises.
        self.assertRegex(
            self.src,
            r"luciel_instance_id::text",
        )

    def test_downgrade_drops_policy(self):
        self.assertRegex(
            self.src,
            r"DROP\s+POLICY\s+IF\s+EXISTS\s+sessions_instance_isolation",
        )

    def test_downgrade_drops_policy_but_does_not_disable_rls(self):
        # Arc 9 C5.4 fix: downgrade MUST NOT disable RLS, because the
        # sibling Wall-1 policy sessions_tenant_isolation (C3.2e) is
        # still in force. Drop only this Wall-3 policy.
        disable_pattern = (
            r"ALTER\s+TABLE\s+sessions\s+DISABLE\s+ROW\s+LEVEL\s+SECURITY"
        )
        drop_pattern = (
            r"DROP\s+POLICY\s+IF\s+EXISTS\s+sessions_instance_isolation"
        )
        downgrade_block_match = re.search(
            r"def\s+downgrade.*", self.src, re.S
        )
        self.assertIsNotNone(downgrade_block_match)
        downgrade_block = downgrade_block_match.group(0)
        self.assertRegex(downgrade_block, drop_pattern)
        self.assertNotRegex(
            downgrade_block,
            disable_pattern,
            "C4.3d downgrade DISABLES RLS -- would neuter the Wall-1 "
            "sibling policy on sessions.",
        )


class TestC43DMigrationImports(unittest.TestCase):
    def test_module_imports_cleanly(self):
        from alembic.script import ScriptDirectory
        from alembic.config import Config
        cfg = Config(str(ALEMBIC_INI))
        sd = ScriptDirectory.from_config(cfg)
        rev = sd.get_revision("arc9_c4_3d_rls_instance_sessions")
        self.assertIsNotNone(rev)
        self.assertEqual(rev.down_revision, "arc9_c4_3c_rls_instance_memory_items")


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
