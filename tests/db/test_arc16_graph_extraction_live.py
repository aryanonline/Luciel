"""ARC 16 — graph population pipeline tests (deterministic, live DB).

Exercises GraphIngestionService with a STUB extractor (no LLM), proving
the deterministic machinery against real Postgres:
  * entity resolution / dedup (same entity mentioned twice → one node)
  * source attribution (every node + edge carries source_id)
  * edge wiring incl. auto-resolution of relation endpoints
  * supersede-on-reingest (old source rows stamped, not duplicated)
  * never-raise envelope

The LLM-backed extractor itself is out of scope here (needs a real
model); it is validated via the runbook with a real key. The contract
boundary is the EntityExtractor protocol, stubbed below.
"""
from __future__ import annotations

import os
import unittest
import uuid

import psycopg
from psycopg import sql as pgsql
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.knowledge.graph_extractor import (
    ExtractedEntity,
    ExtractedRelation,
    ExtractionResult,
    GraphIngestionService,
)


_PG_URL = os.environ.get("LUCIEL_LIVE_POSTGRES_URL")


@unittest.skipUnless(
    _PG_URL, "Set LUCIEL_LIVE_POSTGRES_URL to run graph extraction tests."
)
class TestArc16GraphExtractionLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = "ex_" + uuid.uuid4().hex[:8]
        cls.role = "exrole_" + uuid.uuid4().hex[:8]
        cls.conn = psycopg.connect(_PG_URL, autocommit=True)
        with cls.conn.cursor() as c:
            c.execute(
                pgsql.SQL(
                    "CREATE ROLE {r} LOGIN PASSWORD 'x' NOSUPERUSER "
                    "NOBYPASSRLS"
                ).format(r=pgsql.Identifier(cls.role))
            )
            for stmt in (
                "GRANT USAGE ON SCHEMA public TO {r}",
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                "IN SCHEMA public TO {r}",
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {r}",
            ):
                c.execute(pgsql.SQL(stmt).format(r=pgsql.Identifier(cls.role)))
            c.execute(
                "INSERT INTO admins (id,name,tier,active) "
                "VALUES (%s,%s,'pro',true)",
                (cls.admin, cls.admin),
            )
            c.execute(
                "INSERT INTO instances (admin_id,instance_slug,display_name,"
                "active) VALUES (%s,%s,%s,true) RETURNING id",
                (cls.admin, "inst-" + cls.admin, cls.admin),
            )
            cls.inst = c.fetchone()[0]
            c.execute(
                "INSERT INTO knowledge_sources (admin_id,luciel_instance_id,"
                "source_type,size_bytes,ingestion_status,ingested_by) "
                "VALUES (%s,%s,'csv',1,'ready','seed') RETURNING id",
                (cls.admin, cls.inst),
            )
            cls.src = c.fetchone()[0]

        cls.engine = create_engine(
            f"postgresql+psycopg://{cls.role}:x@127.0.0.1:5432/luciel",
            future=True,
        )

        @event.listens_for(cls.engine, "begin")
        def _bind(conn):  # noqa: ANN001
            conn.execute(
                text("SELECT set_config('app.admin_id', :a, true)").bindparams(
                    a=cls.admin
                )
            )
            conn.execute(
                text(
                    "SELECT set_config('app.instance_id', :i, true)"
                ).bindparams(i=str(cls.inst))
            )

        cls.Session = sessionmaker(bind=cls.engine, future=True)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        with cls.conn.cursor() as c:
            c.execute(
                "DELETE FROM knowledge_graph_edges WHERE admin_id=%s",
                (cls.admin,),
            )
            c.execute(
                "DELETE FROM knowledge_graph_nodes WHERE admin_id=%s",
                (cls.admin,),
            )
            c.execute(
                "DELETE FROM knowledge_sources WHERE admin_id=%s", (cls.admin,)
            )
            c.execute("DELETE FROM instances WHERE admin_id=%s", (cls.admin,))
            c.execute("DELETE FROM admins WHERE id=%s", (cls.admin,))
            c.execute(
                pgsql.SQL("DROP OWNED BY {r}").format(
                    r=pgsql.Identifier(cls.role)
                )
            )
            c.execute(
                pgsql.SQL("DROP ROLE {r}").format(r=pgsql.Identifier(cls.role))
            )
        cls.conn.close()

    def _counts(self):
        with self.Session() as s:
            n = s.execute(
                text(
                    "SELECT count(*) FROM knowledge_graph_nodes "
                    "WHERE admin_id=:a AND superseded_at IS NULL"
                ),
                {"a": self.admin},
            ).scalar_one()
            e = s.execute(
                text(
                    "SELECT count(*) FROM knowledge_graph_edges "
                    "WHERE admin_id=:a AND superseded_at IS NULL"
                ),
                {"a": self.admin},
            ).scalar_one()
        return n, e

    def test_populate_resolves_dedups_and_attributes(self):
        # Extraction mentions 'Riverside' twice (once as entity, once as
        # an edge endpoint) — must resolve to ONE node.
        extraction = ExtractionResult(
            entities=[
                ExtractedEntity("Listing", "123 Main St",
                                {"bedrooms": 3, "price": 900000}),
                ExtractedEntity("Neighborhood", "Riverside"),
            ],
            relations=[
                ExtractedRelation("123 Main St", "Listing",
                                  "Riverside", "Neighborhood", "IS_IN"),
                # endpoint 'Springfield' not in entity list → auto-resolved
                ExtractedRelation("Riverside", "Neighborhood",
                                  "Springfield", "City", "IN"),
            ],
        )
        with self.Session() as s:
            svc = GraphIngestionService(s)
            n_nodes, n_edges = svc.populate_from_source(
                admin_id=self.admin,
                luciel_instance_id=self.inst,
                source_id=self.src,
                extraction=extraction,
            )
        # 3 distinct nodes (Listing, Riverside, Springfield), 2 edges
        self.assertEqual(n_nodes, 3)
        self.assertEqual(n_edges, 2)

        # every node + edge attributed to the source
        with self.Session() as s:
            bad_nodes = s.execute(
                text(
                    "SELECT count(*) FROM knowledge_graph_nodes "
                    "WHERE admin_id=:a AND source_id <> :src"
                ),
                {"a": self.admin, "src": self.src},
            ).scalar_one()
            bad_edges = s.execute(
                text(
                    "SELECT count(*) FROM knowledge_graph_edges "
                    "WHERE admin_id=:a AND source_id <> :src"
                ),
                {"a": self.admin, "src": self.src},
            ).scalar_one()
        self.assertEqual(bad_nodes, 0)
        self.assertEqual(bad_edges, 0)

    def test_reingest_supersedes_not_duplicates(self):
        # Re-running the same extraction must NOT double the active rows;
        # old rows are superseded, the new ones replace them.
        extraction = ExtractionResult(
            entities=[ExtractedEntity("Service", "Microneedling",
                                      {"price": 300})],
            relations=[
                ExtractedRelation("Microneedling", "Service",
                                  "Acne", "Condition", "TREATS"),
            ],
        )
        with self.Session() as s:
            svc = GraphIngestionService(s)
            svc.populate_from_source(
                admin_id=self.admin, luciel_instance_id=self.inst,
                source_id=self.src, extraction=extraction,
            )
        n1, e1 = self._counts()
        with self.Session() as s:
            svc = GraphIngestionService(s)
            svc.populate_from_source(
                admin_id=self.admin, luciel_instance_id=self.inst,
                source_id=self.src, extraction=extraction,
            )
        n2, e2 = self._counts()
        # active counts stable across re-ingest (resolution + supersede)
        self.assertEqual(n1, n2)
        self.assertEqual(e1, e2)

    def test_never_raises_on_bad_extraction(self):
        # A relation referencing impossible data should not raise; the
        # service swallows and returns (0,0).
        with self.Session() as s:
            svc = GraphIngestionService(s)
            # entity_label None will violate NOT NULL → caught internally
            bad = ExtractionResult(
                entities=[ExtractedEntity("X", None)]  # type: ignore[arg-type]
            )
            result = svc.populate_from_source(
                admin_id=self.admin, luciel_instance_id=self.inst,
                source_id=self.src, extraction=bad,
            )
        self.assertEqual(result, (0, 0))
