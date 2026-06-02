"""ARC 16 — hybrid retrieval engine tests.

Two layers:
  * Pure intent-classifier unit tests (no DB, no network) — assert the
    structured-filter-intent router fires on relational/multi-attribute
    queries and bypasses on pure-semantic ones.
  * Live engine tests (gated on LUCIEL_LIVE_POSTGRES_URL) — seed real
    chunks (with embeddings) + a graph under one tenant, patch the
    embedder to a deterministic vector (no OpenAI), and assert:
      - pure-semantic query → graph bypassed (graph_engaged False)
      - structured query → graph engaged, chunks from graph-matched
        sources ordered first (graph-informed merge)
      - never-raise: a broken graph stage degrades to vector-only
"""
from __future__ import annotations

import os
import unittest
import uuid
from unittest.mock import patch

from app.runtime.knowledge_retrieval import has_structured_filter_intent


class TestStructuredFilterIntent(unittest.TestCase):
    """Pure classifier — no DB, no network."""

    def test_pure_semantic_bypasses(self):
        for q in [
            "what's your refund policy?",
            "does this listing have a pool?",
            "tell me about your company",
            "who treats back pain?",
            "",
        ]:
            self.assertFalse(
                has_structured_filter_intent(q),
                f"expected pure-semantic (no graph) for: {q!r}",
            )

    def test_structured_engages(self):
        for q in [
            "which listings have 3 bedrooms and are under $1M?",
            "which practitioners offer both laser and microneedling?",
            "show me homes under $900,000",
            "roles that require both Python and cloud experience",
        ]:
            self.assertTrue(
                has_structured_filter_intent(q),
                f"expected structured-filter intent for: {q!r}",
            )


_PG_URL = os.environ.get("LUCIEL_LIVE_POSTGRES_URL")


@unittest.skipUnless(
    _PG_URL, "Set LUCIEL_LIVE_POSTGRES_URL to run the live hybrid tests."
)
class TestHybridRetrievalLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import json

        import psycopg
        from psycopg import sql as pgsql

        cls.admin = "hy_" + uuid.uuid4().hex[:8]
        cls.role = "hyrole_" + uuid.uuid4().hex[:8]
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
            # two sources: src_graph (will be graph-matched) + src_other
            c.execute(
                "INSERT INTO knowledge_sources (admin_id,luciel_instance_id,"
                "source_type,size_bytes,ingestion_status,ingested_by) "
                "VALUES (%s,%s,'csv',1,'ready','seed') RETURNING id",
                (cls.admin, cls.inst),
            )
            cls.src_graph = c.fetchone()[0]
            c.execute(
                "INSERT INTO knowledge_sources (admin_id,luciel_instance_id,"
                "source_type,size_bytes,ingestion_status,ingested_by) "
                "VALUES (%s,%s,'txt',1,'ready','seed') RETURNING id",
                (cls.admin, cls.inst),
            )
            cls.src_other = c.fetchone()[0]

            vec = "[" + ",".join(["0.0001"] * 1536) + "]"
            # chunk from the graph-matched source
            c.execute(
                "INSERT INTO knowledge_chunks (admin_id,luciel_instance_id,"
                "content,knowledge_type,source_id,embedding) "
                "VALUES (%s,%s,%s,'tenant_document',%s,%s::vector)",
                (cls.admin, cls.inst, "GRAPH_SOURCE_CHUNK", cls.src_graph, vec),
            )
            # chunk from a non-graph source
            c.execute(
                "INSERT INTO knowledge_chunks (admin_id,luciel_instance_id,"
                "content,knowledge_type,source_id,embedding) "
                "VALUES (%s,%s,%s,'tenant_document',%s,%s::vector)",
                (cls.admin, cls.inst, "OTHER_SOURCE_CHUNK", cls.src_other, vec),
            )
            # a graph node whose source is src_graph
            c.execute(
                "INSERT INTO knowledge_graph_nodes (admin_id,"
                "luciel_instance_id,entity_type,entity_label,attributes,"
                "source_id) VALUES (%s,%s,'Listing','123 Main St',%s,%s)",
                (cls.admin, cls.inst,
                 json.dumps({"bedrooms": 3, "price": 900000}), cls.src_graph),
            )

        from sqlalchemy import create_engine, event, text
        from sqlalchemy.orm import sessionmaker

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
        from psycopg import sql as pgsql

        cls.engine.dispose()
        with cls.conn.cursor() as c:
            c.execute(
                "DELETE FROM knowledge_graph_nodes WHERE admin_id=%s",
                (cls.admin,),
            )
            c.execute(
                "DELETE FROM knowledge_chunks WHERE admin_id=%s", (cls.admin,)
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

    def _engine_under_patch(self):
        # Patch embed_single so the vector lane runs without OpenAI.
        return patch(
            "app.knowledge.retriever.embed_single",
            return_value=[0.0001] * 1536,
        )

    def test_pure_semantic_bypasses_graph(self):
        from app.runtime.knowledge_retrieval import HybridRetriever

        with self._engine_under_patch(), self.Session() as s:
            res = HybridRetriever(s).retrieve(
                query="what is your refund policy?",
                admin_id=self.admin,
                luciel_instance_id=self.inst,
                limit=5,
            )
        self.assertFalse(res.graph_engaged)
        self.assertGreater(len(res.chunks), 0)

    def test_structured_engages_graph_and_orders_graph_source_first(self):
        from app.runtime.knowledge_retrieval import HybridRetriever

        with self._engine_under_patch(), self.Session() as s:
            res = HybridRetriever(s).retrieve(
                query="which listings have 3 bedrooms and are under $1M?",
                admin_id=self.admin,
                luciel_instance_id=self.inst,
                limit=5,
            )
        self.assertTrue(res.graph_engaged)
        self.assertGreater(res.graph_node_count, 0)
        # The chunk from the graph-matched source must be ordered first.
        self.assertGreater(len(res.chunks), 0)
        self.assertEqual(res.chunks[0].content, "GRAPH_SOURCE_CHUNK")
        # scope triple intact on merged output
        for c in res.chunks:
            self.assertEqual(c.admin_id, self.admin)
            self.assertEqual(c.luciel_instance_id, self.inst)

    def test_graph_failure_degrades_to_vector(self):
        """A broken graph stage must never be worse than vector-only."""
        from app.runtime.knowledge_retrieval import HybridRetriever

        with self._engine_under_patch(), self.Session() as s:
            r = HybridRetriever(s)
            with patch.object(
                r._graph, "find_nodes", side_effect=RuntimeError("boom")
            ):
                res = r.retrieve(
                    query="which listings have 3 bedrooms and under $1M?",
                    admin_id=self.admin,
                    luciel_instance_id=self.inst,
                    limit=5,
                )
        # graph blew up → vector-only fallback, no raise, chunks present
        self.assertFalse(res.graph_engaged)
        self.assertGreater(len(res.chunks), 0)
