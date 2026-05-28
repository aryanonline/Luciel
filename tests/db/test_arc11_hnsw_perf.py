"""Arc 11 Step 4 — HNSW perf smoke (Pillar 2 §8.2 bullet).

The plan does not promise a benchmark; it promises a *smoke test*:

  > HNSW vs seq-scan: insert 10K synthetic chunks, compare
  > ``EXPLAIN ANALYZE`` runtime for HNSW vs ``SET enable_indexscan=off``.

The point is to prove the index is being used and helps. The
threshold is generous (HNSW at least 5x faster than seq scan on
10K rows) so noisy environments + small synthetic datasets do not
produce false-positive failures.

How to run
----------

    LUCIEL_LIVE_POSTGRES_URL=postgresql://postgres@127.0.0.1:5433/postgres \\
        python -m pytest tests/db/test_arc11_hnsw_perf.py -v

The test inserts 10K rows. On a local pgvector instance that takes
a few seconds. CI does not run this. The test is opt-in by env
var (matching ``test_arc9_ws4b_rls_fuzz.py``) AND additionally
gated behind ``pytest -m slow`` so a local DB user can quickly
skip it during iteration.
"""
from __future__ import annotations

import math
import os
import random
import time
import unittest
import uuid


_PG_URL = os.environ.get("LUCIEL_LIVE_POSTGRES_URL")

# Number of synthetic rows. The plan calls for 10K. Lower this to
# 1K for fast local iteration via env var if you must.
_N_ROWS = int(os.environ.get("ARC11_HNSW_PERF_N", "10000"))

# Minimum HNSW vs seq-scan speedup to assert. 5x is the plan's
# generous threshold. On a real prod-shaped dataset the gap is
# 10–50x; the lower bar keeps the test stable on cheap CI hardware.
_MIN_SPEEDUP = float(os.environ.get("ARC11_HNSW_MIN_SPEEDUP", "5.0"))

# pgvector 0.5+ supports 1536-dim vectors directly.
_DIMS = 1536


