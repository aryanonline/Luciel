"""Arc 9 WS4b -- RLS fuzz: no silent-empty reads when GUC unset.

THE GAP THIS CLOSES
===================
Arc 9 C10.a flipped 17 tenant-scoped tables to FORCE ROW LEVEL SECURITY,
and the Arc 9 C22 corrigendum codified the doctrine:

    "Empty-tier / no-GUC = deny, not silent-empty default."

C9.5 already proves the *mechanics* of RLS on a synthetic table. WS4b
takes the stronger stance the corrigendum requires: against the REAL
production schema (every table named in ALC9_C10_A_FORCE_RLS), as the
NON-superuser ``luciel_app`` role the prod backend connects as:

    1. SELECT with no ``app.admin_id`` GUC set returns 0 rows for every
       FORCE-RLS table (RLS denies; nothing leaks while bootstrap is
       still running).
    2. SELECT with a bogus / unknown ``app.admin_id`` (random uuid that
       owns nothing) also returns 0 rows -- proving the policy evaluator
       actually compares admin_id against the GUC, not just no-ops.
    3. Whichever path the request takes (success, exception, identity
       bootstrap zero-state), we never accidentally serve cross-tenant
       data.

This is the "RLS hardening" gap §8.3 of VANTAGEMIND_VISION-2 listed as
"Not Yet Shipped". WS4b closes it with a runnable assertion.

PROD POSTURE
============
In production the backend connects as ``luciel_app`` (non-superuser,
non-owner, non-BYPASSRLS). This test mirrors that posture by creating
an ephemeral non-superuser role of the same shape and querying as it.
A superuser-only query is included as a sanity check that the table
DOES contain data (so a 0-row result from the app role is "RLS denied",
not "table is empty").

RUN
===
    LUCIEL_LIVE_POSTGRES_URL=postgresql://postgres@127.0.0.1:5433/postgres \\
        python -m pytest tests/db/test_arc9_ws4b_rls_fuzz.py -v

CI does NOT run this -- it has no Postgres. Local + pre-deploy only.
The test is opt-in by env var so import collection works in stock CI.
"""

from __future__ import annotations

import os
import unittest
import uuid


_PG_URL = os.environ.get("LUCIEL_LIVE_POSTGRES_URL")


# Canonical list of 17 FORCE-RLS tenant-scoped tables. MUST stay in
# sync with FORCE_TABLES in alembic/versions/arc9_c10_a_force_rls.py.
# Any new tenant-scoped table added to that migration MUST be added
# here too. This duplication is intentional -- the test is the
# external contract; the migration is the implementation.
_FORCE_RLS_TABLES = (
    "admin_audit_logs",
    "traces",
    "memory_items",
    "conversations",
    "sessions",
    "subscriptions",
    "scope_assignments",
    # Arc 11 renamed knowledge_embeddings -> knowledge_chunks; the
    # stale name here meant this fuzz suite errored at setup and never
    # ran. Corrected to the live table name.
    "knowledge_chunks",
    "api_keys",
    "user_invites",
    "user_consents",
    "identity_claims",
    "instances",
    "admin_widget_domains",
    "retention_policies",
    "deletion_logs",
    "messages",
    # Arc 16: knowledge graph store. Tenant-scoped, FORCE RLS from birth
    # (arc16_c). Listed here so the fuzz invariants (force-RLS set,
    # unset/bogus GUC -> 0 rows) are asserted against them too.
    "knowledge_graph_nodes",
    "knowledge_graph_edges",
)


