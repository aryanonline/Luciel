"""Unit 13e §3.4.10 — session_summaries RLS isolation (NON-NEGOTIABLE).

session_summaries holds lead-derived content (the persisted cross-session
summary keyed on resolved_lead_id), so it is tenant-scoped on admin_id
with a PERMISSIVE + FORCE RLS policy identical to ``leads`` (arc14_u4).

This test proves the wall against the REAL RLS evaluator. The default
``postgres`` connection is a superuser and BYPASSES RLS, so an
ORM-session read would not exercise the policy at all. We therefore
mirror tests/db/test_c9_5_live_rls_integration.py: seed two tenants'
summaries as the superuser (mirrors the migrator role), then connect as
an EPHEMERAL non-superuser role and prove that with the tenant GUC bound
to A the table returns ONLY A's rows — tenant B's summary is invisible.

Lives in the isolation suite (tests/db) and MUST pass; it adds a new
store's isolation guarantee and weakens no existing isolation test.
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
class TestUnit13eSessionSummaryIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql as pgsql

        cls.psycopg = psycopg
        cls.admin_conn = psycopg.connect(_PG_URL, autocommit=True)

        cls.admin_a = f"u13ess-a-{uuid.uuid4().hex[:8]}"
        cls.admin_b = f"u13ess-b-{uuid.uuid4().hex[:8]}"
        cls.app_role = f"luciel_app_{uuid.uuid4().hex[:8]}"
        cls.app_password = uuid.uuid4().hex

        with cls.admin_conn.cursor() as c:
            # Seed two tenants + one summary each, as superuser (bypasses
            # RLS, mirrors the migrator role).
            for admin_id in (cls.admin_a, cls.admin_b):
                c.execute(
                    "INSERT INTO admins (id, name, tier, active) "
                    "VALUES (%s, %s, 'pro', true);",
                    (admin_id, "u13ess"),
                )
                c.execute(
                    "INSERT INTO session_summaries "
                    "(admin_id, resolved_lead_id, session_id, summary, "
                    " created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, now(), now());",
                    (
                        admin_id,
                        f"lead-{admin_id}",
                        str(uuid.uuid4()),
                        f"summary-for-{admin_id}",
                    ),
                )

            # Ephemeral non-superuser role that does NOT own the table, so
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
            c.execute(
                pgsql.SQL(
                    "GRANT SELECT ON session_summaries TO {role};"
                ).format(role=pgsql.Identifier(cls.app_role))
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
            c.execute(
                "DELETE FROM session_summaries WHERE admin_id IN (%s, %s);",
                (cls.admin_a, cls.admin_b),
            )
            c.execute(
                "DELETE FROM admins WHERE id IN (%s, %s);",
                (cls.admin_a, cls.admin_b),
            )
            c.execute(
                pgsql.SQL(
                    "REVOKE SELECT ON session_summaries FROM {role};"
                ).format(role=pgsql.Identifier(cls.app_role))
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

    def _summaries_for(self, cursor) -> list[str]:
        cursor.execute(
            "SELECT admin_id FROM session_summaries "
            "WHERE admin_id IN (%s, %s);",
            (self.admin_a, self.admin_b),
        )
        return [r[0] for r in cursor.fetchall()]

    def test_guc_unset_returns_zero_rows(self):
        with self.psycopg.connect(self.app_url) as conn:
            conn.autocommit = False
            with conn.cursor() as c:
                self.assertEqual(self._summaries_for(c), [])

    def test_tenant_a_scope_sees_only_a(self):
        with self.psycopg.connect(self.app_url) as conn:
            conn.autocommit = False
            with conn.cursor() as c:
                c.execute(
                    "SELECT set_config('app.admin_id', %s, true);",
                    (self.admin_a,),
                )
                visible = self._summaries_for(c)
        self.assertEqual(visible, [self.admin_a])
        self.assertNotIn(self.admin_b, visible)

    def test_tenant_b_scope_sees_only_b(self):
        with self.psycopg.connect(self.app_url) as conn:
            conn.autocommit = False
            with conn.cursor() as c:
                c.execute(
                    "SELECT set_config('app.admin_id', %s, true);",
                    (self.admin_b,),
                )
                visible = self._summaries_for(c)
        self.assertEqual(visible, [self.admin_b])
        self.assertNotIn(self.admin_a, visible)


if __name__ == "__main__":
    unittest.main()
