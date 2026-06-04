"""Rescan Tier-C — migration shape test for rescanc_graph_kb.

Verifies (no live DB required):
1. Revision ID = 'rescanc_graph_kb'.
2. down_revision = 'rescanc_handoff_session_mode'.
3. knowledge_graph_nodes table declared.
4. knowledge_graph_edges table declared.
5. RLS policies declared.
6. Downgrade drops both tables.
"""
from __future__ import annotations

import importlib
import pathlib
import unittest

MIGRATION_PATH = (
    pathlib.Path(__file__).parent.parent.parent
    / "alembic" / "versions" / "rescanc_graph_kb.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "rescanc_graph_kb", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestGraphKBMigrationShape(unittest.TestCase):

    def setUp(self):
        self.m = _load_migration()
        self.content = MIGRATION_PATH.read_text()

    def test_revision_id(self):
        self.assertEqual(self.m.revision, "rescanc_graph_kb")

    def test_down_revision(self):
        self.assertEqual(self.m.down_revision, "rescanc_handoff_session_mode")

    def test_upgrade_callable(self):
        self.assertTrue(callable(getattr(self.m, "upgrade", None)))

    def test_downgrade_callable(self):
        self.assertTrue(callable(getattr(self.m, "downgrade", None)))

    def test_nodes_table_declared(self):
        self.assertIn("knowledge_graph_nodes", self.content)

    def test_edges_table_declared(self):
        self.assertIn("knowledge_graph_edges", self.content)

    def test_rls_enable_nodes(self):
        self.assertIn(
            "ENABLE ROW LEVEL SECURITY", self.content,
        )

    def test_rls_force_nodes(self):
        self.assertIn(
            "FORCE ROW LEVEL SECURITY", self.content,
        )

    def test_rls_policy_nodes(self):
        self.assertIn("kg_nodes_admin_isolation", self.content)

    def test_rls_policy_edges(self):
        self.assertIn("kg_edges_admin_isolation", self.content)

    def test_rls_uses_current_setting_guc(self):
        self.assertIn("current_setting('app.admin_id', true)", self.content)

    def test_downgrade_drops_edges_before_nodes(self):
        drop_edges_pos = self.content.find("drop_table(\"knowledge_graph_edges\")")
        drop_nodes_pos = self.content.find("drop_table(\"knowledge_graph_nodes\")")
        self.assertGreater(drop_edges_pos, 0)
        self.assertGreater(drop_nodes_pos, 0)
        # Edges must be dropped BEFORE nodes (FK dependency).
        self.assertLess(drop_edges_pos, drop_nodes_pos)

    def test_node_type_is_text_not_enum(self):
        """node_type must be a TEXT column, not an ENUM — domain-agnostic."""
        # The migration must NOT declare an ENUM for node_type.
        self.assertNotIn("ENUM", self.content)
        self.assertNotIn("enum", self.content.lower().replace("enumerate", ""))
        # It must use text-type columns.
        self.assertIn("node_type", self.content)
        self.assertIn("edge_type", self.content)

    def test_domain_agnostic_comment(self):
        """Migration must document domain-agnostic node/edge types."""
        self.assertIn("domain-agnostic", self.content)
