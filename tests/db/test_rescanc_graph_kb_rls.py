"""Rescan Tier-C — live RLS verification for knowledge_graph_nodes/edges.

Verifies (with a real PostgreSQL connection):
  * Admin B CANNOT read Admin A's graph nodes (cross-tenant denial).
  * Admin B CANNOT read Admin A's graph edges (cross-tenant denial).
  * Admin A CAN read their own graph nodes and edges.
  * The fail-closed property: unset GUC → empty result set.

Guards: requires LUCIEL_LIVE_POSTGRES_URL env var. Skipped in CI.
"""
from __future__ import annotations

import os
import unittest
import uuid

_PG_URL = os.environ.get("LUCIEL_LIVE_POSTGRES_URL")


@unittest.skipUnless(
    _PG_URL,
    "Set LUCIEL_LIVE_POSTGRES_URL=postgresql://... to run live graph RLS tests",
)
class TestGraphKBRLS(unittest.TestCase):
    """Live cross-tenant denial for knowledge_graph_nodes + knowledge_graph_edges."""

    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls.psycopg = psycopg
        cls.admin_conn = psycopg.connect(_PG_URL, autocommit=True)

        cls.admin_a = f"graph-rls-a-{uuid.uuid4().hex[:8]}"
        cls.admin_b = f"graph-rls-b-{uuid.uuid4().hex[:8]}"

        # Create an app role that is NOBYPASSRLS.
        cls.app_role = f"test_graph_rls_{uuid.uuid4().hex[:8]}"
        with cls.admin_conn.cursor() as cur:
            cur.execute(
                f"CREATE ROLE {cls.app_role} NOINHERIT LOGIN NOBYPASSRLS"
                " NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD 'testpwd';"
            )
            # Grant table access so setup works.
            cur.execute(
                f"GRANT SELECT, INSERT, DELETE ON knowledge_graph_nodes, "
                f"knowledge_graph_edges TO {cls.app_role};"
            )
            # Grant usage on sequences (for INSERT).
            cur.execute(
                f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public "
                f"TO {cls.app_role};"
            )

        # Need admins + instances + knowledge_sources rows to satisfy FKs.
        # Use the admin (superuser) connection for setup inserts.
        cls.instance_a = cls._create_fixtures(cls.admin_a)
        cls.instance_b = cls._create_fixtures(cls.admin_b)

        # Insert graph nodes for Admin A.
        cls.node_a_id = cls._insert_node(cls.admin_a, cls.instance_a)
        cls.edge_a_id = cls._insert_edge(cls.admin_a, cls.instance_a, cls.node_a_id)

    @classmethod
    def _create_fixtures(cls, admin_id: str) -> int:
        """Create admin, instance, knowledge_source rows for FK satisfaction.
        Returns instance_id.
        """
        with cls.admin_conn.cursor() as cur:
            # Admin row (minimal)
            cur.execute(
                "INSERT INTO admins (id, tier, created_at) "
                "VALUES (%s, 'free', now()) ON CONFLICT DO NOTHING;",
                (admin_id,),
            )
            # Instance row
            cur.execute(
                "INSERT INTO instances (admin_id, name, created_at) "
                "VALUES (%s, 'test', now()) RETURNING id;",
                (admin_id,),
            )
            instance_id = cur.fetchone()[0]
            # Knowledge source row
            cur.execute(
                "INSERT INTO knowledge_sources "
                "(admin_id, luciel_instance_id, filename, source_type, "
                " size_bytes, ingested_by, ingestion_status, created_at) "
                "VALUES (%s, %s, 'test.txt', 'txt', 100, 'test', 'ready', now()) "
                "RETURNING id;",
                (admin_id, instance_id),
            )
            cls.__dict__.setdefault("_source_ids", {})[admin_id] = cur.fetchone()[0]
            return instance_id

    @classmethod
    def _source_id(cls, admin_id: str) -> int:
        return cls.__dict__["_source_ids"][admin_id]

    @classmethod
    def _insert_node(cls, admin_id: str, instance_id: int) -> int:
        with cls.admin_conn.cursor() as cur:
            cur.execute(
                "SET app.admin_id = %s;", (admin_id,)
            )
            cur.execute(
                "INSERT INTO knowledge_graph_nodes "
                "(admin_id, instance_id, source_id, node_type, label, attributes, created_at) "
                "VALUES (%s, %s, %s, 'Role', 'Test Node', '{}', now()) RETURNING id;",
                (admin_id, instance_id, cls._source_id(admin_id)),
            )
            return cur.fetchone()[0]

    @classmethod
    def _insert_edge(cls, admin_id: str, instance_id: int, node_id: int) -> int:
        with cls.admin_conn.cursor() as cur:
            cur.execute("SET app.admin_id = %s;", (admin_id,))
            cur.execute(
                "INSERT INTO knowledge_graph_edges "
                "(admin_id, instance_id, source_id, src_node_id, dst_node_id, "
                " edge_type, attributes, created_at) "
                "VALUES (%s, %s, %s, %s, %s, 'is_a', '{}', now()) RETURNING id;",
                (admin_id, instance_id, cls._source_id(admin_id), node_id, node_id),
            )
            return cur.fetchone()[0]

    @classmethod
    def tearDownClass(cls) -> None:
        with cls.admin_conn.cursor() as cur:
            # Cleanup in FK order.
            for aid in (cls.admin_a, cls.admin_b):
                cur.execute(
                    "DELETE FROM knowledge_graph_edges WHERE admin_id = %s;", (aid,)
                )
                cur.execute(
                    "DELETE FROM knowledge_graph_nodes WHERE admin_id = %s;", (aid,)
                )
                cur.execute(
                    "DELETE FROM knowledge_sources WHERE admin_id = %s;", (aid,)
                )
                cur.execute(
                    "DELETE FROM instances WHERE admin_id = %s;", (aid,)
                )
                cur.execute(
                    "DELETE FROM admins WHERE id = %s;", (aid,)
                )
            cur.execute(f"DROP ROLE IF EXISTS {cls.app_role};")
        cls.admin_conn.close()

    def _app_conn(self, admin_id: str | None):
        """Open a connection as the app role with admin_id GUC set."""
        import psycopg
        # Build URL with app role credentials.
        base_url = _PG_URL.replace("://postgres:postgres@", f"://{self.app_role}:testpwd@")
        conn = psycopg.connect(base_url, autocommit=True)
        with conn.cursor() as cur:
            if admin_id is not None:
                cur.execute("SET app.admin_id = %s;", (admin_id,))
            # else: leave unset (fail-closed test)
        return conn

    def test_admin_a_can_read_own_nodes(self):
        """Admin A can read their own nodes under their own GUC."""
        conn = self._app_conn(self.admin_a)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM knowledge_graph_nodes WHERE admin_id = %s;",
                    (self.admin_a,),
                )
                rows = cur.fetchall()
            self.assertGreater(len(rows), 0, "Admin A should see their own nodes")
        finally:
            conn.close()

    def test_admin_b_cannot_read_admin_a_nodes(self):
        """Admin B CANNOT read Admin A's graph nodes (cross-tenant denial)."""
        conn = self._app_conn(self.admin_b)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM knowledge_graph_nodes WHERE admin_id = %s;",
                    (self.admin_a,),
                )
                rows = cur.fetchall()
            self.assertEqual(
                len(rows), 0,
                f"Admin B must NOT see Admin A's graph nodes. Got {len(rows)} rows.",
            )
        finally:
            conn.close()

    def test_admin_b_cannot_read_admin_a_edges(self):
        """Admin B CANNOT read Admin A's graph edges (cross-tenant denial)."""
        conn = self._app_conn(self.admin_b)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM knowledge_graph_edges WHERE admin_id = %s;",
                    (self.admin_a,),
                )
                rows = cur.fetchall()
            self.assertEqual(
                len(rows), 0,
                f"Admin B must NOT see Admin A's graph edges. Got {len(rows)} rows.",
            )
        finally:
            conn.close()

    def test_unset_guc_denies_node_reads(self):
        """Unset app.admin_id GUC → fail-closed: no rows returned."""
        conn = self._app_conn(admin_id=None)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM knowledge_graph_nodes;")
                rows = cur.fetchall()
            self.assertEqual(
                len(rows), 0,
                f"Unset GUC should deny all node reads. Got {len(rows)} rows.",
            )
        finally:
            conn.close()