@unittest.skipUnless(
    _PG_URL,
    "Set LUCIEL_LIVE_POSTGRES_URL=postgresql://... to run live RLS fuzz",
)
class TestArc9WS4bRlsFuzz(unittest.TestCase):
    """Live RLS fuzz: every FORCE-RLS table denies on unset / bogus GUC."""

    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls.psycopg = psycopg
        cls.admin_conn = psycopg.connect(_PG_URL, autocommit=True)

        cls.app_role = f"luciel_app_ws4b_{uuid.uuid4().hex[:8]}"
        cls.app_password = uuid.uuid4().hex

        # Build a non-superuser, non-BYPASSRLS app role -- the same
        # posture as production's luciel_app. We grant it SELECT on
        # all 17 tables so its 0-row results are "RLS denied", not
        # "permission denied".
        from psycopg import sql as pgsql

        with cls.admin_conn.cursor() as cur:
            cur.execute(
                pgsql.SQL("CREATE ROLE {role} LOGIN PASSWORD {pw}").format(
                    role=pgsql.Identifier(cls.app_role),
                    pw=pgsql.Literal(cls.app_password),
                )
            )
            for table in _FORCE_RLS_TABLES:
                cur.execute(
                    pgsql.SQL(
                        "GRANT SELECT ON TABLE {tbl} TO {role}"
                    ).format(
                        tbl=pgsql.Identifier(table),
                        role=pgsql.Identifier(cls.app_role),
                    )
                )

        # Build a connection string for the ephemeral app role.
        from urllib.parse import urlparse, urlunparse

        parts = urlparse(_PG_URL)
        netloc = f"{cls.app_role}:{cls.app_password}@{parts.hostname}"
        if parts.port:
            netloc += f":{parts.port}"
        cls.app_url = urlunparse(parts._replace(netloc=netloc))

    @classmethod
    def tearDownClass(cls) -> None:
        from psycopg import sql as pgsql

        with cls.admin_conn.cursor() as cur:
            # Reassign + drop the role; SELECT grants go with it.
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
        """Open a connection as the ephemeral app role.

        Caller is responsible for closing it. autocommit=False so we
        can SET LOCAL inside a transaction the way the prod engine
        listener does.
        """
        return self.psycopg.connect(self.app_url, autocommit=False)

    def _table_has_rows_via_admin(self, table: str) -> bool:
        """True iff the table contains at least one row (admin view).

        Used to distinguish "RLS denied 0 rows" from "table is empty".
        """
        with self.admin_conn.cursor() as cur:
            cur.execute(f"SELECT EXISTS (SELECT 1 FROM {table})")
            row = cur.fetchone()
            return bool(row[0])

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_force_rls_flag_is_set_on_every_listed_table(self):
        """Sanity: pg_class agrees that every listed table is FORCE RLS.

        If this test fails, the WS4b table list has drifted from the
        actual schema -- update _FORCE_RLS_TABLES to match
        alembic/versions/arc9_c10_a_force_rls.py before investigating
        the per-table fuzz failures.
        """
        with self.admin_conn.cursor() as cur:
            cur.execute(
                """
                SELECT relname, relrowsecurity, relforcerowsecurity
                FROM pg_class
                WHERE relname = ANY(%s)
                  AND relkind = 'r'
                """,
                ([t for t in _FORCE_RLS_TABLES],),
            )
            rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

        missing = [t for t in _FORCE_RLS_TABLES if t not in rows]
        self.assertFalse(
            missing,
            f"FORCE-RLS tables missing from schema: {missing}. "
            f"Either the migration didn't run, or the WS4b table list "
            f"is stale.",
        )

        not_enabled = [t for t in _FORCE_RLS_TABLES if not rows[t][0]]
        self.assertFalse(
            not_enabled,
            f"Tables without ENABLE ROW LEVEL SECURITY: {not_enabled}",
        )

        not_forced = [t for t in _FORCE_RLS_TABLES if not rows[t][1]]
        self.assertFalse(
            not_forced,
            f"Tables without FORCE ROW LEVEL SECURITY: {not_forced}. "
            f"Owner-role traffic would silently bypass RLS on these.",
        )

    def test_unset_guc_returns_zero_rows_on_every_table(self):
        """The core WS4b assertion.

        With ``app.admin_id`` unset, every FORCE-RLS table MUST return
        0 rows under the non-superuser app role. A non-zero count means
        either:
          (a) the policy is missing or written wrong, or
          (b) FORCE wasn't actually applied for this role.
        Either way the four walls have a hole and a customer would see
        cross-tenant data while identity bootstrap is still running.
        """
        leaks: list[tuple[str, int]] = []
        with self._app_conn() as conn:
            with conn.cursor() as cur:
                # No SET LOCAL app.admin_id -- this is the doctrinal
                # zero-state we are asserting RLS rejects.
                for table in _FORCE_RLS_TABLES:
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    row = cur.fetchone()
                    count = int(row[0]) if row else 0
                    if count != 0:
                        leaks.append((table, count))
            conn.rollback()  # Don't leave an open txn around.

        self.assertFalse(
            leaks,
            f"RLS LEAK: tables returned non-zero rows with GUC unset: "
            f"{leaks}. Doctrine violation: empty-GUC must be deny.",
        )

    def test_bogus_guc_also_returns_zero_rows(self):
        """RLS must compare admin_id against the GUC, not no-op.

        With ``app.admin_id`` set to a random uuid that owns nothing,
        every FORCE-RLS table MUST return 0 rows. This rules out a
        regression where the policy is written ``USING (true)`` or
        equivalent.
        """
        bogus = f"ws4b-bogus-{uuid.uuid4().hex}"
        leaks: list[tuple[str, int]] = []
        with self._app_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('app.admin_id', %s, true)",
                    (bogus,),
                )
                for table in _FORCE_RLS_TABLES:
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    row = cur.fetchone()
                    count = int(row[0]) if row else 0
                    if count != 0:
                        leaks.append((table, count))
            conn.rollback()

        self.assertFalse(
            leaks,
            f"RLS LEAK: tables returned rows for a bogus admin_id "
            f"({bogus!r}): {leaks}. Policy is not actually filtering "
            f"on admin_id.",
        )

    def test_admin_role_sanity_some_tables_have_data(self):
        """Sanity check: at least one FORCE-RLS table has data.

        Without this, the two assertions above could pass vacuously
        on an empty DB. We don't require EVERY table to have data
        (some are write-rare, e.g. deletion_logs) -- just that the
        whole set isn't empty.
        """
        any_data = any(
            self._table_has_rows_via_admin(t) for t in _FORCE_RLS_TABLES
        )
        self.assertTrue(
            any_data,
            "Every FORCE-RLS table is empty under the admin role -- "
            "the WS4b zero-row assertions above are vacuous. Seed the "
            "DB or point LUCIEL_LIVE_POSTGRES_URL at a non-empty "
            "cluster before relying on this test.",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
