"""Arc 11 Step 4 — live RLS verification for the two-table knowledge model.

This is the cross-tenant denial test for the new ``knowledge_sources``
table and the post-rename ``knowledge_chunks`` table, plus the
critical JOIN-RLS check the Step 3 carry-forward
(``ARC11_PLAN.md §13``) flagged.

Why live Postgres
-----------------

The shape tests in ``tests/security/test_arc11_rls_migrations_shape.py``
verify the migration body. They prove the RIGHT SQL is generated.
They cannot prove that PostgreSQL actually enforces it: RLS is a
runtime feature; the planner applies USING/WITH CHECK against real
values; the connection's ``current_setting('app.admin_id', true)``
GUC is what gates the fence. The only way to verify "Admin B cannot
see Admin A's chunks through the retriever's JOIN" is to talk to a
real Postgres with pgvector + RLS forced.

JOIN-RLS specifically
---------------------

Step 3 of Arc 11 made the retriever's ``search_similar`` do a
``LEFT OUTER JOIN knowledge_sources`` to enforce the
``ingestion_status='ready'`` gate. The carry-forward note in
``ARC11_PLAN.md §13`` warned that if the chunk-side admin filter
were applied *after* the join, an Admin-B chunk pointing at an
Admin-A source row could leak (the join would yield the row;
Admin-A's source RLS would still permit it under Admin-A's GUC).

The way Postgres enforces RLS makes this hard to mis-arrange: the
chunk-side RLS predicate is pushed down into the chunk scan,
*before* the join. So even if the SQL textually orders the join
before the WHERE, the planner applies RLS to the chunk scan first.
This test PROVES that — by issuing the actual retriever query as
the prod app role under Admin B's GUC, with chunks belonging to
Admin A present in the table, and asserting the result set is
empty.

If this test ever starts returning A's chunks under B's GUC, the
fix is to switch ``search_similar`` to an EXISTS-subquery form
that makes the admin filter syntactically inseparable from the
chunk scan. See the §13 carry-forward note for the suggested
shape.

How to run
----------

    LUCIEL_LIVE_POSTGRES_URL=postgresql://postgres@127.0.0.1:5433/postgres \\
        python -m pytest tests/db/test_arc11_knowledge_rls.py -v

CI does not run this — the sandbox has no Postgres. The test is
opt-in by env var so import collection works in stock CI (matches
``test_arc9_ws4b_rls_fuzz.py`` convention).

Prerequisites at the URL
------------------------

  * Postgres ≥ 14 with the ``vector`` extension installed.
  * Alembic has been upgraded to head (so the new RLS policies and
    HNSW index are in place; tables exist; etc.).
  * The URL connects as a role that owns the tables (or has
    SELECT+INSERT on them) for the *setup* phase. The test creates
    an ephemeral non-superuser role for the actual RLS-fenced
    queries — matching the prod ``luciel_app`` posture.
"""
from __future__ import annotations

import os
import unittest
import uuid


_PG_URL = os.environ.get("LUCIEL_LIVE_POSTGRES_URL")