def _random_unit_vector(rng: random.Random, dims: int = _DIMS) -> list[float]:
    """A pseudo-random unit vector. Cosine distance depends on
    direction, not magnitude, but most embedders return roughly-
    normalised outputs so we mirror that."""
    raw = [rng.gauss(0.0, 1.0) for _ in range(dims)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


def _vector_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


@unittest.skipUnless(
    _PG_URL,
    "Set LUCIEL_LIVE_POSTGRES_URL=postgresql://... to run live HNSW perf",
)
@unittest.skipUnless(
    os.environ.get("ARC11_RUN_PERF") == "1",
    "Set ARC11_RUN_PERF=1 to opt into the 10K-row HNSW smoke; "
    "the seed phase takes a few seconds.",
)
class TestArc11HnswPerf(unittest.TestCase):
    """HNSW index smoke test on knowledge_chunks.embedding."""

    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls.psycopg = psycopg
        cls.conn = psycopg.connect(_PG_URL, autocommit=False)

        # Sandbox tenant.
        cls.admin_id = f"arc11-hnsw-{uuid.uuid4().hex[:8]}"
        cls.instance_id: int | None = None

        rng = random.Random(0xC0FFEE)

        with cls.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO admins (id, email, tier, active)
                VALUES (%s, %s, 'free', true)
                ON CONFLICT (id) DO NOTHING
                """,
                (cls.admin_id, f"{cls.admin_id}@example.test"),
            )
            cur.execute(
                """
                INSERT INTO instances
                    (admin_id, instance_slug, display_name, active)
                VALUES (%s, %s, %s, true)
                RETURNING id
                """,
                (cls.admin_id, f"slug-{cls.admin_id}", "perf"),
            )
            cls.instance_id = int(cur.fetchone()[0])

            # GUC the tenant so the RESTRICTIVE policy on
            # knowledge_sources (Step 4 d1) lets the seed insert
            # land. Same for the chunk policy.
            cur.execute("SET LOCAL app.admin_id = %s", (cls.admin_id,))

            cur.execute(
                """
                INSERT INTO knowledge_sources
                    (admin_id, luciel_instance_id, source_type,
                     size_bytes, ingestion_status, ingested_by, filename)
                VALUES (%s, %s, 'txt', 1, 'ready', 'perf', 'perf.txt')
                RETURNING id
                """,
                (cls.admin_id, cls.instance_id),
            )
            cls.source_id = int(cur.fetchone()[0])

            # Batch-insert N_ROWS chunks. Single COPY would be
            # faster, but psycopg's COPY API requires us to format
            # vector literals correctly; the row-at-a-time INSERT is
            # fine at 10K — pgvector ingest is index-build-bound,
            # not row-insert-bound.
            for i in range(_N_ROWS):
                vec = _random_unit_vector(rng)
                cur.execute(
                    """
                    INSERT INTO knowledge_chunks
                        (admin_id, luciel_instance_id, content,
                         knowledge_type, source_id, source_fk,
                         source_version, embedding)
                    VALUES
                        (%s, %s, %s, 'luciel_knowledge', %s, %s, 1, %s::vector)
                    """,
                    (
                        cls.admin_id,
                        cls.instance_id,
                        f"row-{i}",
                        f"src-{cls.source_id}",
                        cls.source_id,
                        _vector_literal(vec),
                    ),
                )
            cls.query_vec = _random_unit_vector(rng)
            cls.conn.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        with cls.conn.cursor() as cur:
            cur.execute("SET LOCAL app.admin_id = %s", (cls.admin_id,))
            cur.execute(
                "DELETE FROM knowledge_chunks WHERE admin_id = %s",
                (cls.admin_id,),
            )
            cur.execute(
                "DELETE FROM knowledge_sources WHERE admin_id = %s",
                (cls.admin_id,),
            )
            cur.execute("RESET app.admin_id")
            cur.execute(
                "DELETE FROM instances WHERE id = %s",
                (cls.instance_id,),
            )
            cur.execute("DELETE FROM admins WHERE id = %s", (cls.admin_id,))
        cls.conn.commit()
        cls.conn.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _time_query(self, *, hnsw_on: bool) -> float:
        """Run the nearest-neighbour query once and return the
        wall-clock time in seconds. We do not use EXPLAIN ANALYZE
        directly because parsing the plan text across pgvector
        versions is brittle; wall clock is the smoke-test surface
        that actually matters."""
        with self.conn.cursor() as cur:
            cur.execute("SET LOCAL app.admin_id = %s", (self.admin_id,))
            if not hnsw_on:
                # Force a sequential scan by disabling the planner's
                # index access methods. ``enable_indexscan=off``
                # alone is not enough — bitmap scans + index-only
                # scans must also be disabled to force the seq scan.
                cur.execute("SET LOCAL enable_indexscan = off")
                cur.execute("SET LOCAL enable_bitmapscan = off")
                cur.execute("SET LOCAL enable_indexonlyscan = off")
            vec_lit = _vector_literal(self.query_vec)
            sql = (
                "SELECT id "
                "FROM knowledge_chunks "
                "WHERE admin_id = %s "
                "ORDER BY embedding <=> %s::vector "
                "LIMIT 5"
            )
            # Warm-up. First run on a cold buffer pool is noisy.
            cur.execute(sql, (self.admin_id, vec_lit))
            cur.fetchall()

            t0 = time.perf_counter()
            cur.execute(sql, (self.admin_id, vec_lit))
            cur.fetchall()
            return time.perf_counter() - t0

    # ------------------------------------------------------------------
    # The test
    # ------------------------------------------------------------------

    def test_hnsw_is_meaningfully_faster_than_seq_scan(self):
        hnsw_secs = self._time_query(hnsw_on=True)
        seq_secs = self._time_query(hnsw_on=False)
        speedup = seq_secs / max(hnsw_secs, 1e-9)
        # Surface the numbers in the assert message; on a failure
        # the operator wants to see them.
        self.assertGreater(
            speedup, _MIN_SPEEDUP,
            f"HNSW perf smoke failed: hnsw={hnsw_secs:.4f}s "
            f"seq={seq_secs:.4f}s speedup={speedup:.2f}x "
            f"(threshold {_MIN_SPEEDUP}x). Verify the HNSW index "
            f"is present (\\d+ ix_knowledge_chunks_embedding_hnsw) "
            f"and that ANALYZE has been run since seed.",
        )

    def test_hnsw_index_present_on_chunks_table(self):
        """Sanity: the HNSW index actually exists on the table."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT indexname
                  FROM pg_indexes
                 WHERE schemaname = 'public'
                   AND tablename = 'knowledge_chunks'
                   AND indexname = 'ix_knowledge_chunks_embedding_hnsw'
                """
            )
            row = cur.fetchone()
        self.assertIsNotNone(
            row,
            "ix_knowledge_chunks_embedding_hnsw missing. Did "
            "arc11_d3_hnsw_index_chunks run? `alembic upgrade head`.",
        )
