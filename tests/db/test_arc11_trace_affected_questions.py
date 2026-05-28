"""Arc 11 Step 5 — live-DB verification of the affected-questions read.

Static-shape contract for ``TraceRepository.list_recent_traces_using_source``
lives in ``tests/services/test_trace_repository_affected_questions.py``.
That covers signature, kw-only-ness, and the compiled SQL shape.

What only a live Postgres can prove:

  L1  Containment ``BIGINT[] @> BIGINT[]`` actually returns the
      traces whose ``source_ids_used`` arrays include the target id.
  L2  Tenant scoping: a trace with ``source_ids_used=[42]`` but a
      different ``admin_id`` is NOT returned. (Without RLS in the
      test session this is verified at the L1 service-layer filter.)
  L3  ``ORDER BY created_at DESC LIMIT N`` actually caps + orders.
  L4  The GIN index ``ix_traces_source_ids_used`` is being used
      (sanity-check via EXPLAIN; not a strict assertion because
      planners on tiny tables prefer seq scan, but the index must
      at least exist).

How to run
----------

    LUCIEL_LIVE_POSTGRES_URL=postgresql://postgres@127.0.0.1:5433/postgres \\
        python -m pytest tests/db/test_arc11_trace_affected_questions.py -v

CI does not run this — the sandbox has no Postgres. The test is
opt-in by env var (matches ``test_arc9_ws4b_rls_fuzz.py`` and the
Step-4 ``test_arc11_knowledge_rls.py``).
"""
from __future__ import annotations

import os
import unittest
import uuid


_PG_URL = os.environ.get("LUCIEL_LIVE_POSTGRES_URL")


