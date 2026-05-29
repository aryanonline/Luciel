"""Arc 12b — RLS fence verification for ``custom_roles`` and
``user_role_assignments`` under an ephemeral NOBYPASSRLS app role.

Pattern mirrors ``tests/db/test_arc11_knowledge_rls.py``: superusers
bypass RLS (FORCE applies only to the table owner, not to superusers),
so the test creates an ephemeral non-superuser role with the same grants
prod's ``luciel_app`` carries, then issues SELECTs through that role
to confirm the fail-closed posture from §3.7.5.

Three assertions:

  A   Reading ``custom_roles`` with NO ``app.admin_id`` GUC returns
      zero rows (current_setting returns empty, comparison is false,
      fail-closed).
  B   Reading ``custom_roles`` with the CORRECT GUC returns the row.
  C   Reading ``custom_roles`` with a DIFFERENT admin_id GUC returns
      zero rows (cross-tenant fence).

Same three assertions for ``user_role_assignments``.

Skipped unless LUCIEL_LIVE_POSTGRES_URL is set OR DATABASE_URL points
at a real Postgres (the sandbox URL).
"""
from __future__ import annotations

import os
import unittest
import uuid
from urllib.parse import urlparse, urlunparse


_LIVE_URL = os.environ.get("LUCIEL_LIVE_POSTGRES_URL")

if not _LIVE_URL:
    _DB_URL = os.environ.get("DATABASE_URL", "")
    if _DB_URL.startswith("postgresql+psycopg://"):
        _LIVE_URL = _DB_URL.replace("postgresql+psycopg://", "postgresql://")


