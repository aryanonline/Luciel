"""
Arc 9 C6.2 regression tests -- migration shape for admin_audit_logs
forward-only immutability policies.

CONTRACT GUARDED:
    The Alembic migration ``arc9_c6_2_admin_audit_immutability`` MUST:
      1. Land on arc9_c6_1_luciel_ops_role (C6.1 must precede C6.2)
      2. Create exactly TWO policies on admin_audit_logs:
           - admin_audit_logs_no_update  (FOR UPDATE)
           - admin_audit_logs_no_delete  (FOR DELETE)
      3. Both policies MUST be AS RESTRICTIVE
         (PERMISSIVE would BROADEN access -- the exact opposite of
         our goal)
      4. Both policies USING predicate compares current_user to
         the literal string 'luciel_ops'
      5. UPDATE policy MUST also have a WITH CHECK clause (UPDATE
         gates both read-side via USING and write-side via WITH CHECK)
      6. DELETE policy MUST NOT have a WITH CHECK clause (Postgres
         doesn't support it for DELETE; including one is a syntax
         error)
      7. NO policy with FOR ALL, FOR SELECT, or FOR INSERT (would
         break the read path and the chained-write path)
      8. NEVER touches the existing C3.1/C4.3f permissive policies
      9. Provide a working downgrade that drops both policies

THE BUGS THIS GUARDS AGAINST:
    - Author writes AS PERMISSIVE instead of AS RESTRICTIVE
      -> policies are OR'd with the existing permissive policies,
      so they BROADEN access. Catastrophic: any caller now passes
      the audit-log policy check.
    - Author writes FOR ALL instead of FOR UPDATE/DELETE
      -> SELECT and INSERT are also gated by USING/WITH CHECK,
      breaking the write path (audit chain INSERT fails for the
      regular luciel role) AND the read path (forensics queries
      return empty).
    - Author forgets WITH CHECK on the UPDATE policy
      -> UPDATEs can change admin_id to 'luciel_ops' value
      mid-flight if combined with a clever crafted row, or just
      fail unpredictably.
    - Author adds WITH CHECK to the DELETE policy
      -> Postgres syntax error at migration apply time, partial
      transaction rolls back.
    - Author uses session_user instead of current_user
      -> Breaks future SET ROLE-style ops scenarios. Subtle.
    - Author hardcodes a different role name (typo on luciel_ops)
      -> Even legitimate ops calls fail; retention purge can't
      delete admin audit logs (which we don't want anyway, but
      we don't want the wrong failure mode either).

WHY UNIT (not DB-backed):
    Full DB-backed validation (does luciel role get refused?
    does luciel_ops get through? does the chain integrity check
    catch tampering?) lives in C6.4. This test is the
    cheap-and-fast structural guard that runs in every PR.

RUN:
    python -m pytest tests/db/test_c6_2_admin_audit_immutability_migration.py -v
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


MIGRATION_PATH = (
    Path(__file__).parent.parent.parent
    / "alembic"
    / "versions"
    / "arc9_c6_2_admin_audit_immutability.py"
)


class TestC62MigrationShape(unittest.TestCase):
    """Structural assertions on the C6.2 migration's SQL text."""

    @classmethod
    def setUpClass(cls):
        cls.text = MIGRATION_PATH.read_text()
        cls.text_lower = cls.text.lower()
        import importlib.util
        import inspect

        spec = importlib.util.spec_from_file_location(
            "_c6_2_mig", MIGRATION_PATH
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.mig = mod
        # inspect.getsource returns the raw source text, but the
        # migration uses f-strings with {UPDATE_POLICY_NAME} /
        # {DELETE_POLICY_NAME} substitutions. We substitute the
        # constants here so the regex tests can match the policy
        # names as they actually appear at runtime.
        upgrade_raw = inspect.getsource(mod.upgrade)
        downgrade_raw = inspect.getsource(mod.downgrade)
        cls.upgrade_src = upgrade_raw.replace(
            "{UPDATE_POLICY_NAME}", mod.UPDATE_POLICY_NAME
        ).replace(
            "{DELETE_POLICY_NAME}", mod.DELETE_POLICY_NAME
        )
        cls.downgrade_src = downgrade_raw.replace(
            "{UPDATE_POLICY_NAME}", mod.UPDATE_POLICY_NAME
        ).replace(
            "{DELETE_POLICY_NAME}", mod.DELETE_POLICY_NAME
        )
        cls.code_src = cls.upgrade_src + "\n" + cls.downgrade_src
        cls.code_src_lower = cls.code_src.lower()

    def test_migration_file_exists(self):
        self.assertTrue(
            MIGRATION_PATH.exists(),
            f"Migration file missing: {MIGRATION_PATH}",
        )

    def test_revision_id_matches_filename(self):
        m = re.search(r'^revision\s*=\s*"([^"]+)"', self.text, re.MULTILINE)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "arc9_c6_2_admin_audit_immutability")

    def test_down_revision_points_at_c6_1(self):
        """C6.2 must land on top of C6.1 -- the policies reference
        luciel_ops which only exists after C6.1."""
        m = re.search(
            r'^down_revision\s*=\s*"([^"]+)"', self.text, re.MULTILINE
        )
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "arc9_c6_1_luciel_ops_role")

    def test_creates_update_policy(self):
        pattern = re.compile(
            r"create\s+policy\s+admin_audit_logs_no_update\s+"
            r"on\s+admin_audit_logs",
            re.IGNORECASE | re.DOTALL,
        )
        self.assertRegex(
            self.upgrade_src,
            pattern,
            "UPDATE-blocking policy not found in upgrade()",
        )

    def test_creates_delete_policy(self):
        pattern = re.compile(
            r"create\s+policy\s+admin_audit_logs_no_delete\s+"
            r"on\s+admin_audit_logs",
            re.IGNORECASE | re.DOTALL,
        )
        self.assertRegex(
            self.upgrade_src,
            pattern,
            "DELETE-blocking policy not found in upgrade()",
        )

    def test_both_policies_are_restrictive(self):
        """PERMISSIVE would BROADEN access via OR-combination with
        existing C3.1 and C4.3f policies -- catastrophic regression."""
        # Find every CREATE POLICY block in upgrade() and verify each
        # has AS RESTRICTIVE.
        policy_blocks = re.findall(
            r"create\s+policy\s+\w+\s+on\s+\w+(.*?)(?=create\s+policy|\Z)",
            self.upgrade_src,
            re.IGNORECASE | re.DOTALL,
        )
        self.assertEqual(
            len(policy_blocks),
            2,
            f"Expected exactly 2 CREATE POLICY blocks, found "
            f"{len(policy_blocks)}",
        )
        for i, block in enumerate(policy_blocks):
            self.assertRegex(
                block,
                re.compile(r"as\s+restrictive", re.IGNORECASE),
                f"Policy block {i} is not AS RESTRICTIVE: {block[:200]!r}",
            )
            # Also confirm it's NOT PERMISSIVE.
            self.assertNotRegex(
                block,
                re.compile(r"as\s+permissive", re.IGNORECASE),
                f"Policy block {i} must not be PERMISSIVE",
            )

    def test_update_policy_is_for_update(self):
        block = re.search(
            r"create\s+policy\s+admin_audit_logs_no_update(.*?);",
            self.upgrade_src,
            re.IGNORECASE | re.DOTALL,
        )
        self.assertIsNotNone(block)
        self.assertRegex(
            block.group(0),
            re.compile(r"for\s+update\b", re.IGNORECASE),
            "UPDATE policy must use FOR UPDATE clause",
        )

    def test_delete_policy_is_for_delete(self):
        block = re.search(
            r"create\s+policy\s+admin_audit_logs_no_delete(.*?);",
            self.upgrade_src,
            re.IGNORECASE | re.DOTALL,
        )
        self.assertIsNotNone(block)
        self.assertRegex(
            block.group(0),
            re.compile(r"for\s+delete\b", re.IGNORECASE),
            "DELETE policy must use FOR DELETE clause",
        )

    def test_no_for_all_for_select_for_insert(self):
        """FOR ALL would gate SELECT (forensic reads break) and
        INSERT (audit chain INSERTs from luciel break). Catastrophic."""
        for forbidden_clause in ("for\\s+all", "for\\s+select", "for\\s+insert"):
            pattern = re.compile(forbidden_clause, re.IGNORECASE)
            self.assertNotRegex(
                self.upgrade_src,
                pattern,
                f"Policies must not use {forbidden_clause!r} -- would "
                f"break read path and/or audit-chain INSERT.",
            )

    def test_uses_current_user_not_session_user(self):
        """current_user tracks effective identity (post-SET ROLE);
        session_user is the connection identity. Always prefer
        current_user for policy checks."""
        self.assertIn("current_user", self.upgrade_src)
        self.assertNotIn("session_user", self.upgrade_src)

    def test_predicates_compare_to_luciel_ops(self):
        """The whole point is to allow ONLY luciel_ops. Typo on the
        role name (e.g. 'luciel_op' or 'luciel_ops_role') would
        silently disable the escape hatch."""
        # Two policy blocks; each must contain 'luciel_ops' inside
        # a USING clause.
        for policy_name in (
            "admin_audit_logs_no_update",
            "admin_audit_logs_no_delete",
        ):
            block = re.search(
                rf"create\s+policy\s+{policy_name}(.*?);",
                self.upgrade_src,
                re.IGNORECASE | re.DOTALL,
            )
            self.assertIsNotNone(block, f"{policy_name} block not found")
            using_match = re.search(
                r"using\s*\(([^)]*)\)",
                block.group(0),
                re.IGNORECASE | re.DOTALL,
            )
            self.assertIsNotNone(
                using_match,
                f"{policy_name} missing USING clause",
            )
            self.assertIn(
                "'luciel_ops'",
                using_match.group(1),
                f"{policy_name} USING clause must compare to literal "
                f"'luciel_ops'; found: {using_match.group(1)!r}",
            )

    def test_update_policy_has_with_check(self):
        """UPDATE policies need WITH CHECK to gate the write side --
        without it, an UPDATE that passes USING (read) could write
        a row that would not have passed the policy on insert."""
        block = re.search(
            r"create\s+policy\s+admin_audit_logs_no_update(.*?);",
            self.upgrade_src,
            re.IGNORECASE | re.DOTALL,
        )
        self.assertIsNotNone(block)
        self.assertRegex(
            block.group(0),
            re.compile(r"with\s+check", re.IGNORECASE),
            "UPDATE policy must have WITH CHECK clause",
        )

    def test_delete_policy_has_no_with_check(self):
        """Postgres does NOT support WITH CHECK on DELETE policies --
        including it is a syntax error at apply time."""
        block = re.search(
            r"create\s+policy\s+admin_audit_logs_no_delete(.*?);",
            self.upgrade_src,
            re.IGNORECASE | re.DOTALL,
        )
        self.assertIsNotNone(block)
        self.assertNotRegex(
            block.group(0),
            re.compile(r"with\s+check", re.IGNORECASE),
            "DELETE policy must NOT have WITH CHECK (Postgres syntax error)",
        )

    def test_does_not_touch_existing_permissive_policies(self):
        """C3.1 (admin_audit_logs_tenant_isolation) and C4.3f
        (admin_audit_logs_instance_isolation) must remain untouched.
        Dropping or altering them in this migration would reopen
        the Wall-1/Wall-3 fence."""
        for existing in (
            "admin_audit_logs_tenant_isolation",
            "admin_audit_logs_instance_isolation",
        ):
            self.assertNotIn(
                existing,
                self.code_src,
                f"This migration must not reference existing policy "
                f"{existing!r}; that would risk altering it.",
            )

    def test_downgrade_drops_both_policies(self):
        """Symmetric teardown -- both policies must be dropped."""
        for policy_name in (
            "admin_audit_logs_no_update",
            "admin_audit_logs_no_delete",
        ):
            self.assertIn(
                policy_name,
                self.downgrade_src,
                f"{policy_name} not dropped in downgrade()",
            )
        # And both must use DROP POLICY ... ON admin_audit_logs.
        self.assertEqual(
            len(re.findall(
                r"drop\s+policy", self.downgrade_src, re.IGNORECASE
            )),
            2,
            "Expected exactly 2 DROP POLICY statements in downgrade()",
        )

    def test_downgrade_uses_if_exists(self):
        """IF EXISTS makes partial-rollback re-runs safe."""
        drop_lines = re.findall(
            r"drop\s+policy[^;]+;",
            self.downgrade_src,
            re.IGNORECASE,
        )
        for line in drop_lines:
            self.assertRegex(
                line,
                re.compile(r"if\s+exists", re.IGNORECASE),
                f"DROP POLICY missing IF EXISTS: {line!r}",
            )


if __name__ == "__main__":
    unittest.main()