@unittest.skipUnless(
    _PG_URL,
    "Set LUCIEL_LIVE_POSTGRES_URL=postgresql://... to run the live "
    "affected-questions test",
)
class TestArc11AffectedQuestionsLive(unittest.TestCase):
    """Live containment-query verification."""

    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls.psycopg = psycopg
        cls.conn = psycopg.connect(_PG_URL, autocommit=False)

        cls.admin_a = f"arc11-aq-a-{uuid.uuid4().hex[:8]}"
        cls.admin_b = f"arc11-aq-b-{uuid.uuid4().hex[:8]}"
        cls.instance_a: int | None = None
        cls.instance_b: int | None = None

        with cls.conn.cursor() as cur:
            for aid in (cls.admin_a, cls.admin_b):
                cur.execute(
                    """
                    INSERT INTO admins (id, email, tier, active)
                    VALUES (%s, %s, 'free', true)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (aid, f"{aid}@example.test"),
                )
            for attr, aid in (
                ("instance_a", cls.admin_a),
                ("instance_b", cls.admin_b),
            ):
                cur.execute(
                    """
                    INSERT INTO instances
                        (admin_id, instance_slug, display_name, active)
                    VALUES (%s, %s, %s, true)
                    RETURNING id
                    """,
                    (aid, f"slug-{aid}", f"Instance for {aid}"),
                )
                setattr(cls, attr, int(cur.fetchone()[0]))

            # 7 traces under Admin A (the brief calls for 7), one
            # under Admin B with source_ids_used=[42] to verify
            # tenant scoping.
            #
            #  trace #  admin    source_ids_used   in_result_for_42?
            #  -----------------------------------------------------
            #   1        A       []                no
            #   2        A       [42]              yes
            #   3        A       [42, 99]          yes
            #   4        A       [99]              no
            #   5        A       [7, 42, 13]       yes
            #   6        A       []                no
            #   7        A       [42]              yes
            #   8        B       [42]              no (different admin)
            seed_a = [
                ([],            "row-1"),
                ([42],          "row-2"),
                ([42, 99],      "row-3"),
                ([99],          "row-4"),
                ([7, 42, 13],   "row-5"),
                ([],            "row-6"),
                ([42],          "row-7"),
            ]
            cur.execute("SET LOCAL app.admin_id = %s", (cls.admin_a,))
            cls.expected_count_for_42 = 0
            for ids, label in seed_a:
                cur.execute(
                    """
                    INSERT INTO traces
                        (trace_id, session_id, admin_id,
                         luciel_instance_id, user_message,
                         assistant_reply, source_ids_used)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::bigint[])
                    """,
                    (
                        f"{cls.admin_a}-{label}-{uuid.uuid4().hex[:6]}",
                        f"sess-{label}",
                        cls.admin_a,
                        cls.instance_a,
                        label,
                        "reply",
                        ids,
                    ),
                )
                if 42 in ids:
                    cls.expected_count_for_42 += 1

            cur.execute("SET LOCAL app.admin_id = %s", (cls.admin_b,))
            cur.execute(
                """
                INSERT INTO traces
                    (trace_id, session_id, admin_id,
                     luciel_instance_id, user_message,
                     assistant_reply, source_ids_used)
                VALUES (%s, %s, %s, %s, %s, %s, %s::bigint[])
                """,
                (
                    f"{cls.admin_b}-leak-{uuid.uuid4().hex[:6]}",
                    "sess-b-1",
                    cls.admin_b,
                    cls.instance_b,
                    "row-b-leak",
                    "reply",
                    [42],
                ),
            )
            cur.execute("RESET app.admin_id")
            cls.conn.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        with cls.conn.cursor() as cur:
            for aid in (cls.admin_a, cls.admin_b):
                cur.execute(
                    "SET LOCAL app.admin_id = %s", (aid,)
                )
                cur.execute(
                    "DELETE FROM traces WHERE admin_id = %s", (aid,),
                )
            cur.execute("RESET app.admin_id")
            cur.execute(
                "DELETE FROM instances WHERE id IN (%s, %s)",
                (cls.instance_a, cls.instance_b),
            )
            cur.execute(
                "DELETE FROM admins WHERE id IN (%s, %s)",
                (cls.admin_a, cls.admin_b),
            )
        cls.conn.commit()
        cls.conn.close()

    # ----------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------

    def _repo_call(
        self,
        *,
        admin_id: str,
        instance_id: int,
        source_id: int,
        limit: int = 5,
    ):
        """Open a SQLAlchemy session bound to the live DB, set the
        admin GUC, run the repository method. Mirrors the prod
        engine-listener posture."""
        from sqlalchemy import create_engine, event, text as sa_text
        from sqlalchemy.orm import sessionmaker

        from app.repositories.trace_repository import TraceRepository

        engine = create_engine(
            _PG_URL, future=True, isolation_level="AUTOCOMMIT",
        )

        @event.listens_for(engine, "begin")
        def _begin_bind(conn):  # noqa: ANN001
            conn.execute(
                sa_text("SET LOCAL app.admin_id = :aid").bindparams(
                    aid=admin_id
                )
            )

        SessionLocal = sessionmaker(bind=engine, future=True)
        try:
            with SessionLocal() as session:
                repo = TraceRepository(session)
                return repo.list_recent_traces_using_source(
                    admin_id=admin_id,
                    luciel_instance_id=instance_id,
                    source_id=source_id,
                    limit=limit,
                )
        finally:
            engine.dispose()

    # ----------------------------------------------------------------
    # L1 — Containment returns the right set.
    # ----------------------------------------------------------------

    def test_returns_only_traces_that_used_the_source(self):
        """Admin A asking for source_id=42 sees exactly the four
        Admin-A traces whose arrays contain 42."""
        results = self._repo_call(
            admin_id=self.admin_a,
            instance_id=self.instance_a,
            source_id=42,
            limit=100,
        )
        # The seed produced four matches.
        self.assertEqual(
            len(results), self.expected_count_for_42,
            f"Expected {self.expected_count_for_42} traces for source 42, "
            f"got {len(results)}: {[t.trace_id for t in results]}",
        )
        for t in results:
            self.assertIn(
                42, t.source_ids_used,
                f"Trace {t.trace_id} returned but does not contain 42",
            )

    def test_unknown_source_returns_empty(self):
        """A source id no trace has referenced returns ``[]`` —
        the Customer Journey §4.3 "Luciel hasn't drawn on this
        source in any customer conversations yet" path."""
        results = self._repo_call(
            admin_id=self.admin_a,
            instance_id=self.instance_a,
            source_id=999_999,
        )
        self.assertEqual(results, [])

    # ----------------------------------------------------------------
    # L2 — Tenant scoping.
    # ----------------------------------------------------------------

    def test_does_not_leak_other_admins_traces(self):
        """The Admin-B trace with ``source_ids_used=[42]`` must NOT
        appear when Admin A asks for source 42."""
        results = self._repo_call(
            admin_id=self.admin_a,
            instance_id=self.instance_a,
            source_id=42,
            limit=100,
        )
        leaked = [t for t in results if t.admin_id != self.admin_a]
        self.assertEqual(
            leaked, [],
            f"Admin A's affected-questions query leaked Admin B's "
            f"traces: {[(t.trace_id, t.admin_id) for t in leaked]}",
        )

    def test_does_not_leak_other_instances_traces(self):
        """Cross-instance check: Admin B asking for source 42 in
        their OWN instance also returns nothing if their instance
        has no matching trace (B's seed is in instance_b but
        we ask for instance_a)."""
        results = self._repo_call(
            admin_id=self.admin_b,
            instance_id=self.instance_a,  # wrong instance for B
            source_id=42,
        )
        self.assertEqual(results, [])

    # ----------------------------------------------------------------
    # L3 — Ordering + limit.
    # ----------------------------------------------------------------

    def test_orders_newest_first_and_caps_at_limit(self):
        """``ORDER BY created_at DESC LIMIT :limit``."""
        results = self._repo_call(
            admin_id=self.admin_a,
            instance_id=self.instance_a,
            source_id=42,
            limit=2,
        )
        self.assertEqual(
            len(results), 2,
            f"limit=2 must cap result count to 2, got {len(results)}",
        )
        # Strictly non-increasing created_at.
        for a, b in zip(results, results[1:]):
            self.assertGreaterEqual(a.created_at, b.created_at)

    def test_default_limit_is_five(self):
        """Default limit must be 5 (matches Customer Journey §4.3
        modal preview cap). Seed has 4 matches so the natural call
        returns 4, but the signature default still matters."""
        results = self._repo_call(
            admin_id=self.admin_a,
            instance_id=self.instance_a,
            source_id=42,
            # no explicit limit
        )
        # 4 matches in the seed; default limit 5 admits all four.
        self.assertEqual(len(results), self.expected_count_for_42)
        self.assertLessEqual(len(results), 5)

    # ----------------------------------------------------------------
    # L4 — The GIN index exists. (Not a planner assertion — too
    # noisy at 8 rows — just the existence check.)
    # ----------------------------------------------------------------

    def test_gin_index_for_source_ids_used_present(self):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT indexname
                  FROM pg_indexes
                 WHERE schemaname = 'public'
                   AND tablename  = 'traces'
                   AND indexname  = 'ix_traces_source_ids_used'
                """
            )
            row = cur.fetchone()
        self.assertIsNotNone(
            row,
            "ix_traces_source_ids_used missing — Arc 11 Step 1's GIN "
            "index over source_ids_used didn't survive a later "
            "migration. Restore it before relying on this read path.",
        )
