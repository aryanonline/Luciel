"""ARC 16 — ingestion → graph population end-to-end wiring (live DB).

Proves that IngestionService, when given an EntityExtractor, populates
BOTH the vector chunks (Arc 11 baseline) AND the knowledge graph from
the same source, in one ingest, attributed to the same source_id — and
that a failing extractor never fails the ingest (vector chunks still
land). Embedder is patched (no OpenAI); the extractor is a deterministic
stub (no LLM).
"""
from __future__ import annotations

import os
import unittest
import uuid
from unittest.mock import patch

import psycopg
from psycopg import sql as pgsql
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.knowledge.graph_extractor import (
    ExtractedEntity,
    ExtractedRelation,
    ExtractionResult,
)

_PG_URL = os.environ.get("LUCIEL_LIVE_POSTGRES_URL")


class _StubExtractor:
    def __init__(self, result=None, boom=False):
        self._result = result or ExtractionResult(
            entities=[
                ExtractedEntity("Listing", "123 Main St",
                                {"bedrooms": 3, "price": 900000}),
                ExtractedEntity("Neighborhood", "Riverside"),
            ],
            relations=[
                ExtractedRelation("123 Main St", "Listing",
                                  "Riverside", "Neighborhood", "IS_IN"),
            ],
        )
        self._boom = boom

    def extract(self, *, text, business_description=None):  # noqa: ANN001
        if self._boom:
            raise RuntimeError("extractor exploded")
        return self._result


@unittest.skipUnless(
    _PG_URL, "Set LUCIEL_LIVE_POSTGRES_URL to run ingestion-graph wiring."
)
class TestArc16IngestionGraphWiringLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = "ig_" + uuid.uuid4().hex[:8]
        cls.conn = psycopg.connect(_PG_URL, autocommit=True)
        with cls.conn.cursor() as c:
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

        cls.engine = create_engine(_sa_url(_PG_URL), future=True)

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
            for t in (
                "knowledge_graph_edges", "knowledge_graph_nodes",
                "knowledge_chunks", "knowledge_sources", "instances",
            ):
                c.execute(
                    pgsql.SQL("DELETE FROM {t} WHERE admin_id=%s").format(
                        t=pgsql.Identifier(t)
                    ),
                    (cls.admin,),
                )
            c.execute("DELETE FROM admins WHERE id=%s", (cls.admin,))
        cls.conn.close()

    def _ingest(self, extractor):
        from app.knowledge.chunker import EffectiveChunkingConfig
        from app.knowledge.ingestion import IngestionService

        cfg = EffectiveChunkingConfig(
            chunk_size=512, chunk_overlap=0, chunk_strategy="fixed",
            size_source="tenant", overlap_source="tenant",
            strategy_source="tenant",
        )
        # Patch embed_texts (no OpenAI) and the chunking-config resolver
        # (its instance-config fixture is Arc 11 machinery orthogonal to
        # the graph-wiring seam under test here).
        with patch(
            "app.knowledge.ingestion.embed_texts",
            side_effect=lambda chunks: [[0.0001] * 1536 for _ in chunks],
        ), patch.object(
            IngestionService, "_resolve_chunking_config", return_value=cfg
        ):
            with self.Session() as s:
                svc = IngestionService(s, graph_extractor=extractor)
                res = svc.ingest_text(
                    content="123 Main St is a 3 bedroom home in Riverside.",
                    admin_id=self.admin,
                    luciel_instance_id=self.inst,
                    title="t",
                )
                s.commit()
                return res

    def _graph_counts(self, source_id):
        with self.Session() as s:
            n = s.execute(
                text(
                    "SELECT count(*) FROM knowledge_graph_nodes "
                    "WHERE admin_id=:a AND source_id=:s"
                ),
                {"a": self.admin, "s": source_id},
            ).scalar_one()
            e = s.execute(
                text(
                    "SELECT count(*) FROM knowledge_graph_edges "
                    "WHERE admin_id=:a AND source_id=:s"
                ),
                {"a": self.admin, "s": source_id},
            ).scalar_one()
            ch = s.execute(
                text(
                    "SELECT count(*) FROM knowledge_chunks "
                    "WHERE admin_id=:a AND source_id=:s"
                ),
                {"a": self.admin, "s": source_id},
            ).scalar_one()
        return n, e, ch

    def test_ingest_populates_chunks_and_graph_same_source(self):
        res = self._ingest(_StubExtractor())
        n, e, ch = self._graph_counts(res.source_id)
        self.assertGreater(ch, 0, "vector chunks must be created")
        self.assertEqual(n, 2, "two graph nodes from the same source")
        self.assertEqual(e, 1, "one graph edge from the same source")

    def test_failing_extractor_does_not_fail_ingest(self):
        # Extractor explodes — ingest must still succeed and create chunks.
        res = self._ingest(_StubExtractor(boom=True))
        n, e, ch = self._graph_counts(res.source_id)
        self.assertGreater(ch, 0, "vector chunks must still be created")
        self.assertEqual(n, 0, "no graph nodes when extractor failed")

    def test_no_extractor_behaves_like_arc11(self):
        # No extractor injected → pure Arc 11 behaviour, no graph rows.
        res = self._ingest(None)
        n, e, ch = self._graph_counts(res.source_id)
        self.assertGreater(ch, 0)
        self.assertEqual(n, 0)
        self.assertEqual(e, 0)


def _sa_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url
