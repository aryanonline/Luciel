"""
Arc 12 EX3 regression tests — drop ``sessions.domain_id`` and
``sessions.agent_id``.

CONTRACT GUARDED:
    The Arc 12 excision plan (``arc12_specs/02_EXCISION_PLAN.md`` §EX3)
    requires the legacy ``sessions.domain_id`` and ``sessions.agent_id``
    columns to be physically dropped once EX1 has stopped reading or
    writing them at the application layer, EX1c has dropped them from
    public Pydantic projections, and EX2 has re-sealed every live RLS
    policy off ``admin_id`` (+ ``luciel_instance_id``).

    These tests pin:

      1. The forward migration exists, has the expected revision id,
         and chains off the prior EX3 head
         (``arc12_ex3_drop_api_key_agent_domain``).
      2. The migration drops BOTH indexes (``ix_sessions_agent_id`` and
         ``ix_sessions_domain_id``) BEFORE the column drops.
      3. The migration drops BOTH columns.
      4. The downgrade re-adds both columns as nullable ``String(100)``
         with the matching indexes. ``domain_id`` is re-added as
         nullable (NOT the pre-EX3 NOT NULL) because the downgrade
         cannot know the original values; existing rows would fail a
         NOT NULL re-add.
      5. The SQLAlchemy ``SessionModel`` no longer declares either
         attribute (the v2 projection: admin_id + luciel_instance_id
         scope the session per Walls 3/4).

WHY UNIT (not DB-backed):
    Per the Arc 9 C3 convention — shape tests catch text-level drift
    on the migration DDL and the chain pointers; live-DB column
    behaviour rides on the existing ``test_rls_arc12_ex2_drop_agent_
    domain_refs.py`` suite plus the Wall-3 RLS instance-session tests.

RUN:
    python -m pytest tests/db/test_arc12_ex3_drop_session_agent_domain.py -v
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from sqlalchemy import inspect

from app.models.session import SessionModel


VERSIONS_DIR = (
    Path(__file__).parent.parent.parent / "app" / "migrations" / "versions"
)
MIGRATION_PATH = (
    VERSIONS_DIR / "arc12_ex3_drop_session_agent_domain.py"
)


class TestEx3SessionMigrationShape(unittest.TestCase):

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
            m.group(1), "arc12_ex3_drop_session_agent_domain"
        )

    def test_chains_off_prior_ex3_head(self):
        """EX3 (sessions) chains off the prior EX3 head
        ``arc12_ex3_drop_api_key_agent_domain``."""
        m = re.search(
            r'^down_revision\s*=\s*"([^"]+)"',
            self.text, re.MULTILINE,
        )
        self.assertIsNotNone(m)
        self.assertEqual(
            m.group(1), "arc12_ex3_drop_api_key_agent_domain"
        )

    def test_upgrade_drops_both_indexes_before_columns(self):
        """Indexes must drop BEFORE columns so PG doesn't have to cascade
        across the index when the column goes away."""
        drop_idx_agent = self.text.find('drop_index(\n        op.f("ix_sessions_agent_id")')
        drop_idx_domain = self.text.find('drop_index(\n        op.f("ix_sessions_domain_id")')
        drop_col_agent = self.text.find('drop_column("sessions", "agent_id")')
        drop_col_domain = self.text.find('drop_column("sessions", "domain_id")')
        for pos, label in [
            (drop_idx_agent, "drop_index agent_id"),
            (drop_idx_domain, "drop_index domain_id"),
            (drop_col_agent, "drop_column agent_id"),
            (drop_col_domain, "drop_column domain_id"),
        ]:
            self.assertGreater(pos, -1, f"missing: {label}")
        self.assertLess(drop_idx_agent, drop_col_agent)
        self.assertLess(drop_idx_domain, drop_col_domain)

    def test_upgrade_drops_both_columns(self):
        self.assertRegex(
            self.text,
            r'op\.drop_column\(\s*"sessions"\s*,\s*"agent_id"\s*\)',
        )
        self.assertRegex(
            self.text,
            r'op\.drop_column\(\s*"sessions"\s*,\s*"domain_id"\s*\)',
        )

    def test_downgrade_readds_both_columns_as_nullable(self):
        # domain_id MUST come back as nullable=True (not the pre-EX3
        # NOT NULL) because the downgrade cannot know original values.
        self.assertRegex(
            self.text,
            r'add_column\(\s*"sessions"\s*,\s*'
            r'sa\.Column\(\s*"domain_id"\s*,\s*'
            r'sa\.String\(\s*length\s*=\s*100\s*\)\s*,\s*'
            r'nullable\s*=\s*True',
        )
        # agent_id re-added as nullable String(100).
        self.assertRegex(
            self.text,
            r'add_column\(\s*"sessions"\s*,\s*'
            r'sa\.Column\(\s*"agent_id"\s*,\s*'
            r'sa\.String\(\s*length\s*=\s*100\s*\)\s*,\s*'
            r'nullable\s*=\s*True',
        )

    def test_downgrade_recreates_both_indexes(self):
        self.assertRegex(
            self.text,
            r'create_index\(\s*op\.f\(\s*"ix_sessions_domain_id"\s*\)',
        )
        self.assertRegex(
            self.text,
            r'create_index\(\s*op\.f\(\s*"ix_sessions_agent_id"\s*\)',
        )


class TestSessionModelShape(unittest.TestCase):
    """The SessionModel ORM must no longer declare domain_id/agent_id.

    The v2 session scope is (admin_id, luciel_instance_id, session_id)
    per Walls 3/4; removing the attrs is what stops SQLAlchemy from
    emitting SELECTs that reference the now-absent DB columns."""

    def test_model_has_no_domain_id_attribute(self):
        mapper = inspect(SessionModel)
        attrs = {c.key for c in mapper.attrs}
        self.assertNotIn("domain_id", attrs)

    def test_model_has_no_agent_id_attribute(self):
        mapper = inspect(SessionModel)
        attrs = {c.key for c in mapper.attrs}
        self.assertNotIn("agent_id", attrs)

    def test_model_retains_v2_projection_fields(self):
        """The v2 session row still has admin_id +
        luciel_instance_id (the canonical Walls 3/4 pin)."""
        mapper = inspect(SessionModel)
        attrs = {c.key for c in mapper.attrs}
        self.assertIn("admin_id", attrs)
        self.assertIn("luciel_instance_id", attrs)


if __name__ == "__main__":
    unittest.main()