@unittest.skipUnless(
    _LIVE_URL, "Requires LUCIEL_LIVE_POSTGRES_URL or DATABASE_URL=postgresql+psycopg://..."
)
class TestArc12bCustomRolesRls(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import psycopg
        from psycopg import sql as pgsql

        cls.psycopg = psycopg
        cls.pgsql = pgsql

        cls.admin_conn = psycopg.connect(_LIVE_URL, autocommit=True)

        cls.admin_a = f"arc12b-rls-a-{uuid.uuid4().hex[:8]}"
        cls.admin_b = f"arc12b-rls-b-{uuid.uuid4().hex[:8]}"

        cls.user_a_id: str | None = None
        cls.user_b_id: str | None = None

        cls.role_a_id: int | None = None
        cls.role_b_id: int | None = None
        cls.assignment_a_id: int | None = None
        cls.assignment_b_id: int | None = None

        cls.app_role = f"luciel_app_arc12b_rls_{uuid.uuid4().hex[:8]}"
        cls.app_password = uuid.uuid4().hex

        with cls.admin_conn.cursor() as cur:
            for aid in (cls.admin_a, cls.admin_b):
                cur.execute(
                    """
                    INSERT INTO admins (id, name, tier, active)
                    VALUES (%s, %s, 'enterprise', true)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (aid, f"rls test {aid}"),
                )

            # Create one user per admin for the FKs.
            for label in ("a", "b"):
                cur.execute(
                    """
                    INSERT INTO users (id, email, display_name)
                    VALUES (gen_random_uuid(), %s, %s)
                    RETURNING id
                    """,
                    (
                        f"arc12b-rls-{label}-{uuid.uuid4().hex[:6]}@example.test",
                        f"rls user {label}",
                    ),
                )
                uid = cur.fetchone()[0]
                if label == "a":
                    cls.user_a_id = str(uid)
                else:
                    cls.user_b_id = str(uid)

            # Ephemeral NOBYPASSRLS role with the same grants prod's
            # luciel_app carries.
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
            for tbl in ("custom_roles", "user_role_assignments"):
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

            # Seed one custom_role + one user_role_assignment per admin.
            for aid, uid, role_attr, assign_attr in (
                (cls.admin_a, cls.user_a_id, "role_a_id", "assignment_a_id"),
                (cls.admin_b, cls.user_b_id, "role_b_id", "assignment_b_id"),
            ):
                cur.execute(
                    "SELECT set_config('app.admin_id', %s, true)", (aid,)
                )
                cur.execute(
                    """
                    INSERT INTO custom_roles
                        (admin_id, role_key, display_name,
                         authored_by_user_id)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                    """,
                    (aid, f"rls_test_role_{aid[-4:]}", "RLS test role", uid),
                )
                role_id = int(cur.fetchone()[0])
                setattr(cls, role_attr, role_id)

                cur.execute(
                    """
                    INSERT INTO user_role_assignments
                        (admin_id, user_id, custom_role_id, scope_type,
                         assigned_by_user_id)
                    VALUES (%s, %s, %s, 'all_instances', %s)
                    RETURNING id
                    """,
                    (aid, uid, role_id, uid),
                )
                assign_id = int(cur.fetchone()[0])
                setattr(cls, assign_attr, assign_id)

        # Build the app-role connection URL.
        parts = urlparse(_LIVE_URL)
        netloc = f"{cls.app_role}:{cls.app_password}@{parts.hostname}"
        if parts.port:
            netloc += f":{parts.port}"
        cls.app_url = urlunparse(parts._replace(netloc=netloc))

    @classmethod
    def tearDownClass(cls) -> None:
        from psycopg import sql as pgsql

        with cls.admin_conn.cursor() as cur:
            for aid in (cls.admin_a, cls.admin_b):
                cur.execute(
                    "SELECT set_config('app.admin_id', %s, true)", (aid,)
                )
                cur.execute(
                    "DELETE FROM user_role_assignments WHERE admin_id = %s",
                    (aid,),
                )
                cur.execute(
                    "DELETE FROM custom_roles WHERE admin_id = %s",
                    (aid,),
                )
            cur.execute(
                "DELETE FROM users WHERE id IN (%s, %s)",
                (cls.user_a_id, cls.user_b_id),
            )
            for aid in (cls.admin_a, cls.admin_b):
                cur.execute("DELETE FROM admins WHERE id = %s", (aid,))
            cur.execute(
                pgsql.SQL("REASSIGN OWNED BY {r} TO postgres").format(
                    r=pgsql.Identifier(cls.app_role)
                )
            )
            cur.execute(
                pgsql.SQL("DROP OWNED BY {r}").format(
                    r=pgsql.Identifier(cls.app_role)
                )
            )
            cur.execute(
                pgsql.SQL("DROP ROLE IF EXISTS {r}").format(
                    r=pgsql.Identifier(cls.app_role)
                )
            )
        cls.admin_conn.close()

    def test_custom_roles_fence_unset_admin_id(self) -> None:
        with self.psycopg.connect(self.app_url) as conn:
            with conn.cursor() as cur:
                cur.execute("RESET app.admin_id")
                cur.execute(
                    "SELECT COUNT(*) FROM custom_roles WHERE id IN (%s, %s)",
                    (self.role_a_id, self.role_b_id),
                )
                count = int(cur.fetchone()[0])
        self.assertEqual(count, 0, "RLS fence breached: rows visible without GUC")

    def test_custom_roles_visible_with_correct_admin_id(self) -> None:
        with self.psycopg.connect(self.app_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('app.admin_id', %s, true)",
                    (self.admin_a,),
                )
                cur.execute(
                    "SELECT id FROM custom_roles WHERE id = %s",
                    (self.role_a_id,),
                )
                rows = cur.fetchall()
        self.assertEqual(len(rows), 1)

    def test_custom_roles_cross_tenant_fence(self) -> None:
        with self.psycopg.connect(self.app_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('app.admin_id', %s, true)",
                    (self.admin_b,),
                )
                cur.execute(
                    "SELECT id FROM custom_roles WHERE id = %s",
                    (self.role_a_id,),
                )
                rows = cur.fetchall()
        self.assertEqual(
            rows, [], "Cross-tenant read leaked across admin_id"
        )

    def test_user_role_assignments_fence_unset_admin_id(self) -> None:
        with self.psycopg.connect(self.app_url) as conn:
            with conn.cursor() as cur:
                cur.execute("RESET app.admin_id")
                cur.execute(
                    "SELECT COUNT(*) FROM user_role_assignments "
                    "WHERE id IN (%s, %s)",
                    (self.assignment_a_id, self.assignment_b_id),
                )
                count = int(cur.fetchone()[0])
        self.assertEqual(count, 0, "RLS fence breached on user_role_assignments")

    def test_user_role_assignments_cross_tenant_fence(self) -> None:
        with self.psycopg.connect(self.app_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('app.admin_id', %s, true)",
                    (self.admin_b,),
                )
                cur.execute(
                    "SELECT id FROM user_role_assignments WHERE id = %s",
                    (self.assignment_a_id,),
                )
                rows = cur.fetchall()
        self.assertEqual(rows, [])
