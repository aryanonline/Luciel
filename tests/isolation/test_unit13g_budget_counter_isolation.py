"""Unit 13g §3.7.2b — budget counter + counted-sessions RLS isolation.

The write-through budget store (conversation_budget_counter +
conversation_counted_sessions) holds tenant data keyed on admin_id, so
both tables are tenant-scoped with a PERMISSIVE + FORCE RLS policy
identical to conversation_overage_ledger / session_summaries.

This test proves the wall against the REAL RLS evaluator. The default
``postgres`` connection is a superuser and BYPASSES RLS, so an ORM read
would not exercise the policy. We therefore mirror
tests/isolation/test_unit13e_session_summary_isolation.py: seed two
tenants' counter + counted-session rows as the superuser (mirrors the
migrator role), then connect as an EPHEMERAL non-superuser role and prove
that with the tenant GUC bound to A the tables return ONLY A's rows —
tenant B's rows are invisible — AND that A cannot INSERT a row carrying
B's admin_id (the WITH CHECK half of the policy).

Lives in the isolation gate (tests/isolation) and MUST pass; it adds a
new store's isolation guarantee and weakens no existing isolation test.
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
class TestUnit13gBudgetCounterIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql as pgsql

        cls.psycopg = psycopg
        cls.admin_conn = psycopg.connect(_PG_URL, autocommit=True)

        cls.admin_a = f"u13gci-a-{uuid.uuid4().hex[:8]}"
        cls.admin_b = f"u13gci-b-{uuid.uuid4().hex[:8]}"
        cls.app_role = f"luciel_app_{uuid.uuid4().hex[:8]}"
        cls.app_password = uuid.uuid4().hex

        with cls.admin_conn.cursor() as c:
            # Seed two tenants + one counter row + one counted-session row
            # each, as superuser (bypasses RLS, mirrors the migrator role).
            for admin_id in (cls.admin_a, cls.admin_b):
                c.execute(
                    "INSERT INTO admins (id, name, tier, active) "
                    "VALUES (%s, %s, 'pro', true);",
                    (admin_id, "u13gci"),
                )
                c.execute(
                    "INSERT INTO conversation_budget_counter "
                    "(admin_id, instance_id, billing_period_start, "
                    " conversation_count, created_at, updated_at) "
                    "VALUES (%s, 7, '2026-06-01', 5, now(), now());",
                    (admin_id,),
                )
                c.execute(
                    "INSERT INTO conversation_counted_sessions "
                    "(admin_id, instance_id, billing_period_start, session_id, "
                    " created_at, updated_at) "
                    "VALUES (%s, 7, '2026-06-01', %s, now(), now());",
                    (admin_id, f"sess-{admin_id}"),
                )

            # Ephemeral non-superuser role that does NOT own the tables, so
            # RLS applies to its queries.
            c.execute(
                pgsql.SQL("CREATE ROLE {role} LOGIN PASSWORD {pw};").format(
                    role=pgsql.Identifier(cls.app_role),
                    pw=pgsql.Literal(cls.app_password),
                )
            )
            c.execute(
                pgsql.SQL("GRANT USAGE ON SCHEMA public TO {role};").format(
                    role=pgsql.Identifier(cls.app_role),
                )
            )
            for tbl in (
                "conversation_budget_counter",
                "conversation_counted_sessions",
            ):
                c.execute(
                    pgsql.SQL(
                        "GRANT SELECT, INSERT ON {t} TO {role};"
                    ).format(
                        t=pgsql.Identifier(tbl),
                        role=pgsql.Identifier(cls.app_role),
                    )
                )

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
            for tbl in (
                "conversation_counted_sessions",
                "conversation_budget_counter",
            ):
                c.execute(
                    f"DELETE FROM {tbl} WHERE admin_id IN (%s, %s);",
                    (cls.admin_a, cls.admin_b),
                )
            c.execute(
                "DELETE FROM admins WHERE id IN (%s, %s);",
                (cls.admin_a, cls.admin_b),
            )
            for tbl in (
                "conversation_budget_counter",
                "conversation_counted_sessions",
            ):
                c.execute(
                    pgsql.SQL(
                        "REVOKE SELECT, INSERT ON {t} FROM {role};"
                    ).format(
                        t=pgsql.Identifier(tbl),
                        role=pgsql.Identifier(cls.app_role),
                    )
                )
            c.execute(
                pgsql.SQL(
                    "REVOKE USAGE ON SCHEMA public FROM {role};"
                ).format(role=pgsql.Identifier(cls.app_role))
            )
            c.execute(
                pgsql.SQL("DROP ROLE IF EXISTS {role};").format(
                    role=pgsql.Identifier(cls.app_role)
                )
            )
        cls.admin_conn.close()

    def _counter_admins(self, cursor) -> list[str]:
        cursor.execute(
            "SELECT admin_id FROM conversation_budget_counter "
            "WHERE admin_id IN (%s, %s);",
            (self.admin_a, self.admin_b),
        )
        return [r[0] for r in cursor.fetchall()]

    def _counted_admins(self, cursor) -> list[str]:
        cursor.execute(
            "SELECT admin_id FROM conversation_counted_sessions "
            "WHERE admin_id IN (%s, %s);",
            (self.admin_a, self.admin_b),
        )
        return [r[0] for r in cursor.fetchall()]

    def test_guc_unset_returns_zero_rows(self):
        with self.psycopg.connect(self.app_url) as conn:
            conn.autocommit = False
            with conn.cursor() as c:
                self.assertEqual(self._counter_admins(c), [])
                self.assertEqual(self._counted_admins(c), [])

    def test_tenant_a_scope_sees_only_a(self):
        with self.psycopg.connect(self.app_url) as conn:
            conn.autocommit = False
            with conn.cursor() as c:
                c.execute(
                    "SELECT set_config('app.admin_id', %s, true);",
                    (self.admin_a,),
                )
                counters = self._counter_admins(c)
                counted = self._counted_admins(c)
        self.assertEqual(counters, [self.admin_a])
        self.assertEqual(counted, [self.admin_a])
        self.assertNotIn(self.admin_b, counters)
        self.assertNotIn(self.admin_b, counted)

    def test_tenant_b_scope_sees_only_b(self):
        with self.psycopg.connect(self.app_url) as conn:
            conn.autocommit = False
            with conn.cursor() as c:
                c.execute(
                    "SELECT set_config('app.admin_id', %s, true);",
                    (self.admin_b,),
                )
                counters = self._counter_admins(c)
                counted = self._counted_admins(c)
        self.assertEqual(counters, [self.admin_b])
        self.assertEqual(counted, [self.admin_b])
        self.assertNotIn(self.admin_a, counters)
        self.assertNotIn(self.admin_a, counted)

    def test_tenant_a_cannot_insert_row_for_tenant_b(self):
        # WITH CHECK half: bound to A, an INSERT carrying B's admin_id must
        # be rejected by the policy (cannot increment another tenant's
        # counter).
        with self.psycopg.connect(self.app_url) as conn:
            conn.autocommit = False
            with conn.cursor() as c:
                c.execute(
                    "SELECT set_config('app.admin_id', %s, true);",
                    (self.admin_a,),
                )
                with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                    c.execute(
                        "INSERT INTO conversation_budget_counter "
                        "(admin_id, instance_id, billing_period_start, "
                        " conversation_count, created_at, updated_at) "
                        "VALUES (%s, 99, '2026-06-01', 1, now(), now());",
                        (self.admin_b,),
                    )
            conn.rollback()


if __name__ == "__main__":
    unittest.main()
