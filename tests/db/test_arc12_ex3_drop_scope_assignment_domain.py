"""
Arc 12 EX3 regression tests — drop ``scope_assignments.domain_id``.

CONTRACT GUARDED:
    The Arc 12 excision plan requires the legacy
    ``scope_assignments.domain_id`` column (the last non-audit-chain
    domain_id in v2) to be physically dropped once EX1 has stopped
    reading/writing it at the application layer, EX1c has dropped it
    from the public Pydantic projection, and EX2 has confirmed no
    live RLS policy references it.

    These tests pin:

      1. The forward migration exists with the expected revision id
         and chains off the prior EX3 head
         (``arc12_ex3_drop_user_invite_domain``).
      2. The duplicate-assignment partial index is RE-CREATED on the
         v2 ``(user_id, admin_id, role)`` shape — duplicate-assignment
         protection MUST survive this migration.
      3. The migration drops the column AFTER dropping the wide
         partial index.
      4. The downgrade re-adds ``domain_id`` as nullable ``String(100)``
         and rebuilds the original wide partial index.
      5. The migration redefines ``public.arc9_c22_bootstrap_identity``
         without ``domain_id`` in its RETURNS shape or SELECT lists.
      6. The SQLAlchemy ``ScopeAssignment`` model no longer declares
         ``domain_id`` and the ``IdentityBootstrap._COLUMNS`` tuple
         no longer carries it either.
      7. ``ScopePolicy._resolve_role_on_instance`` does NOT key on
         ``domain_id`` — the v2 role resolution is
         ``(admin_id, instance.id, role)``.

WHY UNIT (not DB-backed):
    Per the Arc 9 C3 convention — shape tests catch text-level drift
    on migration DDL and chain pointers; the live-DB column behaviour
    rides on the existing EX2 RLS suite.

RUN:
    python -m pytest tests/db/test_arc12_ex3_drop_scope_assignment_domain.py -v
"""
from __future__ import annotations

import inspect as py_inspect
import re
import unittest
from pathlib import Path

from sqlalchemy import inspect as sa_inspect

from app.models.scope_assignment import ScopeAssignment


VERSIONS_DIR = (
    Path(__file__).parent.parent.parent / "alembic" / "versions"
)
MIGRATION_PATH = (
    VERSIONS_DIR / "arc12_ex3_drop_scope_assignment_domain.py"
)


