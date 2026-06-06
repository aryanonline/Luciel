"""Arc 9 C6.4 -- DB-backed behavioural suite for the luciel_ops role.

These tests connect to a live Postgres and prove that the C6.1 grant
matrix + C6.2 immutability policies enforce the doctrine end-to-end:

  Class 1: NON-OPS BLOCKED
    The application role (luciel_app or test owner) cannot UPDATE or
    DELETE admin_audit_logs under any circumstance -- the forward-only
    immutability policy is RESTRICTIVE and applies to every role
    except luciel_ops.

  Class 2: OPS ALLOWED (where appropriate)
    The luciel_ops role:
      - CAN SELECT admin_audit_logs (forensic reads)
      - CANNOT UPDATE admin_audit_logs (the audit chain is append-only;
        the C6.2 policy explicitly blocks even luciel_ops from UPDATE)
      - CANNOT DELETE admin_audit_logs (same reason)
      - CAN SELECT + DELETE the 8 retention tables
      - CANNOT INSERT/UPDATE the 8 retention tables (no grant)
      - CANNOT touch the auth perimeter (admins, tenant_configs, users,
        user_invites, user_consents) -- no grants at all

  Class 3: FLAG-GATED / FORWARD-ONLY DOCTRINE
    The C6.2 policies exist whether or not
    audit_log_immutability_enabled is True. The flag governs
    application-side guards, not DB-side policies. Policies are
    installed unconditionally because the forward-only doctrine
    treats them as ALWAYS-ON structural protections.

  Class 4: NO GUC LEAK
    When get_ops_db_session() is opened with admin_id / instance_id
    set in the in-process ContextVars, the resulting ops connection
    must NOT have app.admin_id or app.instance_id set. This is the
    behavioural complement to the C6.3 structural test that verifies
    the after_begin listener is not attached to OpsSessionLocal.

Harness:
  The tests gate on the LUCIEL_INTEGRATION_DB env var. When unset,
  the entire module is skipped (matches the C5.4 regression-suite
  pattern). When set, it must point at a Postgres URL with both
  luciel_app and luciel_ops roles already created and the C6.1 +
  C6.2 migrations applied.

  Typical sandbox invocation::

      LUCIEL_INTEGRATION_DB=postgresql+psycopg2://luciel_app:apppass@127.0.0.1:55432/luciel \\
      LUCIEL_INTEGRATION_OPS_DB=postgresql+psycopg2://luciel_ops:opspass@127.0.0.1:55432/luciel \\
      DATABASE_URL=... SECRET_KEY=... JWT_SECRET_KEY=... \\
      pytest tests/db/test_c6_4_ops_role_behavioural.py -v
"""

from __future__ import annotations

import os
import unittest

import psycopg2
import pytest
from sqlalchemy import create_engine, text


INTEGRATION_DB = os.environ.get("LUCIEL_INTEGRATION_DB")
INTEGRATION_OPS_DB = os.environ.get("LUCIEL_INTEGRATION_OPS_DB")

