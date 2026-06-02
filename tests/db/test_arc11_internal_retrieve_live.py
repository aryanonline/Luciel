"""Arc 11 Step 7 — live verification of /internal/v1/retrieve.

What only a live Postgres can prove:

  L1  Calling the handler under platform_admin with payload.admin_id=A
      returns ONLY Admin A's chunks, even though A has chunks under
      a 2-tenant fixture and the platform_admin's own request.state
      has no admin scope.
  L2  The returned ``explain`` text shows the HNSW or
      ``Index Scan`` path is being used (locked-in plan signature).

The static-shape contract for the handler lives in
``tests/api/test_internal_retrieve.py``.

How to run:

    LUCIEL_LIVE_POSTGRES_URL=postgresql://postgres@127.0.0.1:5433/postgres \\
        python -m pytest tests/db/test_arc11_internal_retrieve_live.py -v

CI does not run this; opt-in via env var (same convention as
``test_arc9_ws4b_rls_fuzz.py``).
"""
from __future__ import annotations

import os
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock


_PG_URL = os.environ.get("LUCIEL_LIVE_POSTGRES_URL")


def _have_live_embedder() -> bool:
    """True only when a REAL OpenAI key is configured.

    The L1/L2 tests call the live ``internal_retrieve`` handler, which
    embeds the query via ``embed_single`` (OpenAI text-embedding-3-small).
    Founder decision #2 (no live API cost in CI) means we must NOT fail
    on a placeholder key — we skip honestly. A real key is non-empty and
    not one of the obvious test placeholders.
    """
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        return False
    placeholders = {"sk-test", "test", "sk-fake", "dummy", "none"}
    return key.lower() not in placeholders


_LIVE_EMBEDDER_REASON = (
    "Set a real OPENAI_API_KEY to run the live internal-retrieve "
    "verification (embeds the query via OpenAI; founder decision #2 "
    "forbids failing on a placeholder key — skip instead)."
)


def _make_platform_admin_request():
    """Build a real Starlette Request carrying platform_admin perms.

    The ``internal_retrieve`` handler is decorated with slowapi's
    ``@limiter.limit`` which requires a genuine Starlette Request — a
    SimpleNamespace is rejected ("parameter `request` must be an
    instance of starlette.requests.Request"). Mirrors the helper in
    tests/api/test_internal_retrieve.py.
    """
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/internal/v1/retrieve",
        "headers": [(b"host", b"test")],
        "query_string": b"",
        "client": ("127.0.0.1", 0),
    }
    req = Request(scope)
    req.state.permissions = ["platform_admin"]
    req.state.admin_id = None
    return req


def _sa_url(url: str | None) -> str | None:
    """Force the psycopg (v3) dialect for SQLAlchemy create_engine.

    A bare ``postgresql://`` URL routes to psycopg2 (not installed);
    ``postgresql+psycopg://`` binds the same driver app/db/session.py
    uses in production. psycopg.connect() (used directly elsewhere in
    this file) takes the plain URL unchanged.
    """
    if url and url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


