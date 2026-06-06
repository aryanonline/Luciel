"""Arc 9 C9.5 — Live-Postgres RLS integration test (opt-in).

THE GAP THIS CLOSES
===================
tests/db/test_c5_4_tenant_leak_regression.py is a *static* shape test
(it parses migration SQL but cannot run it against a real database
since the CI sandbox has no Postgres). It proves the SQL is well-formed
but does not prove that:

    1. PostgreSQL evaluates the RLS predicates against the GUCs
       we bind in app/db/session.py
    2. A SELECT with the GUC unset returns ZERO rows from a tenant
       table (RLS deny)
    3. A SELECT with the GUC set to tenant_A returns ONLY tenant_A
       rows (cross-tenant deny on read)
    4. An INSERT with the GUC set to tenant_A but admin_id pointing
       at tenant_B is denied (WITH CHECK enforcement)
    5. The set_config(..., is_local=true) GUC does NOT leak between
       transactions on the same connection (the contract that
       app/db/session.py's BEGIN listener relies on)

PROD POSTURE
============
In production, the app connects to RDS as a NON-superuser role
(luciel_app). Postgres RLS rules apply to non-superuser, non-owner
roles only -- superusers and table owners bypass RLS by default.
Migrations (which need to seed bypassing RLS) run as the privileged
migrator role. The runtime app role NEVER bypasses RLS.

This test mirrors that posture: the table is owned by the connecting
superuser (postgres), and we create an ephemeral non-superuser role
(luciel_app_<uuid>) to query as -- proving the RLS evaluator actually
fires.

RUN
===
    LUCIEL_LIVE_POSTGRES_URL=postgresql://postgres@127.0.0.1:5433/postgres \\
        python -m pytest tests/db/test_c9_5_live_rls_integration.py -v

CI does NOT run this -- it has no Postgres. Local developers run
before any RLS or GUC-binding change ships.
"""

from __future__ import annotations

import os
import unittest
import uuid
from urllib.parse import urlparse, urlunparse


_PG_URL = os.environ.get("LUCIEL_LIVE_POSTGRES_URL")


