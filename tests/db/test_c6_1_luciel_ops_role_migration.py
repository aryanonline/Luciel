"""
Arc 9 C6.1 regression tests -- migration shape for luciel_ops role.

CONTRACT GUARDED:
    The Alembic migration ``arc9_c6_1_luciel_ops_role`` MUST:
      1. Land on the current head (arc9_c5_2_rls_instance_messages)
      2. CREATE ROLE luciel_ops with BYPASSRLS attribute
      3. Be NOINHERIT, NOCREATEDB, NOCREATEROLE, NOSUPERUSER,
         NOREPLICATION -- everything off except LOGIN + BYPASSRLS
      4. GRANT USAGE ON SCHEMA public TO luciel_ops
      5. GRANT SELECT (NEVER UPDATE/DELETE) on admin_audit_logs --
         forward-only audit-log immutability is the C6 doctrine
      6. GRANT SELECT + DELETE on the retention purge surface
         tables (matching admin_service.hard_delete_tenant_after_retention)
      7. NOT GRANT INSERT on ANY table -- ops removes data, never
         creates it
      8. NOT GRANT any privilege on auth-perimeter tables (admins,
         tenant_configs, users, user_invites, user_consents) --
         ops cannot delete a tenant's identity, only its data
      9. NOT GRANT USAGE on ANY sequence -- without sequences, even
         an accidentally-added INSERT grant cannot fabricate rows
     10. Idempotent CREATE ROLE (DO block with IF NOT EXISTS check)
     11. Provide a symmetric downgrade that REVOKEs then DROPs

THE BUGS THIS GUARDS AGAINST:
    - Author forgets BYPASSRLS -> ops role is fenced by RLS and
      retention purge silently fails to clean instance-scoped
      Wall-3 rows (the exact gap C6 exists to close).
    - Author grants UPDATE or DELETE on admin_audit_logs -> violates
      forward-only immutability doctrine. C6.2 layers a RESTRICTIVE
      policy on top as defense-in-depth, but the grant boundary
      is the first line.
    - Author grants on auth-perimeter tables -> blast-radius expands;
      a compromised ops credential can nuke tenant identities.
    - Author grants INSERT anywhere -> ops can fabricate rows
      including (especially) audit rows that break the hash chain.
    - Author grants sequence USAGE -> defense-in-depth against (4).
    - Author writes CREATE ROLE without IF NOT EXISTS -> migration
      cannot be re-applied after rollback in dev.

WHY UNIT (not DB-backed):
    The full DB-backed test of "ops role can DELETE despite RLS,
    luciel role cannot" requires a live Postgres test fixture and
    is the responsibility of C6.4 (separate test module). THIS
    test catches structural drift in the migration text itself
    -- which is where one-token bugs hide.

RUN:
    python -m pytest tests/db/test_c6_1_luciel_ops_role_migration.py -v
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


MIGRATION_PATH = (
    Path(__file__).parent.parent.parent
    / "alembic"
    / "versions"
    / "arc9_c6_1_luciel_ops_role.py"
)


# Auth-perimeter tables that MUST never appear in any grant in this
# migration. Identity-tier; retention deletes these through the
# regular luciel role under C4.4 scope-bound RLS, NOT through
# luciel_ops.
AUTH_PERIMETER_TABLES = (
    "admins",
    "tenant_configs",
    "users",
    "user_invites",
    "user_consents",
)


class TestC61MigrationShape(unittest.TestCase):
    """Structural assertions on the C6.1 migration's SQL text."""

    @classmethod
    def setUpClass(cls):
        cls.text = MIGRATION_PATH.read_text()
        cls.text_lower = cls.text.lower()
        # Import the migration module so we can inspect its grant
        # tuples directly -- much more robust than regex-matching the
        # source text which would also hit the docstring.
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_c6_1_mig", MIGRATION_PATH
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.mig = mod
        # Extract just the upgrade()/downgrade() bodies (no docstring)
        # for any regex test that should not hit the docstring.
        import inspect

        cls.upgrade_src = inspect.getsource(mod.upgrade)
        cls.downgrade_src = inspect.getsource(mod.downgrade)
        cls.code_src = cls.upgrade_src + "\n" + cls.downgrade_src
        cls.code_src_lower = cls.code_src.lower()

    def test_migration_file_exists(self):
        self.assertTrue(
            MIGRATION_PATH.exists(),
            f"Migration file missing: {MIGRATION_PATH}",
        )

    def test_revision_id_matches_filename(self):
        m = re.search(r'^revision\s*=\s*"([^"]+)"', self.text, re.MULTILINE)
        self.assertIsNotNone(m, "revision identifier missing")
        self.assertEqual(m.group(1), "arc9_c6_1_luciel_ops_role")

    def test_down_revision_points_at_current_head(self):
        """Lands on arc9_c5_2_rls_instance_messages -- the production
        head at C6.1 authoring time."""
        m = re.search(
            r'^down_revision\s*=\s*"([^"]+)"', self.text, re.MULTILINE
        )
        self.assertIsNotNone(m, "down_revision missing")
        self.assertEqual(m.group(1), "arc9_c5_2_rls_instance_messages")

    def test_create_role_is_idempotent(self):
        """CREATE ROLE wrapped in a DO block with IF NOT EXISTS check
        so re-running after a rollback doesn't explode."""
        self.assertIn("do $$", self.text_lower)
        self.assertIn("if not exists", self.text_lower)
        self.assertIn(
            "select 1 from pg_roles where rolname = 'luciel_ops'",
            self.text_lower,
        )

    def test_role_has_bypassrls(self):
        """BYPASSRLS is the WHOLE POINT of this role. Without it,
        retention is still fenced by Wall-1/Wall-3 RLS policies and
        the C6 gap stays open."""
        self.assertIn("bypassrls", self.text_lower)

    def test_role_has_login(self):
        self.assertIn("login", self.text_lower)

    def test_role_has_no_dangerous_attributes(self):
        """Defense-in-depth: every privilege escalation flag off."""
        for forbidden in (
            "nocreatedb",
            "nocreaterole",
            "nosuperuser",
            "noreplication",
            "noinherit",
        ):
            self.assertIn(
                forbidden,
                self.text_lower,
                f"Role attribute {forbidden!r} missing -- ops role "
                "must be locked down on every dimension except BYPASSRLS.",
            )

    def test_grants_schema_usage_on_public(self):
        self.assertRegex(
            self.text_lower,
            r"grant\s+usage\s+on\s+schema\s+public\s+to\s+luciel_ops",
        )

    def test_admin_audit_logs_is_select_only(self):
        """Forward-only audit immutability: ops can READ audit rows
        for forensics but never modify them. The C6.2 RESTRICTIVE
        policy is defense-in-depth on top of this grant boundary.

        Implementation check: admin_audit_logs must appear in
        SELECT_ONLY_TABLES and NOT in SELECT_DELETE_TABLES."""
        self.assertIn(
            "admin_audit_logs",
            self.mig.SELECT_ONLY_TABLES,
            "admin_audit_logs missing from SELECT_ONLY_TABLES tuple",
        )
        self.assertNotIn(
            "admin_audit_logs",
            self.mig.SELECT_DELETE_TABLES,
            "admin_audit_logs MUST NOT be in SELECT_DELETE_TABLES -- "
            "forward-only audit immutability doctrine (C6).",
        )
        # Defense-in-depth: also confirm upgrade() body never builds a
        # GRANT string that combines admin_audit_logs with UPDATE/DELETE/
        # INSERT verbs.
        forbidden_pattern = re.compile(
            r"grant[^;]*?\b(update|delete|insert)\b[^;]*?admin_audit_logs",
            re.IGNORECASE | re.DOTALL,
        )
        match = forbidden_pattern.search(self.upgrade_src)
        self.assertIsNone(
            match,
            f"upgrade() must not grant write privileges on "
            f"admin_audit_logs; found: {match.group(0) if match else None!r}",
        )

    def test_retention_surface_tables_have_delete_grants(self):
        """The whole retention purge surface must be in the
        SELECT_DELETE_TABLES tuple. Cross-checked against
        admin_service.hard_delete_tenant_after_retention 12-step
        chain (verified 2026-05-24)."""
        required_tables = (
            "sessions",
            "conversations",
            "identity_claims",
            "memory_items",
            "api_keys",
            "luciel_instances",
            "agents",
            "agent_configs",
        )
        for table in required_tables:
            self.assertIn(
                table,
                self.mig.SELECT_DELETE_TABLES,
                f"Retention-surface table {table!r} missing from "
                f"SELECT_DELETE_TABLES; would survive the purge under "
                f"luciel_ops's BYPASSRLS.",
            )
        # Also confirm upgrade() body actually emits a
        # SELECT, DELETE grant for each table in the tuple.
        pattern = re.compile(
            r"grant\s+select\s*,\s*delete\s+on\s+\{table\}\s+to\s+luciel_ops",
            re.IGNORECASE,
        )
        self.assertRegex(
            self.upgrade_src,
            pattern,
            "upgrade() must contain a loop emitting "
            "'GRANT SELECT, DELETE ON {table} TO luciel_ops;' "
            "-- the literal f-string template must use the verbs "
            "SELECT and DELETE in that order.",
        )

    def test_no_insert_grants_anywhere(self):
        """Ops removes data, never creates it. INSERT grants would
        let a compromised ops credential fabricate rows -- especially
        dangerous for audit rows where the hash chain is the integrity
        invariant."""
        # Look only inside upgrade()/downgrade() function bodies, and
        # only at non-comment lines (inline comments may legitimately
        # discuss why INSERT is deliberately omitted).
        non_comment_code = "\n".join(
            line for line in self.code_src.splitlines()
            if not line.lstrip().startswith("#")
        )
        forbidden_pattern = re.compile(
            r"grant[^;]*\binsert\b", re.IGNORECASE | re.DOTALL
        )
        match = forbidden_pattern.search(non_comment_code)
        self.assertIsNone(
            match,
            f"INSERT grant must not appear in upgrade()/downgrade(): "
            f"{match.group(0) if match else None!r}",
        )

    def test_no_sequence_grants(self):
        """Defense-in-depth against accidental INSERT: without
        sequence USAGE, an accidentally-added INSERT grant fails
        loudly with 'permission denied for sequence' instead of
        silently injecting data."""
        # Check only function bodies; docstring may discuss sequences
        # in the context of explaining the design choice.
        self.assertNotIn("on sequence", self.code_src_lower)
        self.assertNotIn("on all sequences", self.code_src_lower)

    def test_no_auth_perimeter_grants(self):
        """Identity-tier tables must never appear in any grant.
        Retention deletes admins/tenant_configs through the regular
        luciel role under C4.4 scope-bound RLS."""
        all_grant_tables = (
            self.mig.SELECT_ONLY_TABLES + self.mig.SELECT_DELETE_TABLES
        )
        for table in AUTH_PERIMETER_TABLES:
            self.assertNotIn(
                table,
                all_grant_tables,
                f"Auth-perimeter table {table!r} must not appear in "
                f"any luciel_ops grant tuple; blast radius must stay "
                f"narrow to non-identity tables.",
            )

    def test_no_messages_grant(self):
        """messages cascades from sessions via ON DELETE CASCADE
        per the retention design; a direct grant is unnecessary
        surface area."""
        all_grant_tables = (
            self.mig.SELECT_ONLY_TABLES + self.mig.SELECT_DELETE_TABLES
        )
        self.assertNotIn(
            "messages",
            all_grant_tables,
            "messages must not have a direct grant to luciel_ops "
            "(cascades from sessions via ON DELETE CASCADE).",
        )

    def test_downgrade_revokes_before_drop(self):
        """REVOKE must come before DROP ROLE in the downgrade --
        Postgres refuses to drop a role that still holds privileges."""
        # Find positions of first REVOKE and first DROP ROLE in
        # downgrade(). Slicing on the downgrade() definition.
        downgrade_split = re.split(
            r"def\s+downgrade\s*\(\s*\)\s*->\s*None\s*:",
            self.text,
        )
        self.assertEqual(
            len(downgrade_split),
            2,
            "Could not locate downgrade() function in migration",
        )
        downgrade_body = downgrade_split[1].lower()
        revoke_pos = downgrade_body.find("revoke")
        drop_pos = downgrade_body.find("drop role")
        self.assertGreaterEqual(revoke_pos, 0, "downgrade missing REVOKE")
        self.assertGreaterEqual(drop_pos, 0, "downgrade missing DROP ROLE")
        self.assertLess(
            revoke_pos,
            drop_pos,
            "REVOKE must come before DROP ROLE in downgrade",
        )

    def test_downgrade_drop_role_is_idempotent(self):
        """DROP ROLE wrapped in IF EXISTS check so partially-applied
        rollbacks can re-run cleanly."""
        downgrade_body = re.split(
            r"def\s+downgrade\s*\(\s*\)\s*->\s*None\s*:", self.text
        )[1].lower()
        self.assertIn("if exists", downgrade_body)


if __name__ == "__main__":
    unittest.main()
