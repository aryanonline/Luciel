"""ARC 16 — live graph store + recursive-CTE traversal tests.

Runs against a real Postgres (gated on LUCIEL_LIVE_POSTGRES_URL, like
the Arc 11 live RLS suite). Proves the graph store behaves correctly
end-to-end: attribute filtering, multi-hop recursive traversal with a
cycle guard, the scope triple on every node, and cross-tenant RLS
isolation. No embedder needed — the graph layer does not embed.
"""
from __future__ import annotations

import json
import os
import unittest
import uuid

import psycopg
from psycopg import sql as pgsql
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker


_PG_URL = os.environ.get("LUCIEL_LIVE_POSTGRES_URL")


def _sa_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


@unittest.skipUnless(
    _PG_URL,
    "Set LUCIEL_LIVE_POSTGRES_URL to run the live graph store tests.",
)
class TestArc16GraphStoreLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin_a = "ga_" + uuid.uuid4().hex[:8]
        cls.admin_b = "gb_" + uuid.uuid4().hex[:8]
        cls.role = "grole_" + uuid.uuid4().hex[:8]
        cls.conn = psycopg.connect(_PG_URL, autocommit=True)

        with cls.conn.cursor() as c:
            c.execute(
                pgsql.SQL(
                    "CREATE ROLE {r} LOGIN PASSWORD 'x' NOSUPERUSER "
                    "NOBYPASSRLS"
                ).format(r=pgsql.Identifier(cls.role))
            )
            c.execute(
                pgsql.SQL("GRANT USAGE ON SCHEMA public TO {r}").format(
                    r=pgsql.Identifier(cls.role)
                )
            )
            c.execute(
                pgsql.SQL(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                    "IN SCHEMA public TO {r}"
                ).format(r=pgsql.Identifier(cls.role))
            )
            c.execute(
                pgsql.SQL(
                    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public "
                    "TO {r}"
                ).format(r=pgsql.Identifier(cls.role))
            )

            cls.meta: dict[str, dict] = {}
            for aid in (cls.admin_a, cls.admin_b):
                c.execute(
                    "INSERT INTO admins (id,name,tier,active) "
                    "VALUES (%s,%s,'pro',true) ON CONFLICT (id) DO NOTHING",
                    (aid, aid),
                )
                c.execute(
                    "INSERT INTO instances (admin_id,instance_slug,"
                    "display_name,active) VALUES (%s,%s,%s,true) RETURNING id",
                    (aid, "inst-" + aid, aid),
                )
                inst = c.fetchone()[0]
                c.execute(
                    "INSERT INTO knowledge_sources (admin_id,"
                    "luciel_instance_id,source_type,size_bytes,"
                    "ingestion_status,ingested_by) "
                    "VALUES (%s,%s,'csv',1,'ready','seed') RETURNING id",
                    (aid, inst),
                )
                src = c.fetchone()[0]
                cls.meta[aid] = {"inst": inst, "src": src}

            a = cls.meta[cls.admin_a]

            def node(etype, label, attrs):
                c.execute(
                    "INSERT INTO knowledge_graph_nodes (admin_id,"
                    "luciel_instance_id,entity_type,entity_label,attributes,"
                    "source_id) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                    (cls.admin_a, a["inst"], etype, label,
                     json.dumps(attrs), a["src"]),
                )
                return c.fetchone()[0]

            def edge(s, d, rel):
                c.execute(
                    "INSERT INTO knowledge_graph_edges (admin_id,"
                    "luciel_instance_id,src_node_id,dst_node_id,"
                    "relationship_type,source_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (cls.admin_a, a["inst"], s, d, rel, a["src"]),
                )

            cls.l1 = node("Listing", "123 Main St",
                          {"bedrooms": 3, "price": 900000})
            cls.l2 = node("Listing", "45 Oak Ave",
                          {"bedrooms": 2, "price": 700000})
            riverside = node("Neighborhood", "Riverside", {})
            city = node("City", "Springfield", {})
            edge(cls.l1, riverside, "IS_IN")
            edge(cls.l2, riverside, "IS_IN")
            edge(riverside, city, "IN")
            edge(city, riverside, "CONTAINS")  # deliberate cycle

            b = cls.meta[cls.admin_b]
            c.execute(
                "INSERT INTO knowledge_graph_nodes (admin_id,"
                "luciel_instance_id,entity_type,entity_label,attributes,"
                "source_id) VALUES (%s,%s,%s,%s,%s,%s)",
                (cls.admin_b, b["inst"], "Listing", "B-SECRET-LISTING",
                 json.dumps({"bedrooms": 3, "price": 900000}), b["src"]),
            )

        cls.engine = create_engine(
            f"postgresql+psycopg://{cls.role}:x@127.0.0.1:5432/luciel",
            future=True,
        )

        @event.listens_for(cls.engine, "begin")
        def _bind(conn):  # noqa: ANN001
            conn.execute(
                text("SELECT set_config('app.admin_id', :a, true)").bindparams(
                    a=cls.admin_a
                )
            )
            conn.execute(
                text(
                    "SELECT set_config('app.instance_id', :i, true)"
                ).bindparams(i=str(a["inst"]))
            )

        cls.Session = sessionmaker(bind=cls.engine, future=True)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        with cls.conn.cursor() as c:
            for aid in (cls.admin_a, cls.admin_b):
                c.execute(
                    "DELETE FROM knowledge_graph_edges WHERE admin_id=%s",
                    (aid,),
                )
                c.execute(
                    "DELETE FROM knowledge_graph_nodes WHERE admin_id=%s",
                    (aid,),
                )
                c.execute(
                    "DELETE FROM knowledge_sources WHERE admin_id=%s", (aid,)
                )
                c.execute("DELETE FROM instances WHERE admin_id=%s", (aid,))
                c.execute("DELETE FROM admins WHERE id=%s", (aid,))
            c.execute(
                pgsql.SQL("DROP OWNED BY {r}").format(
                    r=pgsql.Identifier(cls.role)
                )
            )
            c.execute(
                pgsql.SQL("DROP ROLE {r}").format(r=pgsql.Identifier(cls.role))
            )
        cls.conn.close()

    def _repo(self, session):
        from app.repositories.knowledge_graph_repository import (
            KnowledgeGraphRepository,
        )

        return KnowledgeGraphRepository(session)

    def test_find_nodes_attribute_filter(self):
        a = self.meta[self.admin_a]
        with self.Session() as s:
            res = self._repo(s).find_nodes(
                admin_id=self.admin_a,
                luciel_instance_id=a["inst"],
                entity_type="Listing",
                attribute_filters={"bedrooms": 3},
            )
        self.assertEqual(
            sorted(n.entity_label for n in res), ["123 Main St"]
        )

    def test_recursive_traverse_with_cycle_guard(self):
        a = self.meta[self.admin_a]
        with self.Session() as s:
            reached = self._repo(s).traverse(
                admin_id=self.admin_a,
                luciel_instance_id=a["inst"],
                seed_node_ids=[self.l1],
                max_depth=3,
            )
        self.assertEqual(
            sorted(n.entity_label for n in reached),
            ["123 Main St", "Riverside", "Springfield"],
        )

    def test_scope_triple_on_every_node(self):
        a = self.meta[self.admin_a]
        with self.Session() as s:
            res = self._repo(s).find_nodes(
                admin_id=self.admin_a,
                luciel_instance_id=a["inst"],
                limit=500,
            )
        self.assertGreater(len(res), 0)
        for n in res:
            self.assertEqual(n.admin_id, self.admin_a)
            self.assertEqual(n.luciel_instance_id, a["inst"])
            self.assertIsInstance(n.source_id, int)
            self.assertGreater(n.source_id, 0)

    def test_cross_tenant_isolation(self):
        a = self.meta[self.admin_a]
        with self.Session() as s:
            res = self._repo(s).find_nodes(
                admin_id=self.admin_a,
                luciel_instance_id=a["inst"],
                limit=500,
            )
        self.assertFalse(
            any("B-SECRET" in n.entity_label for n in res),
            "cross-tenant leak: tenant B graph node visible to A",
        )
