"""
Arc 9 C3.3 regression tests -- RLS migration for knowledge_embeddings.

CONTRACT GUARDED:
    knowledge_embeddings is the first NULL-permissive table in the
    Wall 1 Layer 2 rollout. Its policy MUST have an ASYMMETRIC
    USING / WITH CHECK pair to preserve the documented scope shape:

    READ-SIDE (USING):
      admin_id IS NULL OR admin_id = current_setting(...)

      Reads cross-tenant domain_knowledge rows AND own-tenant rows.

    WRITE-SIDE (WITH CHECK):
      (admin_id IS NULL AND current_setting() = 'platform')
      OR admin_id = current_setting(...)

      Regular admins can only write own-tenant rows. Domain-knowledge
      writes (admin_id NULL) require the 'platform' GUC sentinel.

THE BUG THIS GUARDS AGAINST:
    A future author copies the simple C3.1 policy template and
    forgets the NULL-permissive carveout -- the knowledge retriever
    breaks for every domain_knowledge query (~50% of retriever
    traffic). The bug would surface as silent retrieval-recall
    drops, not as a hard error -- the worst kind of regression.

WHY UNIT (not DB-backed):
    Per Arc 9 C3.1 convention -- shape tests catch text-level drift;
    behavioural tests against real Postgres land in C7 regression
    suite where infrastructure is in place.

RUN:
    python -m pytest tests/db/test_rls_c3_3_knowledge_embeddings.py -v
"""
from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path


MIGRATION_PATH = (
    Path(__file__).parent.parent.parent
    / "alembic" / "versions"
    / "arc9_c3_3_rls_knowledge_embeddings.py"
)


class TestC33MigrationShape(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.text = MIGRATION_PATH.read_text()
        cls.text_lower = cls.text.lower()

    def test_migration_file_exists(self):
        self.assertTrue(MIGRATION_PATH.exists())

    def test_chains_after_c32g(self):
        """C3.3 is the next step after C3.2g (scope_assignments)."""
        m = re.search(
            r'^down_revision\s*=\s*"([^"]+)"',
            self.text, re.MULTILINE,
        )
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "arc9_c3_2g_rls_scope_assignments")

    def test_revision_id(self):
        m = re.search(
            r'^revision\s*=\s*"([^"]+)"', self.text, re.MULTILINE
        )
        self.assertEqual(m.group(1), "arc9_c3_3_rls_knowledge_embeddings")

    def test_enables_rls_on_knowledge_embeddings(self):
        self.assertIn(
            "alter table knowledge_embeddings enable row level security",
            self.text_lower,
        )

    def test_using_has_null_permissive_carveout(self):
        """Read-side MUST permit admin_id IS NULL (domain_knowledge
        rows). Without this, the retriever loses ~50% of its surface."""
        # Find the USING block.
        m = re.search(
            r"using\s*\((.*?)\)\s*with\s+check",
            self.text_lower,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "USING ... WITH CHECK block not found")
        using_body = m.group(1)
        self.assertIn(
            "tenant_id is null",
            using_body,
            "USING MUST include `tenant_id IS NULL` so cross-tenant "
            "domain_knowledge rows remain visible.",
        )
        self.assertIn(
            "tenant_id = current_setting",
            using_body,
            "USING MUST also include the own-tenant match.",
        )

    def test_with_check_gates_null_writes_to_platform(self):
        """Write-side: domain_knowledge insert (admin_id NULL) MUST
        require the 'platform' GUC sentinel. Otherwise an ordinary
        admin could upload content as if it were platform-curated."""
        # Capture the WITH CHECK block to end of policy.
        m = re.search(
            r"with\s+check\s*\((.*?)\)\s*;",
            self.text_lower,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "WITH CHECK block not found")
        check_body = m.group(1)
        # The NULL branch MUST be gated by = 'platform'.
        # We look for both 'tenant_id is null' and a 'platform' token
        # appearing together in the same body.
        self.assertIn("tenant_id is null", check_body)
        self.assertIn(
            "'platform'",
            check_body,
            "WITH CHECK MUST gate admin_id IS NULL writes by "
            "current_setting() = 'platform'. Otherwise ordinary "
            "admins can write cross-tenant rows.",
        )
        # The own-tenant branch is also present.
        self.assertIn("tenant_id = current_setting", check_body)

    def test_using_and_with_check_are_asymmetric(self):
        """The whole point of this policy is that USING is more
        permissive than WITH CHECK (read NULL freely, write NULL
        only as platform). If a future author makes them symmetric
        again, the platform-write gate is lost."""
        m_using = re.search(
            r"using\s*\((.*?)\)\s*with\s+check",
            self.text_lower, re.DOTALL,
        )
        m_check = re.search(
            r"with\s+check\s*\((.*?)\)\s*;",
            self.text_lower, re.DOTALL,
        )
        self.assertIsNotNone(m_using)
        self.assertIsNotNone(m_check)
        # WITH CHECK must mention 'platform' specifically; USING must NOT
        # (USING is intentionally permissive on NULL without platform check).
        self.assertNotIn(
            "'platform'",
            m_using.group(1),
            "USING clause MUST NOT gate NULL by platform -- that would "
            "break the cross-tenant domain_knowledge read surface.",
        )
        self.assertIn("'platform'", m_check.group(1))

    def test_current_setting_uses_missing_ok_arg(self):
        self.assertRegex(
            self.text_lower,
            r"current_setting\(\s*'app\.admin_id'\s*,\s*true\s*\)",
        )

    def test_downgrade_drops_policy_and_disables_rls(self):
        m = re.search(
            r"def downgrade\(\) -> None:(.*?)(?=\Z|\ndef )",
            self.text, re.DOTALL,
        )
        self.assertIsNotNone(m)
        body = m.group(1).lower()
        drop_idx = body.find("drop policy")
        disable_idx = body.find("disable row level security")
        self.assertGreaterEqual(drop_idx, 0)
        self.assertGreaterEqual(disable_idx, 0)
        self.assertLess(drop_idx, disable_idx)
        self.assertIn("drop policy if exists", body)


class TestC33MigrationImports(unittest.TestCase):
    """Importability check -- catches Python-level defects."""

    def test_module_imports_cleanly(self):
        spec = importlib.util.spec_from_file_location(
            "_c33_test", str(MIGRATION_PATH)
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(
            module.revision, "arc9_c3_3_rls_knowledge_embeddings"
        )
        self.assertEqual(
            module.down_revision, "arc9_c3_2g_rls_scope_assignments"
        )
        self.assertTrue(callable(module.upgrade))
        self.assertTrue(callable(module.downgrade))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