pytestmark = pytest.mark.skipif(
    not (INTEGRATION_DB and INTEGRATION_OPS_DB),
    reason=(
        "Arc 9 C6.4 behavioural tests require live Postgres with both "
        "luciel_app and luciel_ops roles. Set LUCIEL_INTEGRATION_DB + "
        "LUCIEL_INTEGRATION_OPS_DB to enable."
    ),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _app_engine():
    """Engine bound to the application role (non-BYPASSRLS)."""
    return create_engine(INTEGRATION_DB, pool_pre_ping=True)


def _ops_engine():
    """Engine bound to the luciel_ops role (BYPASSRLS)."""
    return create_engine(INTEGRATION_OPS_DB, pool_pre_ping=True)


# ---------------------------------------------------------------------------
# Class 1: non-ops blocked from mutating admin_audit_logs
# ---------------------------------------------------------------------------


# Permissive baseline policy name. In production this is installed by
# the broader Arc 9 C3.* migrations (one PERMISSIVE FOR ALL policy per
# table). For the C6.4 test harness we install a simulated permissive
# baseline so SELECT works for non-ops roles, then the RESTRICTIVE C6.2
# policies narrow that to forbid UPDATE/DELETE only.
#
# Postgres semantics recap (the rule we're modelling):
#   visible = (OR of permissive policies) AND (AND of restrictive policies)
# With FORCE RLS and no permissive policies, the OR-baseline is empty so
# everything is denied. The C6.2 doctrine assumes a sibling permissive
# baseline is already in place; without that the restrictive policies
# look like they over-filter. They do not -- they correctly narrow the
# permissive baseline.
C64_PERMISSIVE_BASELINE = "admin_audit_logs_c64_test_baseline"


def _reseed_admin_audit_logs(engine):
    """Make tests idempotent: ensure 2 known seed rows before each test.

    The seed rows themselves must be inserted while RLS is briefly
    disabled because once RLS is FORCE-enabled on admin_audit_logs,
    even the table owner luciel_app cannot INSERT (FORCE applies to
    owners; restrictive policy + missing permissive policy filters
    every row).

    For test purposes we briefly disable RLS, reseed, then re-enable.
    This mirrors how a real DBA would prepare an audit-log snapshot.

    We also install a permissive baseline policy that mirrors the
    production state under the broader Arc 9 C3.* tenant-isolation
    fence: every table has one permissive FOR ALL policy. The C6.2
    restrictive policies then narrow that permissive baseline.
    """
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE admin_audit_logs NO FORCE ROW LEVEL SECURITY;"))
        conn.execute(text("ALTER TABLE admin_audit_logs DISABLE ROW LEVEL SECURITY;"))
        conn.execute(text("DELETE FROM admin_audit_logs;"))
        conn.execute(text(
            "INSERT INTO admin_audit_logs (admin_id, action, actor, payload) "
            "VALUES ('tenant-a','TEST_SEED','fixture','{\"k\":\"v\"}'::jsonb),"
            "       ('tenant-b','TEST_SEED','fixture','{\"k\":\"v\"}'::jsonb);"
        ))
        # Install permissive baseline (idempotent).
        conn.execute(text(
            f"DROP POLICY IF EXISTS {C64_PERMISSIVE_BASELINE} ON admin_audit_logs;"
        ))
        conn.execute(text(
            f"CREATE POLICY {C64_PERMISSIVE_BASELINE} ON admin_audit_logs "
            f"AS PERMISSIVE FOR ALL TO PUBLIC USING (true) WITH CHECK (true);"
        ))
        conn.execute(text("ALTER TABLE admin_audit_logs ENABLE ROW LEVEL SECURITY;"))
        conn.execute(text("ALTER TABLE admin_audit_logs FORCE ROW LEVEL SECURITY;"))


class TestC64NonOpsBlocked(unittest.TestCase):
    """The C6.2 RESTRICTIVE policies block UPDATE/DELETE for non-ops roles.

    Note on Postgres semantics: the C6.2 policies are RESTRICTIVE, which
    means they AND with the (default) permissive policy set. The
    permissive default is "allow all", so RESTRICTIVE narrows it to
    "allow only when current_user = 'luciel_ops'". For any other role,
    the WHERE clause evaluates false and the row is invisible to the
    UPDATE/DELETE -- so the statement returns rowcount=0 rather than
    raising. We assert on rowcount.

    RLS must be ENABLED on the table for restrictive policies to fire.
    The C6.2 migration does NOT enable RLS itself (that's a separate C9
    envelope-close concern), so we enable it here as a SETUP step that
    represents the post-C9 production state. This is also how the
    forward-only doctrine treats the policies -- they exist before RLS
    is enabled, then become enforced when RLS goes hot.
    """

    @classmethod
    def setUpClass(cls):
        cls.engine = _app_engine()

    def setUp(self):
        # Reseed before every test to keep them independent.
        _reseed_admin_audit_logs(self.engine)

    @classmethod
    def tearDownClass(cls):
        with cls.engine.begin() as conn:
            conn.execute(text("ALTER TABLE admin_audit_logs NO FORCE ROW LEVEL SECURITY;"))
            conn.execute(text("ALTER TABLE admin_audit_logs DISABLE ROW LEVEL SECURITY;"))
            conn.execute(text(
                f"DROP POLICY IF EXISTS {C64_PERMISSIVE_BASELINE} ON admin_audit_logs;"
            ))
        cls.engine.dispose()

    def test_app_role_cannot_update_admin_audit_logs(self):
        """UPDATE under luciel_app returns rowcount=0 (policy filtered)."""
        with self.engine.begin() as conn:
            result = conn.execute(
                text("UPDATE admin_audit_logs SET action='HACKED' WHERE admin_id=:t"),
                {"t": "tenant-a"},
            )
            self.assertEqual(
                result.rowcount, 0,
                "App role UPDATE on admin_audit_logs must return "
                "rowcount=0 -- restrictive policy should filter all rows."
            )

    def test_app_role_cannot_delete_admin_audit_logs(self):
        """DELETE under luciel_app returns rowcount=0 (policy filtered)."""
        with self.engine.begin() as conn:
            result = conn.execute(
                text("DELETE FROM admin_audit_logs WHERE admin_id=:t"),
                {"t": "tenant-a"},
            )
            self.assertEqual(
                result.rowcount, 0,
                "App role DELETE on admin_audit_logs must return "
                "rowcount=0 -- restrictive policy should filter all rows."
            )

    def test_admin_audit_logs_row_count_unchanged_after_attempt(self):
        """After failed UPDATE/DELETE attempts, rows are untouched."""
        with self.engine.begin() as conn:
            # Attempt mutation that should be filtered.
            conn.execute(text("UPDATE admin_audit_logs SET action='X';"))
            conn.execute(text("DELETE FROM admin_audit_logs;"))

            # Verify the seed rows are still intact and unmodified.
            result = conn.execute(
                text("SELECT admin_id, action FROM admin_audit_logs ORDER BY id")
            ).fetchall()
            self.assertEqual(len(result), 2)
            self.assertEqual({r[1] for r in result}, {"TEST_SEED"})


# ---------------------------------------------------------------------------
# Class 2: ops allowed where appropriate, blocked where appropriate
# ---------------------------------------------------------------------------


class TestC64OpsAllowedAndBlocked(unittest.TestCase):
    """The luciel_ops grant matrix matches the C6.1 doctrine exactly."""

    @classmethod
    def setUpClass(cls):
        cls.app_engine = _app_engine()
        cls.ops_engine = _ops_engine()

    def setUp(self):
        _reseed_admin_audit_logs(self.app_engine)

    @classmethod
    def tearDownClass(cls):
        with cls.app_engine.begin() as conn:
            conn.execute(text("ALTER TABLE admin_audit_logs NO FORCE ROW LEVEL SECURITY;"))
            conn.execute(text("ALTER TABLE admin_audit_logs DISABLE ROW LEVEL SECURITY;"))
            conn.execute(text(
                f"DROP POLICY IF EXISTS {C64_PERMISSIVE_BASELINE} ON admin_audit_logs;"
            ))
        cls.app_engine.dispose()
        cls.ops_engine.dispose()

    def test_ops_can_select_admin_audit_logs(self):
        """luciel_ops sees all rows across all tenants (BYPASSRLS)."""
        with self.ops_engine.connect() as conn:
            rows = conn.execute(text("SELECT admin_id FROM admin_audit_logs")).fetchall()
            tenants = {r[0] for r in rows}
            self.assertIn("tenant-a", tenants)
            self.assertIn("tenant-b", tenants)

    def test_ops_update_admin_audit_logs_raises_permission_denied(self):
        """luciel_ops has SELECT-only grant on admin_audit_logs."""
        with self.assertRaises(Exception) as ctx:
            with self.ops_engine.begin() as conn:
                conn.execute(
                    text("UPDATE admin_audit_logs SET action='OPS_TAMPER';")
                )
        self.assertIn("permission denied", str(ctx.exception).lower())

    def test_ops_delete_admin_audit_logs_raises_permission_denied(self):
        """Same posture for DELETE -- no grant means raises."""
        with self.assertRaises(Exception) as ctx:
            with self.ops_engine.begin() as conn:
                conn.execute(text("DELETE FROM admin_audit_logs;"))
        self.assertIn("permission denied", str(ctx.exception).lower())

    def test_ops_can_delete_retention_table_sessions(self):
        """C6.1 grants SELECT + DELETE on sessions -- DELETE must work."""
        with self.ops_engine.begin() as conn:
            # First confirm rows exist.
            before = conn.execute(text("SELECT count(*) FROM sessions")).scalar()
            self.assertGreater(before, 0, "Fixture seed missing.")

            # Delete tenant-b rows.
            result = conn.execute(
                text("DELETE FROM sessions WHERE admin_id=:t"),
                {"t": "tenant-b"},
            )
            self.assertGreater(result.rowcount, 0)

            # Reinsert via app role to leave fixture intact for next test.
        with self.app_engine.begin() as conn:
            conn.execute(text("INSERT INTO sessions (admin_id) VALUES ('tenant-b');"))

    def test_ops_cannot_insert_into_retention_tables(self):
        """No INSERT grant on any retention table -- must raise."""
        with self.assertRaises(Exception) as ctx:
            with self.ops_engine.begin() as conn:
                conn.execute(
                    text("INSERT INTO sessions (admin_id) VALUES ('tenant-x');")
                )
        self.assertIn("permission denied", str(ctx.exception).lower())

    def test_ops_cannot_touch_auth_perimeter_admins(self):
        """C6.1 deliberately excludes admins table from grants."""
        with self.assertRaises(Exception) as ctx:
            with self.ops_engine.begin() as conn:
                conn.execute(text("SELECT * FROM admins;"))
        self.assertIn("permission denied", str(ctx.exception).lower())

    def test_ops_cannot_touch_auth_perimeter_users(self):
        with self.assertRaises(Exception) as ctx:
            with self.ops_engine.begin() as conn:
                conn.execute(text("SELECT * FROM users;"))
        self.assertIn("permission denied", str(ctx.exception).lower())

    def test_ops_cannot_touch_auth_perimeter_tenant_configs(self):
        with self.assertRaises(Exception) as ctx:
            with self.ops_engine.begin() as conn:
                conn.execute(text("SELECT * FROM tenant_configs;"))
        self.assertIn("permission denied", str(ctx.exception).lower())

    def test_ops_cannot_touch_auth_perimeter_user_invites(self):
        with self.assertRaises(Exception) as ctx:
            with self.ops_engine.begin() as conn:
                conn.execute(text("SELECT * FROM user_invites;"))
        self.assertIn("permission denied", str(ctx.exception).lower())

    def test_ops_cannot_touch_auth_perimeter_user_consents(self):
        with self.assertRaises(Exception) as ctx:
            with self.ops_engine.begin() as conn:
                conn.execute(text("SELECT * FROM user_consents;"))
        self.assertIn("permission denied", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# Class 3: forward-only doctrine -- policies exist unconditionally
# ---------------------------------------------------------------------------


class TestC64ForwardOnlyDoctrine(unittest.TestCase):
    """C6.2 policies are installed always, regardless of any feature flag.

    The forward-only doctrine treats audit-log immutability as a
    structural protection. The C6.2 migration installs the policies
    unconditionally. The audit_log_immutability_enabled application
    flag governs only application-side guards (test assertions, future
    runtime checks), not DB-side policy existence.
    """

    @classmethod
    def setUpClass(cls):
        cls.engine = _app_engine()

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def test_no_update_policy_exists(self):
        with self.engine.connect() as conn:
            row = conn.execute(text("""
                SELECT polname, polpermissive, polcmd
                FROM pg_policy
                WHERE polname = 'admin_audit_logs_no_update'
                  AND polrelid = 'admin_audit_logs'::regclass;
            """)).fetchone()
            self.assertIsNotNone(row, "admin_audit_logs_no_update policy missing.")
            self.assertFalse(row[1], "Policy must be RESTRICTIVE, not PERMISSIVE.")
            self.assertEqual(row[2], "w", "polcmd 'w' = UPDATE.")

    def test_no_delete_policy_exists(self):
        with self.engine.connect() as conn:
            row = conn.execute(text("""
                SELECT polname, polpermissive, polcmd
                FROM pg_policy
                WHERE polname = 'admin_audit_logs_no_delete'
                  AND polrelid = 'admin_audit_logs'::regclass;
            """)).fetchone()
            self.assertIsNotNone(row, "admin_audit_logs_no_delete policy missing.")
            self.assertFalse(row[1], "Policy must be RESTRICTIVE, not PERMISSIVE.")
            self.assertEqual(row[2], "d", "polcmd 'd' = DELETE.")

    def test_policies_reference_current_user_not_session_user(self):
        """Doctrine: current_user (not session_user) for SET ROLE robustness."""
        with self.engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT polname, pg_get_expr(polqual, polrelid) AS qual
                FROM pg_policy
                WHERE polrelid = 'admin_audit_logs'::regclass
                ORDER BY polname;
            """)).fetchall()
            self.assertEqual(len(rows), 2)
            for polname, qual in rows:
                self.assertIn("CURRENT_USER", qual.upper(),
                              f"{polname}: must use CURRENT_USER")
                self.assertNotIn("SESSION_USER", qual.upper(),
                                 f"{polname}: must NOT use SESSION_USER")
                self.assertIn("luciel_ops", qual,
                              f"{polname}: must compare against luciel_ops")

    def test_luciel_ops_role_has_bypassrls(self):
        """C6.1 doctrine: role must carry BYPASSRLS attribute."""
        with self.engine.connect() as conn:
            row = conn.execute(text("""
                SELECT rolname, rolbypassrls, rolsuper, rolcanlogin
                FROM pg_roles WHERE rolname='luciel_ops';
            """)).fetchone()
            self.assertIsNotNone(row, "luciel_ops role missing.")
            self.assertTrue(row[1], "luciel_ops must have BYPASSRLS.")
            self.assertFalse(row[2], "luciel_ops must NOT be superuser.")
            self.assertTrue(row[3], "luciel_ops must have LOGIN.")

    def test_luciel_ops_grant_count_matches_doctrine(self):
        """Exactly 17 grants: 1 SELECT on audit + 16 (S+D) on 8 retention."""
        with self.engine.connect() as conn:
            count = conn.execute(text("""
                SELECT count(*) FROM information_schema.role_table_grants
                WHERE grantee='luciel_ops';
            """)).scalar()
            self.assertEqual(count, 17,
                             "Expected 17 grants (1 SELECT on admin_audit_logs "
                             "+ 8 retention tables × 2 grants each). "
                             "Drift here means the C6.1 grant matrix changed.")


# ---------------------------------------------------------------------------
# Class 4: no GUC leak from ContextVar to ops session
# ---------------------------------------------------------------------------


class TestC64NoGucLeak(unittest.TestCase):
    """The Python-level guarantee from C6.3, validated against live DB.

    Even when the application's tenant ContextVars are populated (as
    they would be inside a normal request scope), opening an ops
    session through get_ops_db_session() must produce a connection
    where app.admin_id and app.instance_id are EMPTY -- because the
    after_begin tenant-context listener is attached to SessionLocal,
    not OpsSessionLocal.

    This is the behavioural complement to the C6.3 structural test
    (event.contains check) -- here we observe the GUC values on the
    actual Postgres connection.
    """

    @classmethod
    def setUpClass(cls):
        # Patch settings so OpsSessionLocal gets constructed against
        # the integration DB.
        from app.core import config as config_mod
        config_mod.settings.luciel_ops_db_url = INTEGRATION_OPS_DB

        # Force reload of session module so the gate re-evaluates.
        import importlib
        import app.db.session as session_mod
        cls.session_mod = importlib.reload(session_mod)

    @classmethod
    def tearDownClass(cls):
        from app.core import config as config_mod
        config_mod.settings.luciel_ops_db_url = None
        import importlib
        import app.db.session as session_mod
        importlib.reload(session_mod)

    def test_ops_session_has_empty_admin_id_guc(self):
        """app.admin_id is empty on ops connection regardless of context."""
        from app.db.tenant_context import set_current_admin_id

        # Set the ContextVar as a request scope would.
        token = set_current_admin_id("tenant-a")
        try:
            with self.session_mod.get_ops_db_session() as ops_db:
                result = ops_db.execute(text(
                    "SELECT current_setting('app.admin_id', true);"
                )).scalar()
                # current_setting with missing_ok=true returns empty
                # string (not NULL) for unset GUCs. That's the expected
                # "no leak" state.
                self.assertIn(result, (None, ""),
                              "GUC LEAK: app.admin_id set on ops connection. "
                              f"got={result!r}")
        finally:
            from app.db.tenant_context import reset_current_admin_id
            try:
                reset_current_admin_id(token)
            except Exception:
                pass

    def test_ops_session_has_empty_instance_id_guc(self):
        """app.instance_id is empty on ops connection regardless of context."""
        from app.db.instance_context import set_current_instance_id

        token = set_current_instance_id(42)
        try:
            with self.session_mod.get_ops_db_session() as ops_db:
                result = ops_db.execute(text(
                    "SELECT current_setting('app.instance_id', true);"
                )).scalar()
                self.assertIn(result, (None, ""),
                              "GUC LEAK: app.instance_id set on ops connection. "
                              f"got={result!r}")
        finally:
            from app.db.instance_context import reset_current_instance_id
            try:
                reset_current_instance_id(token)
            except Exception:
                pass

    def test_ops_session_runs_as_luciel_ops_role(self):
        """Sanity: the ops session actually connects as luciel_ops."""
        with self.session_mod.get_ops_db_session() as ops_db:
            user = ops_db.execute(text("SELECT current_user;")).scalar()
            self.assertEqual(user, "luciel_ops")

    def test_ops_session_has_bypassrls_in_effect(self):
        """Confirm BYPASSRLS attribute is in effect on the connection."""
        with self.session_mod.get_ops_db_session() as ops_db:
            bypass = ops_db.execute(text(
                "SELECT rolbypassrls FROM pg_roles WHERE rolname=current_user;"
            )).scalar()
            self.assertTrue(bypass)


if __name__ == "__main__":
    unittest.main()