@unittest.skipUnless(
    _PG_URL,
    "Set LUCIEL_LIVE_POSTGRES_URL=postgresql://... to run live RLS tests",
)
class TestArc11KnowledgeRls(unittest.TestCase):
    """Live cross-tenant denial + JOIN-RLS verification."""

    @classmethod
    def setUpClass(cls) -> None:
        import psycopg
        from psycopg import sql as pgsql

        cls.psycopg = psycopg
        cls.pgsql = pgsql

        # Admin connection: owner role / superuser; used for setup
        # and teardown.
        cls.admin_conn = psycopg.connect(_PG_URL, autocommit=True)

        # Two ephemeral admin IDs + instance ids. Random-suffixed so
        # parallel runs don't collide and a partial-failure leaves
        # cleanup-friendly residue.
        cls.admin_a = f"arc11-rls-a-{uuid.uuid4().hex[:8]}"
        cls.admin_b = f"arc11-rls-b-{uuid.uuid4().hex[:8]}"
        cls.instance_a: int | None = None
        cls.instance_b: int | None = None

        # Ephemeral non-superuser app role mirroring prod's luciel_app.
        cls.app_role = f"luciel_app_arc11_rls_{uuid.uuid4().hex[:8]}"
        cls.app_password = uuid.uuid4().hex

        with cls.admin_conn.cursor() as cur:
            # Create the two admin rows. ``admins.id`` is the FK
            # target for knowledge_sources.admin_id and
            # knowledge_chunks.admin_id.
            for aid in (cls.admin_a, cls.admin_b):
                cur.execute(
                    """
                    INSERT INTO admins (id, email, tier, active)
                    VALUES (%s, %s, 'free', true)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (aid, f"{aid}@example.test"),
                )

            # Create one instance per admin.
            for aid_attr, aid in (
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
                row = cur.fetchone()
                setattr(cls, aid_attr, int(row[0]))

            # Create the ephemeral app role. NOBYPASSRLS is the
            # defining attribute; default privileges from Arc 9
            # C10.b grant CRUD on every table including the new
            # knowledge_sources.
            cur.execute(
                pgsql.SQL(
                    "CREATE ROLE {role} LOGIN PASSWORD {pw} NOBYPASSRLS"
                ).format(
                    role=pgsql.Identifier(cls.app_role),
                    pw=pgsql.Literal(cls.app_password),
                )
            )
            cur.execute(
                pgsql.SQL("GRANT USAGE ON SCHEMA public TO {role}").format(
                    role=pgsql.Identifier(cls.app_role),
                )
            )
            for tbl in ("knowledge_sources", "knowledge_chunks"):
                cur.execute(
                    pgsql.SQL(
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON {tbl} TO {role}"
                    ).format(
                        tbl=pgsql.Identifier(tbl),
                        role=pgsql.Identifier(cls.app_role),
                    )
                )
            cur.execute(
                pgsql.SQL(
                    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}"
                ).format(role=pgsql.Identifier(cls.app_role))
            )

        # Build the app-role connection URL.
        from urllib.parse import urlparse, urlunparse

        parts = urlparse(_PG_URL)
        netloc = f"{cls.app_role}:{cls.app_password}@{parts.hostname}"
        if parts.port:
            netloc += f":{parts.port}"
        cls.app_url = urlunparse(parts._replace(netloc=netloc))

        # Seed: one source + N chunks under Admin A. Done as the
        # OWNER (admin_conn) because admin_conn can bypass FORCE on
        # the GUC-aware policy by setting the GUC.
        cls.source_a_id: int | None = None
        cls.chunk_a_ids: list[int] = []

        with cls.admin_conn.cursor() as cur:
            cur.execute("SET LOCAL app.admin_id = %s", (cls.admin_a,))
            cur.execute(
                """
                INSERT INTO knowledge_sources
                    (admin_id, luciel_instance_id, source_type, size_bytes,
                     ingestion_status, ingested_by, filename)
                VALUES (%s, %s, 'txt', 1234, 'ready', 'test', 'a.txt')
                RETURNING id
                """,
                (cls.admin_a, cls.instance_a),
            )
            cls.source_a_id = int(cur.fetchone()[0])

            # Insert three chunks. Use raw SQL because the pgvector
            # column requires a vector literal; we synthesise a
            # 1536-dim zero vector with a tiny perturbation to keep
            # the row valid.
            vec_lit = (
                "[" + ",".join("0.0001" for _ in range(1536)) + "]"
            )
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
                    RETURNING id
                    """,
                    (
                        cls.admin_a,
                        cls.instance_a,
                        f"chunk content {i}",
                        cls.source_a_id,
                        vec_lit,
                    ),
                )
                cls.chunk_a_ids.append(int(cur.fetchone()[0]))

    @classmethod
    def tearDownClass(cls) -> None:
        from psycopg import sql as pgsql

        with cls.admin_conn.cursor() as cur:
            # Drop our chunks + source. Admin GUC so RLS lets us
            # touch our own rows.
            cur.execute("SET LOCAL app.admin_id = %s", (cls.admin_a,))
            cur.execute(
                "DELETE FROM knowledge_chunks WHERE admin_id = %s",
                (cls.admin_a,),
            )
            cur.execute(
                "DELETE FROM knowledge_sources WHERE admin_id = %s",
                (cls.admin_a,),
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
            try:
                cur.execute(
                    pgsql.SQL("DROP OWNED BY {role}").format(
                        role=pgsql.Identifier(cls.app_role),
                    )
                )
            except Exception:
                pass
            cur.execute(
                pgsql.SQL("DROP ROLE IF EXISTS {role}").format(
                    role=pgsql.Identifier(cls.app_role),
                )
            )
        cls.admin_conn.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _app_conn(self):
        """Open a connection as the ephemeral non-superuser app role.

        autocommit=False so SET LOCAL inside a transaction works the
        way the prod engine listener does (Arc 9 C2/C4.1 doctrine).
        """
        return self.psycopg.connect(self.app_url, autocommit=False)

    # ------------------------------------------------------------------
    # Pre-flight sanity: schema state.
    # ------------------------------------------------------------------

    def test_pre_flight_rls_forced_on_both_tables(self):
        """Sanity: FORCE RLS is in place on both tables. Without
        FORCE, the owner role bypasses RLS and every test below is
        a false negative."""
        with self.admin_conn.cursor() as cur:
            cur.execute(
                """
                SELECT relname, relrowsecurity, relforcerowsecurity
                  FROM pg_class
                 WHERE relname IN ('knowledge_sources', 'knowledge_chunks')
                   AND relkind = 'r'
                """
            )
            rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        for tbl in ("knowledge_sources", "knowledge_chunks"):
            self.assertIn(tbl, rows, f"{tbl} not in pg_class")
            enabled, forced = rows[tbl]
            self.assertTrue(enabled, f"{tbl}: RLS not enabled")
            self.assertTrue(forced, f"{tbl}: RLS not FORCED")

    def test_pre_flight_admin_isolation_policies_exist(self):
        """The expected policy names from Step 4 are installed."""
        with self.admin_conn.cursor() as cur:
            cur.execute(
                """
                SELECT polname, tablename
                  FROM pg_policies
                 WHERE schemaname = 'public'
                   AND tablename IN ('knowledge_sources', 'knowledge_chunks')
                """
            )
            polmap: dict[str, set[str]] = {}
            for polname, table in cur.fetchall():
                polmap.setdefault(table, set()).add(polname)

        self.assertIn("knowledge_sources", polmap)
        self.assertIn(
            "knowledge_sources_admin_isolation",
            polmap["knowledge_sources"],
        )
        self.assertIn(
            "knowledge_sources_admin_isolation_write",
            polmap["knowledge_sources"],
        )

        # Post-rename audit: no policy on knowledge_chunks should
        # still carry the legacy ``knowledge_embeddings_`` prefix.
        self.assertIn("knowledge_chunks", polmap)
        stale = {
            p for p in polmap["knowledge_chunks"]
            if p.startswith("knowledge_embeddings_")
        }
        self.assertSetEqual(
            stale, set(),
            f"Stale knowledge_embeddings_* policies on chunks: {stale}",
        )

    # ------------------------------------------------------------------
    # Cross-tenant denial — direct SELECTs.
    # ------------------------------------------------------------------

    def test_admin_b_sees_no_knowledge_sources(self):
        """Direct SELECT under Admin B's GUC returns no Admin-A rows."""
        with self._app_conn() as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL app.admin_id = %s", (self.admin_b,))
            cur.execute(
                "SELECT id FROM knowledge_sources WHERE admin_id = %s",
                (self.admin_a,),
            )
            rows = cur.fetchall()
        self.assertEqual(
            rows, [],
            "Admin B saw Admin A's knowledge_sources rows — RLS leak",
        )

    def test_admin_b_sees_no_knowledge_chunks(self):
        """Direct SELECT under Admin B's GUC returns no Admin-A chunks."""
        with self._app_conn() as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL app.admin_id = %s", (self.admin_b,))
            cur.execute(
                "SELECT id FROM knowledge_chunks WHERE admin_id = %s",
                (self.admin_a,),
            )
            rows = cur.fetchall()
        self.assertEqual(
            rows, [],
            "Admin B saw Admin A's knowledge_chunks rows — RLS leak",
        )

    def test_admin_a_sees_their_own_sources_and_chunks(self):
        """Positive control: Admin A under Admin A's GUC DOES see
        their own rows. Otherwise the "0 rows for B" result would
        be ambiguous between "RLS denied" and "table empty"."""
        with self._app_conn() as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL app.admin_id = %s", (self.admin_a,))
            cur.execute("SELECT id FROM knowledge_sources")
            sources = cur.fetchall()
            cur.execute("SELECT id FROM knowledge_chunks")
            chunks = cur.fetchall()
        self.assertGreaterEqual(len(sources), 1)
        self.assertGreaterEqual(len(chunks), 3)

    # ------------------------------------------------------------------
    # Fail-closed: GUC unset / NULL / RESET.
    # ------------------------------------------------------------------

    def test_no_guc_returns_zero_rows_knowledge_sources(self):
        """No GUC at all — both fail-closed (Arc 9 WS4b doctrine)."""
        with self._app_conn() as conn, conn.cursor() as cur:
            # Do NOT SET app.admin_id.
            cur.execute("SELECT id FROM knowledge_sources")
            rows = cur.fetchall()
        self.assertEqual(rows, [])

    def test_reset_guc_returns_zero_rows_knowledge_sources(self):
        """RESET clears the GUC; reads must still fail-closed."""
        with self._app_conn() as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL app.admin_id = %s", (self.admin_a,))
            cur.execute("RESET app.admin_id")
            cur.execute("SELECT id FROM knowledge_sources")
            rows = cur.fetchall()
        self.assertEqual(rows, [])

    # ------------------------------------------------------------------
    # Write-side denial.
    # ------------------------------------------------------------------

    def test_cannot_insert_other_admins_source(self):
        """Admin A under Admin A's GUC cannot insert a row stamped
        with Admin B's admin_id. The RESTRICTIVE WITH CHECK fence
        denies the write; PG raises ``insufficient_privilege``."""
        with self._app_conn() as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL app.admin_id = %s", (self.admin_a,))
            with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    """
                    INSERT INTO knowledge_sources
                        (admin_id, luciel_instance_id, source_type,
                         size_bytes, ingestion_status, ingested_by)
                    VALUES (%s, %s, 'txt', 1, 'pending', 'leak-attempt')
                    """,
                    (self.admin_b, self.instance_a),
                )
            conn.rollback()

    # ------------------------------------------------------------------
    # JOIN-RLS: the §13 carry-forward critical case.
    # ------------------------------------------------------------------

    def test_retriever_query_no_leak_under_admin_b(self):
        """Issue the actual ``KnowledgeRepository.search_similar``
        query (via the live code path) under Admin B's GUC. With
        Admin A's chunks + source present, B must see zero rows.

        This is the JOIN-RLS test from ARC11_PLAN.md §13. If it
        ever fails — i.e., Admin A's chunks appear in B's result
        set — the chunk-side RLS is being applied AFTER the join,
        and we need the EXISTS-subquery rewrite the carry-forward
        note describes.
        """
        from sqlalchemy import create_engine, event, text as sa_text
        from sqlalchemy.orm import sessionmaker

        from app.repositories.knowledge_repository import (
            KnowledgeRepository,
        )

        engine = create_engine(
            self.app_url, future=True, isolation_level="AUTOCOMMIT",
        )

        # Install a SET-LOCAL listener so every BEGIN binds the GUC.
        # This is the same posture as ``app/db/tenant_context.py``
        # (Arc 9 C2 / C4.1 listener doctrine).
        @event.listens_for(engine, "begin")
        def _begin_bind_admin(conn):  # noqa: ANN001 — SA event sig
            conn.execute(
                sa_text("SET LOCAL app.admin_id = :aid").bindparams(
                    aid=self.admin_b
                )
            )

        Session = sessionmaker(bind=engine, future=True)

        try:
            with Session() as session:
                # Synthesise the same near-zero vector used at seed
                # time so cosine distance is well-defined.
                query_vec = [0.0001] * 1536
                repo = KnowledgeRepository(session)
                results = repo.search_similar(
                    query_embedding=query_vec,
                    admin_id=self.admin_b,
                    domain_id=None,
                    luciel_instance_id=self.instance_b,
                    agent_id=None,
                    knowledge_type=None,
                    limit=10,
                )
        finally:
            engine.dispose()

        # The retriever returns a list of dicts; admin_id on each
        # row MUST equal admin_b or be NULL (legacy platform rows,
        # which are not under test here).
        leaked = [
            r for r in results
            if r.get("admin_id") not in (self.admin_b, None)
        ]
        self.assertEqual(
            leaked, [],
            f"JOIN-RLS leak: Admin B's retriever saw rows for "
            f"other admins: {leaked!r}. The chunk-side RLS is being "
            f"applied AFTER the join. See ARC11_PLAN.md §13.",
        )
        # And in particular: NONE of the seeded Admin-A chunks.
        leaked_ids = [r["id"] for r in results if r["id"] in self.chunk_a_ids]
        self.assertEqual(
            leaked_ids, [],
            f"JOIN-RLS leak: B's retriever saw A's seeded chunk ids: "
            f"{leaked_ids!r}.",
        )

    def test_retriever_query_succeeds_under_admin_a(self):
        """Positive control for the JOIN-RLS test: Admin A's
        retriever under Admin A's GUC DOES see the seeded chunks.
        Otherwise the negative result for B is ambiguous."""
        from sqlalchemy import create_engine, event, text as sa_text
        from sqlalchemy.orm import sessionmaker

        from app.repositories.knowledge_repository import (
            KnowledgeRepository,
        )

        engine = create_engine(
            self.app_url, future=True, isolation_level="AUTOCOMMIT",
        )

        @event.listens_for(engine, "begin")
        def _begin_bind_admin(conn):  # noqa: ANN001
            conn.execute(
                sa_text("SET LOCAL app.admin_id = :aid").bindparams(
                    aid=self.admin_a
                )
            )

        Session = sessionmaker(bind=engine, future=True)
        try:
            with Session() as session:
                repo = KnowledgeRepository(session)
                results = repo.search_similar(
                    query_embedding=[0.0001] * 1536,
                    admin_id=self.admin_a,
                    domain_id=None,
                    luciel_instance_id=self.instance_a,
                    agent_id=None,
                    knowledge_type=None,
                    limit=10,
                )
        finally:
            engine.dispose()

        seen_ids = {r["id"] for r in results}
        for cid in self.chunk_a_ids:
            self.assertIn(
                cid, seen_ids,
                f"Positive control failed: Admin A's retriever did not "
                f"see seeded chunk id={cid}. Investigate before "
                f"trusting the JOIN-RLS denial test.",
            )