@unittest.skipUnless(
    _PG_URL,
    "Set LUCIEL_LIVE_POSTGRES_URL=postgresql://... to run live RLS tests",
)
class TestC95LiveRlsIntegration(unittest.TestCase):
    """Live-Postgres RLS integration tests for Arc 9 C9."""

    @classmethod
    def setUpClass(cls):
        # Lazy import so collection works in CI even without psycopg.
        import psycopg  # noqa: F401

        cls.psycopg = psycopg
        cls.admin_conn = psycopg.connect(_PG_URL, autocommit=True)

        cls.tenant_a = uuid.uuid4()
        cls.tenant_b = uuid.uuid4()
        cls.tenant_c = uuid.uuid4()

        cls.app_role = f"luciel_app_{uuid.uuid4().hex[:8]}"
        cls.app_password = uuid.uuid4().hex
        cls.table = f"c9_test_{uuid.uuid4().hex[:12]}"

        # Use psycopg.sql to safely compose role + password literals
        # (CREATE ROLE does not accept query parameters for these).
        from psycopg import sql as pgsql

        with cls.admin_conn.cursor() as c:
            # Ephemeral non-superuser app role.
            c.execute(
                pgsql.SQL("CREATE ROLE {role} LOGIN PASSWORD {pw};").format(
                    role=pgsql.Identifier(cls.app_role),
                    pw=pgsql.Literal(cls.app_password),
                )
            )
            # PG15+ no longer grants USAGE on schema public to PUBLIC by
            # default, so a fresh role cannot resolve unqualified table
            # names until granted. Production's luciel_app role
            # (migration arc9_c10_b_luciel_app_role) carries this grant
            # for the same reason; mirror it here so the RLS evaluator
            # -- not a schema-permission error -- is what this test
            # actually exercises on PG17.
            c.execute(
                pgsql.SQL("GRANT USAGE ON SCHEMA public TO {role};").format(
                    role=pgsql.Identifier(cls.app_role),
                )
            )
            # Tenant-scoped table owned by superuser. The app role does
            # NOT own it, so RLS applies to the app role's queries.
            c.execute(
                f"""
                CREATE TABLE {cls.table} (
                    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                    admin_id uuid NOT NULL,
                    payload text NOT NULL
                );
                """
            )
            c.execute(f"ALTER TABLE {cls.table} ENABLE ROW LEVEL SECURITY;")
            c.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE "
                f"ON {cls.table} TO {cls.app_role};"
            )
            c.execute(
                f"""
                CREATE POLICY tenant_isolation ON {cls.table}
                    USING (
                        admin_id::text = current_setting('app.admin_id', true)
                    )
                    WITH CHECK (
                        admin_id::text = current_setting('app.admin_id', true)
                    );
                """
            )
            # Seed as superuser (bypasses RLS, mirrors migrator role).
            for tenant in (cls.tenant_a, cls.tenant_b, cls.tenant_c):
                c.execute(
                    f"INSERT INTO {cls.table} (admin_id, payload) "
                    f"VALUES (%s, %s), (%s, %s);",
                    (tenant, f"row1-{tenant}", tenant, f"row2-{tenant}"),
                )

        # Build the app-role connection URL.
        parsed = urlparse(_PG_URL)
        cls.app_url = urlunparse(
            parsed._replace(
                netloc=(
                    f"{cls.app_role}:{cls.app_password}@"
                    f"{parsed.hostname}:{parsed.port or 5432}"
                )
            )
        )

    @classmethod
    def tearDownClass(cls):
        from psycopg import sql as pgsql
        with cls.admin_conn.cursor() as c:
            c.execute(f"DROP TABLE IF EXISTS {cls.table};")
            # Revoke the schema grant before DROP ROLE -- Postgres
            # refuses to drop a role that still owns dependent privileges.
            c.execute(
                pgsql.SQL(
                    "REVOKE USAGE ON SCHEMA public FROM {role};"
                ).format(role=pgsql.Identifier(cls.app_role))
            )
            c.execute(f"DROP ROLE IF EXISTS {cls.app_role};")
        cls.admin_conn.close()

    def _count_visible(self, cursor) -> int:
        cursor.execute(f"SELECT COUNT(*) FROM {self.table};")
        return cursor.fetchone()[0]

    # ----- the five contract tests -----

    def test_guc_unset_returns_zero_rows(self):
        """The C7 GUC-leak invariant: no GUC, no rows.

        With no app.admin_id bound, current_setting(..., true) returns
        empty string. The predicate admin_id::text = '' is FALSE for
        every row (UUIDs never stringify to ''). Result: 0 rows.
        """
        with self.psycopg.connect(self.app_url) as conn:
            conn.autocommit = False
            with conn.cursor() as c:
                visible = self._count_visible(c)
                self.assertEqual(
                    visible,
                    0,
                    f"GUC unset must return 0 rows; got {visible}. "
                    f"RLS is NOT enforcing.",
                )

    def test_guc_set_to_tenant_a_returns_only_tenant_a_rows(self):
        """Wall-1 contract: GUC=A binds to tenant A's rows only."""
        with self.psycopg.connect(self.app_url) as conn:
            conn.autocommit = False
            with conn.cursor() as c:
                c.execute(
                    "SELECT set_config('app.admin_id', %s, true);",
                    (str(self.tenant_a),),
                )
                c.execute(f"SELECT admin_id FROM {self.table};")
                rows = c.fetchall()
                self.assertEqual(
                    len(rows), 2,
                    f"Expected 2 rows for tenant A, got {len(rows)}",
                )
                for (tid,) in rows:
                    self.assertEqual(
                        str(tid), str(self.tenant_a),
                        f"Cross-tenant leak: GUC=A returned admin_id={tid}",
                    )

    def test_guc_set_to_tenant_b_returns_only_tenant_b_rows(self):
        """Symmetric: GUC=B binds to tenant B's rows only."""
        with self.psycopg.connect(self.app_url) as conn:
            conn.autocommit = False
            with conn.cursor() as c:
                c.execute(
                    "SELECT set_config('app.admin_id', %s, true);",
                    (str(self.tenant_b),),
                )
                c.execute(f"SELECT admin_id FROM {self.table};")
                rows = c.fetchall()
                self.assertEqual(len(rows), 2)
                for (tid,) in rows:
                    self.assertEqual(str(tid), str(self.tenant_b))

    def test_insert_with_mismatched_tenant_id_is_denied(self):
        """WITH CHECK enforcement: cannot write to another tenant.

        With GUC=A, an INSERT placing admin_id=B must fail. Proves the
        WITH CHECK clause evaluates against the same GUC as USING.
        """
        with self.psycopg.connect(self.app_url) as conn:
            conn.autocommit = False
            with conn.cursor() as c:
                c.execute(
                    "SELECT set_config('app.admin_id', %s, true);",
                    (str(self.tenant_a),),
                )
                with self.assertRaises(
                    self.psycopg.errors.InsufficientPrivilege
                ):
                    c.execute(
                        f"INSERT INTO {self.table} "
                        f"(admin_id, payload) VALUES (%s, %s);",
                        (self.tenant_b, "cross-tenant write attempt"),
                    )
            conn.rollback()

    def test_is_local_guc_does_not_leak_across_transactions(self):
        """The session.py BEGIN listener relies on per-transaction GUCs.

        If is_local=true GUCs leaked across transactions, a request
        that ran after another tenant's request on the same pooled
        connection would see the previous tenant's GUC. Verify that
        does not happen.
        """
        with self.psycopg.connect(self.app_url) as conn:
            conn.autocommit = False
            # T1: bind GUC, see 2 rows.
            with conn.cursor() as c:
                c.execute(
                    "SELECT set_config('app.admin_id', %s, true);",
                    (str(self.tenant_a),),
                )
                self.assertEqual(self._count_visible(c), 2)
            conn.commit()
            # T2: same connection, no GUC, must see 0 rows.
            with conn.cursor() as c:
                visible_t2 = self._count_visible(c)
                self.assertEqual(
                    visible_t2, 0,
                    f"is_local GUC leaked across transactions: T2 saw "
                    f"{visible_t2} rows. session.py BEGIN listener "
                    f"contract is broken.",
                )

    def test_update_cannot_move_row_to_another_tenant(self):
        """WITH CHECK on UPDATE: cannot reparent a row.

        Tenant A cannot UPDATE a row of their own to claim it belongs
        to tenant B (would otherwise allow data exfil via reparenting).
        """
        with self.psycopg.connect(self.app_url) as conn:
            conn.autocommit = False
            with conn.cursor() as c:
                c.execute(
                    "SELECT set_config('app.admin_id', %s, true);",
                    (str(self.tenant_a),),
                )
                # Confirm we have a row to attempt to reparent.
                c.execute(f"SELECT id FROM {self.table} LIMIT 1;")
                row = c.fetchone()
                self.assertIsNotNone(row, "tenant A should see its rows")
                row_id = row[0]
                with self.assertRaises(
                    self.psycopg.errors.InsufficientPrivilege
                ):
                    c.execute(
                        f"UPDATE {self.table} "
                        f"SET admin_id = %s WHERE id = %s;",
                        (self.tenant_b, row_id),
                    )
            conn.rollback()


if __name__ == "__main__":
    unittest.main()
