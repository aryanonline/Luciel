"""
Arc 12 EX3 regression tests — drop ``api_keys.domain_id`` and
``api_keys.agent_id``.

CONTRACT GUARDED:
    The Arc 12 excision plan (``arc12_specs/02_EXCISION_PLAN.md`` §EX3)
    requires the legacy ``api_keys.domain_id`` and ``api_keys.agent_id``
    columns to be physically dropped once EX1 has stopped writing
    meaningful values, EX1c has dropped them from public Pydantic
    projections, and EX2 has re-sealed every live RLS policy off
    ``admin_id`` (+ ``luciel_instance_id``).

    These tests pin:

      1. The forward migration exists, has the expected revision id,
         and chains off the prior EX3 head
         (``arc12_ex3_drop_memory_agent_id``).
      2. The migration drops BOTH columns (no index drops needed — the
         create migration ``edb185277456_add_api_keys_table`` and the
         agent_id add migration ``8b896ecd5881_add_agent_id_to_
         api_keys_and_traces`` never created an index on either column).
      3. The downgrade re-adds both columns as nullable ``String(100)``.
      4. The SQLAlchemy ``ApiKey`` model no longer declares either
         attribute (the v2 projection shape: admin_id + luciel_instance_id
         only for tenant/instance binding).

WHY UNIT (not DB-backed):
    Per the Arc 9 C3 convention — shape tests catch text-level drift
    on the migration DDL and the chain pointers; live-DB column
    behavior rides on the existing ``test_rls_c3_4_api_keys.py`` and
    ``test_rls_c4_3a_instance_api_keys.py`` suites.

RUN:
    python -m pytest tests/db/test_arc12_ex3_drop_api_key_agent_domain.py -v
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from sqlalchemy import inspect

from app.models.api_key import ApiKey


VERSIONS_DIR = (
    Path(__file__).parent.parent.parent / "alembic" / "versions"
)
MIGRATION_PATH = (
    VERSIONS_DIR / "arc12_ex3_drop_api_key_agent_domain.py"
)


class TestEx3ApiKeyMigrationShape(unittest.TestCase):

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
            m.group(1), "arc12_ex3_drop_api_key_agent_domain"
        )

    def test_chains_off_prior_ex3_head(self):
        """EX3 (api_keys) chains off the prior EX3 head
        ``arc12_ex3_drop_memory_agent_id``, which itself chains off
        ``arc12_ex3_drop_trace_agent_domain``."""
        m = re.search(
            r'^down_revision\s*=\s*"([^"]+)"',
            self.text, re.MULTILINE,
        )
        self.assertIsNotNone(m)
        self.assertEqual(
            m.group(1), "arc12_ex3_drop_memory_agent_id"
        )

    def test_upgrade_drops_both_columns(self):
        self.assertRegex(
            self.text,
            r'op\.drop_column\(\s*"api_keys"\s*,\s*"agent_id"\s*\)',
        )
        self.assertRegex(
            self.text,
            r'op\.drop_column\(\s*"api_keys"\s*,\s*"domain_id"\s*\)',
        )

    def test_upgrade_has_no_index_drops(self):
        """Neither column was indexed; the migration must not pretend
        to drop indexes that don't exist."""
        self.assertNotIn("drop_index", self.text)

    def test_downgrade_readds_both_columns(self):
        # domain_id re-added as nullable String(100).
        self.assertRegex(
            self.text,
            r'add_column\(\s*"api_keys"\s*,\s*'
            r'sa\.Column\(\s*"domain_id"\s*,\s*'
            r'sa\.String\(\s*length\s*=\s*100\s*\)\s*,\s*'
            r'nullable\s*=\s*True',
        )
        # agent_id re-added as nullable String(100).
        self.assertRegex(
            self.text,
            r'add_column\(\s*"api_keys"\s*,\s*'
            r'sa\.Column\(\s*"agent_id"\s*,\s*'
            r'sa\.String\(\s*length\s*=\s*100\s*\)\s*,\s*'
            r'nullable\s*=\s*True',
        )


class TestApiKeyModelShape(unittest.TestCase):
    """The ApiKey ORM model must no longer declare domain_id/agent_id.

    These are the v2 projection asserts: ApiKey binds to admin_id (+
    optional luciel_instance_id) only. The two legacy scoping columns
    were dropped in EX3 and removing them from the model is what makes
    SQLAlchemy stop emitting SELECTs that reference the now-absent DB
    columns."""

    def test_model_has_no_domain_id_attribute(self):
        mapper = inspect(ApiKey)
        attrs = {c.key for c in mapper.attrs}
        self.assertNotIn("domain_id", attrs)

    def test_model_has_no_agent_id_attribute(self):
        mapper = inspect(ApiKey)
        attrs = {c.key for c in mapper.attrs}
        self.assertNotIn("agent_id", attrs)

    def test_model_retains_v2_projection_fields(self):
        """The v2 binding surface still has admin_id +
        luciel_instance_id (the canonical tenant/instance pin)."""
        mapper = inspect(ApiKey)
        attrs = {c.key for c in mapper.attrs}
        self.assertIn("admin_id", attrs)
        self.assertIn("luciel_instance_id", attrs)


if __name__ == "__main__":
    unittest.main()