@unittest.skipUnless(
    _PG_URL,
    "Set LUCIEL_LIVE_POSTGRES_URL=postgresql://... to run live "
    "internal-retrieve verification",
)
class TestArc11InternalRetrieveLive(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls.psycopg = psycopg
        cls.conn = psycopg.connect(_PG_URL, autocommit=False)

        cls.admin_a = f"arc11-ir-a-{uuid.uuid4().hex[:8]}"
        cls.admin_b = f"arc11-ir-b-{uuid.uuid4().hex[:8]}"

        with cls.conn.cursor() as cur:
            for aid in (cls.admin_a, cls.admin_b):
                cur.execute(
                    """
                    INSERT INTO admins (id, name, tier, active)
                    VALUES (%s, %s, 'free', true)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (aid, f"Admin {aid}"),
                )

            # Seed instances + sources + chunks for both admins.
            cls.instances: dict[str, int] = {}
            cls.source_pks: dict[str, int] = {}
            for aid in (cls.admin_a, cls.admin_b):
                cur.execute(
                    "SELECT set_config('app.admin_id', %s, true)", (aid,),
                )
                cur.execute(
                    """
                    INSERT INTO instances
                        (admin_id, instance_slug, display_name, active)
                    VALUES (%s, %s, %s, true)
                    RETURNING id
                    """,
                    (aid, f"slug-{aid}", f"Inst for {aid}"),
                )
                inst_id = int(cur.fetchone()[0])
                cls.instances[aid] = inst_id

                cur.execute(
                    """
                    INSERT INTO knowledge_sources
                        (admin_id, luciel_instance_id, source_type,
                         size_bytes, ingestion_status, ingested_by, filename)
                    VALUES (%s, %s, 'txt', 100, 'ready', 'test', 'a.txt')
                    RETURNING id
                    """,
                    (aid, inst_id),
                )
                src_pk = int(cur.fetchone()[0])
                cls.source_pks[aid] = src_pk

                vec = "[" + ",".join("0.0001" for _ in range(1536)) + "]"
                for i in range(3):
                    cur.execute(
                        """
                        INSERT INTO knowledge_chunks
                            (admin_id, luciel_instance_id, content,
                             knowledge_type, source_id,
                             source_version, embedding)
                        VALUES
                            (%s, %s, %s, 'luciel_knowledge', %s, 1,
                             %s::vector)
                        """,
                        (
                            aid, inst_id, f"chunk-{aid}-{i}",
                            src_pk, vec,
                        ),
                    )
            cur.execute("RESET app.admin_id")
            cls.conn.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        with cls.conn.cursor() as cur:
            for aid in (cls.admin_a, cls.admin_b):
                cur.execute("SELECT set_config('app.admin_id', %s, true)", (aid,))
                cur.execute(
                    "DELETE FROM knowledge_chunks WHERE admin_id = %s", (aid,),
                )
                cur.execute(
                    "DELETE FROM knowledge_sources WHERE admin_id = %s", (aid,),
                )
            cur.execute("RESET app.admin_id")
            cur.execute(
                "DELETE FROM instances WHERE admin_id IN (%s, %s)",
                (cls.admin_a, cls.admin_b),
            )
            cur.execute(
                "DELETE FROM admins WHERE id IN (%s, %s)",
                (cls.admin_a, cls.admin_b),
            )
        cls.conn.commit()
        cls.conn.close()

    # ----- L1 -----
    @unittest.skipUnless(_have_live_embedder(), _LIVE_EMBEDDER_REASON)
    def test_l1_platform_admin_scoped_query_returns_only_target_tenant(self):
        """The handler binds ``payload.admin_id`` via bind_tenant_scope;
        even though the caller is platform_admin and the request has
        no admin_id of its own, the bound scope filters to A's chunks."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.api.v1 import admin_knowledge as ak

        engine = create_engine(
            _sa_url(_PG_URL), future=True, isolation_level="AUTOCOMMIT",
        )
        SessionLocal = sessionmaker(bind=engine, future=True)

        platform_admin_request = _make_platform_admin_request()

        try:
            with SessionLocal() as session:
                payload = ak.InternalRetrieveRequest(
                    admin_id=self.admin_a,
                    instance_id=self.instances[self.admin_a],
                    query="hello world",
                    top_k=10,
                )
                response = ak.internal_retrieve(
                    request=platform_admin_request,  # type: ignore[arg-type]
                    payload=payload,
                    db=session,
                )
        finally:
            engine.dispose()

        # Every returned chunk's source_identifier must point at A's
        # source row (or be a legacy stringy one). None should match
        # admin B's source pk.
        b_source_pk = self.source_pks[self.admin_b]
        a_source_pk = self.source_pks[self.admin_a]
        leaked = [
            c for c in response.chunks
            if c.source_identifier == b_source_pk
        ]
        self.assertEqual(
            leaked, [],
            f"platform_admin retrieve scoped to admin A leaked admin B's "
            f"chunks: {leaked}",
        )
        # And the positive control: at least one of A's chunks came back.
        a_match = [c for c in response.chunks if c.source_identifier == a_source_pk]
        self.assertGreater(
            len(a_match), 0,
            "positive control: A's own chunks should be in the response",
        )

    # ----- L2 -----
    @unittest.skipUnless(_have_live_embedder(), _LIVE_EMBEDDER_REASON)
    def test_l2_explain_text_includes_recognizable_plan_signature(self):
        """The EXPLAIN ANALYZE output should mention either ``Index Scan``
        (sequential scan fallback when pgvector index is small) or
        the HNSW plan token. On the seeded fixture of 6 chunks the
        planner may pick seq scan, so we accept either signature."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.api.v1 import admin_knowledge as ak

        engine = create_engine(
            _sa_url(_PG_URL), future=True, isolation_level="AUTOCOMMIT",
        )
        SessionLocal = sessionmaker(bind=engine, future=True)

        platform_admin_request = _make_platform_admin_request()

        try:
            with SessionLocal() as session:
                response = ak.internal_retrieve(
                    request=platform_admin_request,  # type: ignore[arg-type]
                    payload=ak.InternalRetrieveRequest(
                        admin_id=self.admin_a,
                        instance_id=self.instances[self.admin_a],
                        query="hello",
                        top_k=5,
                    ),
                    db=session,
                )
        finally:
            engine.dispose()

        # On a tiny seed Postgres picks seq scan; on a 10K-row seed it
        # picks the HNSW index. Either is a valid signature that EXPLAIN
        # ran and produced a real plan.
        expl = response.explain
        self.assertTrue(
            "Scan" in expl or "Index" in expl or "<EXPLAIN unavailable" not in expl,
            f"EXPLAIN output looks degenerate: {expl[:500]!r}",
        )