class TestEx3ScopeAssignmentMigrationShape(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.text = MIGRATION_PATH.read_text()

    def test_migration_file_exists(self):
        self.assertTrue(MIGRATION_PATH.exists())

    def test_revision_id(self):
        m = re.search(
            r'^revision\s*=\s*"([^"]+)"', self.text, re.MULTILINE
        )
        self.assertIsNotNone(m)
        self.assertEqual(
            m.group(1), "arc12_ex3_drop_scope_assignment_domain"
        )

    def test_chains_off_prior_ex3_head(self):
        m = re.search(
            r'^down_revision\s*=\s*"([^"]+)"',
            self.text, re.MULTILINE,
        )
        self.assertIsNotNone(m)
        self.assertEqual(
            m.group(1), "arc12_ex3_drop_user_invite_domain"
        )

    def test_upgrade_drops_wide_partial_index_before_column(self):
        """The (user_id, admin_id, domain_id, role) partial index must
        be dropped before the column drop. The migration emits the SQL
        as adjacent string-literal fragments; strip the Python quote
        glue before searching so the test is insensitive to formatting."""
        # Drop the Python adjacent-string-literal glue (`" "` / `"\n    "`).
        glued = re.sub(r'"\s*"', "", self.text)
        drop_idx = glued.find(
            "DROP INDEX IF EXISTS "
            "public.ix_scope_assignments_user_tenant_domain_role_active"
        )
        drop_col = glued.find(
            'op.drop_column("scope_assignments", "domain_id")'
        )
        self.assertGreater(drop_idx, -1, "missing wide index drop")
        self.assertGreater(drop_col, -1, "missing column drop")
        self.assertLess(drop_idx, drop_col)

    def test_upgrade_recreates_v2_duplicate_assignment_index(self):
        """The duplicate-assignment guard must survive — same name,
        narrower (user_id, admin_id, role) shape."""
        glued = re.sub(r'"\s*"', "", self.text)
        self.assertIn(
            "CREATE INDEX ix_scope_assignments_user_tenant_domain_role_active "
            "ON public.scope_assignments "
            "(user_id, admin_id, role) "
            "WHERE ended_at IS NULL",
            glued,
            "v2 duplicate-assignment partial index must be recreated "
            "on (user_id, admin_id, role) WHERE ended_at IS NULL",
        )

    def test_upgrade_redefines_arc9_c22_without_domain_id(self):
        """The arc9_c22 bootstrap SECDEF function must be redefined
        without domain_id in its RETURNS shape or its body SELECTs."""
        # The v2 CREATE OR REPLACE block must NOT mention sa.domain_id
        # or have domain_id in its RETURNS TABLE shape.
        v2_block = re.search(
            r"_CREATE_FN_V2_SQL\s*=\s*\"\"\"(.*?)\"\"\"",
            self.text, re.DOTALL,
        )
        self.assertIsNotNone(
            v2_block, "v2 SECDEF function body missing"
        )
        body = v2_block.group(1)
        self.assertNotIn("domain_id", body)
        self.assertNotIn("sa.domain_id", body)

    def test_upgrade_drops_column(self):
        self.assertRegex(
            self.text,
            r'op\.drop_column\(\s*"scope_assignments"\s*,\s*"domain_id"\s*\)',
        )

    def test_downgrade_readds_column_as_nullable(self):
        # domain_id MUST come back as nullable=True because the
        # downgrade cannot know original values.
        self.assertRegex(
            self.text,
            r'add_column\(\s*\n?\s*"scope_assignments"\s*,\s*\n?\s*'
            r'sa\.Column\(\s*\n?\s*"domain_id"\s*,\s*\n?\s*'
            r'sa\.String\(\s*length\s*=\s*100\s*\)\s*,\s*\n?\s*'
            r'nullable\s*=\s*True',
        )

    def test_downgrade_recreates_wide_partial_index(self):
        glued = re.sub(r'"\s*"', "", self.text)
        self.assertIn(
            "CREATE INDEX ix_scope_assignments_user_tenant_domain_role_active "
            "ON public.scope_assignments "
            "(user_id, admin_id, domain_id, role) "
            "WHERE ended_at IS NULL",
            glued,
            "downgrade must restore the wide partial index "
            "(user_id, admin_id, domain_id, role)",
        )

    def test_downgrade_restores_wide_arc9_c22(self):
        """The downgrade must restore the arc9_c22 function shape
        that includes domain_id in RETURNS TABLE and SELECT lists."""
        v1_block = re.search(
            r"_CREATE_FN_V1_SQL\s*=\s*\"\"\"(.*?)\"\"\"",
            self.text, re.DOTALL,
        )
        self.assertIsNotNone(
            v1_block, "v1 (wide) SECDEF function body missing"
        )
        body = v1_block.group(1)
        # domain_id must reappear in the wide shape's RETURNS and body.
        self.assertIn("domain_id", body)
        self.assertIn("sa.domain_id", body)


class TestScopeAssignmentModelShape(unittest.TestCase):
    """The ScopeAssignment ORM must no longer declare domain_id."""

    def test_model_has_no_domain_id_attribute(self):
        mapper = sa_inspect(ScopeAssignment)
        attrs = {c.key for c in mapper.attrs}
        self.assertNotIn("domain_id", attrs)

    def test_model_retains_v2_projection_fields(self):
        mapper = sa_inspect(ScopeAssignment)
        attrs = {c.key for c in mapper.attrs}
        self.assertIn("admin_id", attrs)
        self.assertIn("user_id", attrs)
        self.assertIn("role", attrs)


class TestIdentityBootstrapShape(unittest.TestCase):
    """The IdentityBootstrap _COLUMNS tuple + SELECT must no longer
    reference domain_id post-EX3."""

    def test_columns_tuple_drops_domain_id(self):
        from app.identity.bootstrap import IdentityBootstrap

        self.assertNotIn("domain_id", IdentityBootstrap._COLUMNS)

    def test_resolve_select_omits_domain_id(self):
        from app.identity.bootstrap import IdentityBootstrap

        source = py_inspect.getsource(IdentityBootstrap.resolve)
        # The SELECT list must NOT thread domain_id from the SECDEF.
        self.assertNotIn("domain_id", source)


class TestScopePolicyDoesNotDependOnDomainId(unittest.TestCase):
    """``ScopePolicy._resolve_role_on_instance`` and
    ``enforce_role_on_instance`` must key the v2 role resolution on
    ``admin_id`` + ``instance.id`` + ``role`` — never on ``domain_id``."""

    def test_resolve_role_on_instance_source_has_no_domain_id(self):
        from app.policy import scope as scope_mod

        src = py_inspect.getsource(scope_mod.ScopePolicy._resolve_role_on_instance)
        self.assertNotIn("domain_id", src)

    def test_enforce_role_on_instance_source_has_no_domain_id(self):
        from app.policy import scope as scope_mod

        src = py_inspect.getsource(scope_mod.ScopePolicy.enforce_role_on_instance)
        self.assertNotIn("domain_id", src)


class TestSentinelBridgesRemoved(unittest.TestCase):
    """The ``_DOMAIN_COLLAPSE_SENTINEL`` bridges that satisfied the
    NOT-NULL must be gone with the column."""

    def test_scope_assignment_service_has_no_sentinel(self):
        import app.services.scope_assignment_service as svc

        self.assertFalse(hasattr(svc, "_DOMAIN_COLLAPSE_SENTINEL"))

    def test_tier_provisioning_service_has_no_sentinel(self):
        import app.services.tier_provisioning_service as svc

        self.assertFalse(hasattr(svc, "_DOMAIN_COLLAPSE_SENTINEL"))


if __name__ == "__main__":
    unittest.main()
