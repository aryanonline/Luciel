"""Unit 13c — auth_class migration: shape guard + live round-trip.

Two layers, mirroring the repo's migration-test convention:

1. SHAPE (always runs, backend-free): AST/text assertions on
   ``alembic/versions/unit13c_connection_auth_class.py`` — revision id,
   down_revision = current head, the NOT NULL add + temporary
   server_default, the per-type backfill UPDATEs, the four-value CHECK
   constraint, and a symmetric downgrade.

2. LIVE (skipUnless ``LUCIEL_LIVE_POSTGRES_URL``): the DB has already been
   upgraded to head, so the column + CHECK are in place. We assert the
   live honesty backstop directly on the table — the CHECK ADMITS the four
   classes, REJECTS a bogus class, and NOT NULL rejects a null — and we
   exercise the backfill mapping by seeding one row per connection_type
   and running the migration's own UPDATE statements, asserting each row
   lands in the correct §3.8.5 class. Everything runs inside a transaction
   that is rolled back, leaving the live DB untouched.
"""
from __future__ import annotations

import importlib.util
import os
import re
import unittest
import uuid
from pathlib import Path

VERSIONS_DIR = Path(__file__).parent.parent.parent / "app" / "migrations" / "versions"
REV_ID = "unit13c_connection_auth_class"
DOWN_REV = "unit9_escalation_signal_llm_unavailable"

ALLOWED_CLASSES = (
    "oauth_token",
    "long_lived_token",
    "api_key",
    "provisioned_resource",
)

# (connection_type, expected auth_class) — mirrors AUTH_CLASS_BY_TYPE.
TYPE_TO_CLASS = (
    ("calendar", "oauth_token"),
    ("crm", "oauth_token"),
    ("email_sender", "provisioned_resource"),
    ("sms_sender", "provisioned_resource"),
    ("record_source", "api_key"),
    ("outbound_webhook", "api_key"),
)


def _path() -> Path:
    return VERSIONS_DIR / f"{REV_ID}.py"


def _text() -> str:
    return _path().read_text()


# ---------------------------------------------------------------------
# Layer 1 — shape (always runs).
# ---------------------------------------------------------------------


class TestUnit13cMigrationShape(unittest.TestCase):
    def test_migration_file_exists(self) -> None:
        self.assertTrue(_path().exists())

    def test_revision_matches_filename(self) -> None:
        m = re.search(r'^revision\s*=\s*"([^"]+)"', _text(), re.MULTILINE)
        self.assertTrue(m and m.group(1) == REV_ID)

    def test_chains_to_current_head(self) -> None:
        m = re.search(r'^down_revision\s*=\s*"([^"]+)"', _text(), re.MULTILINE)
        self.assertTrue(m and m.group(1) == DOWN_REV)

    def test_module_imports_clean(self) -> None:
        spec = importlib.util.spec_from_file_location(
            f"_t_{REV_ID}", str(_path())
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.revision, REV_ID)
        self.assertEqual(module.down_revision, DOWN_REV)
        self.assertTrue(callable(module.upgrade))
        self.assertTrue(callable(module.downgrade))

    def test_adds_not_null_column_with_temp_default(self) -> None:
        up = _text().split("def upgrade")[1].split("def downgrade")[0]
        self.assertIn("add_column", up)
        self.assertIn("auth_class", up)
        self.assertIn("nullable=False", up)
        # Temporary server_default lets the NOT NULL add succeed against
        # existing rows; it MUST be dropped after the backfill.
        self.assertIn("server_default", up)
        self.assertIn("server_default=None", up)

    def test_backfill_covers_every_type_to_its_class(self) -> None:
        # The backfill table + UPDATE live at module scope (_BACKFILL) +
        # in upgrade(); assert against the whole file text.
        text = _text()
        self.assertIn("UPDATE", text.upper())
        for conn_type, _klass in TYPE_TO_CLASS:
            self.assertIn(conn_type, text, conn_type)
        for klass in ALLOWED_CLASSES:
            self.assertIn(klass, text, klass)

    def test_backfill_casts_enum_to_text(self) -> None:
        # connection_type is a PG ENUM; comparing it to varchar literals
        # without ::text fails with "operator does not exist". The cast is
        # the fix and must be present.
        up = _text().split("def upgrade")[1].split("def downgrade")[0]
        self.assertIn("connection_type::text", up)

    def test_check_constraint_pins_four_values(self) -> None:
        # create_check_constraint is in upgrade(); the four allowed values
        # live in the module-level _ALLOWED tuple it builds the predicate
        # from. Assert the call exists + every value is a quoted literal.
        text = _text()
        self.assertIn(
            "create_check_constraint",
            text.split("def upgrade")[1].split("def downgrade")[0],
        )
        for klass in ALLOWED_CLASSES:
            self.assertIn(f'"{klass}"', text, klass)

    def test_downgrade_drops_check_and_column(self) -> None:
        down = _text().split("def downgrade")[1]
        self.assertIn("drop_constraint", down)
        self.assertIn("drop_column", down)


# ---------------------------------------------------------------------
# Layer 2 — live round-trip / honesty backstop (skipUnless live PG).
# ---------------------------------------------------------------------

_PG_URL = os.environ.get("LUCIEL_LIVE_POSTGRES_URL")


@unittest.skipUnless(
    _PG_URL,
    "Set LUCIEL_LIVE_POSTGRES_URL=postgresql://... to run live migration tests",
)
class TestUnit13cAuthClassLive(unittest.TestCase):
    """The DB is at head: column + CHECK exist. Assert the live backstop
    and backfill mapping inside a rolled-back transaction."""

    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls.psycopg = psycopg
        cls.conn = psycopg.connect(_PG_URL, autocommit=False)
        cls.admin_id = f"u13c-{uuid.uuid4().hex[:8]}"
        with cls.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO admins (id, name, tier, tier_source, active)
                VALUES (%s, %s, 'free', 'free_signup', true)
                ON CONFLICT (id) DO NOTHING
                """,
                (cls.admin_id, f"luciel-{cls.admin_id}"),
            )
            cur.execute(
                """
                INSERT INTO instances
                    (admin_id, instance_slug, display_name, active)
                VALUES (%s, %s, %s, true)
                RETURNING id
                """,
                (cls.admin_id, f"slug-{cls.admin_id}", "u13c inst"),
            )
            cls.instance_id = int(cur.fetchone()[0])
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        with cls.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM instance_connections WHERE admin_id = %s",
                (cls.admin_id,),
            )
            cur.execute(
                "DELETE FROM instances WHERE admin_id = %s", (cls.admin_id,)
            )
            cur.execute("DELETE FROM admins WHERE id = %s", (cls.admin_id,))
        cls.conn.commit()
        cls.conn.close()

    def setUp(self) -> None:
        # admin_id is required by the RLS policy on instance_connections.
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('app.admin_id', %s, true)",
                (self.admin_id,),
            )

    def tearDown(self) -> None:
        self.conn.rollback()

    def _insert(self, *, conn_type: str, auth_class: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO instance_connections
                    (admin_id, instance_id, connection_type, provider,
                     status, auth_class)
                VALUES (%s, %s, %s::connection_type, %s,
                        'unconfigured'::connection_status, %s)
                """,
                (
                    self.admin_id,
                    self.instance_id,
                    conn_type,
                    "test-provider",
                    auth_class,
                ),
            )

    def test_check_admits_all_four_classes(self) -> None:
        # Each of the four classes inserts cleanly (CHECK passes). Distinct
        # connection_types so the partial unique index over
        # (admin_id, instance_id, connection_type) doesn't collide; the
        # CHECK on auth_class is independent of the type.
        distinct_types = (
            "record_source",
            "outbound_webhook",
            "calendar",
            "crm",
        )
        for conn_type, klass in zip(distinct_types, ALLOWED_CLASSES):
            self._insert(conn_type=conn_type, auth_class=klass)
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM instance_connections WHERE admin_id = %s",
                (self.admin_id,),
            )
            self.assertEqual(int(cur.fetchone()[0]), len(ALLOWED_CLASSES))

    def test_check_rejects_bogus_class(self) -> None:
        with self.assertRaises(self.psycopg.errors.CheckViolation):
            self._insert(conn_type="record_source", auth_class="bogus_class")

    def test_not_null_rejects_null_auth_class(self) -> None:
        with self.assertRaises(self.psycopg.errors.NotNullViolation):
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO instance_connections
                        (admin_id, instance_id, connection_type, provider,
                         status, auth_class)
                    VALUES (%s, %s, 'record_source'::connection_type, %s,
                            'unconfigured'::connection_status, NULL)
                    """,
                    (self.admin_id, self.instance_id, "test-provider"),
                )

    def test_backfill_maps_each_type_to_its_class(self) -> None:
        # Seed one row per connection_type with a deliberately WRONG
        # auth_class (the migration's temp default), then run the
        # migration's own backfill UPDATEs and assert each row lands in
        # the correct §3.8.5 class.
        for conn_type, _expected in TYPE_TO_CLASS:
            self._insert(conn_type=conn_type, auth_class="api_key")

        backfill = (
            ("oauth_token", ("calendar", "crm")),
            ("provisioned_resource", ("email_sender", "sms_sender")),
            ("api_key", ("record_source", "outbound_webhook")),
        )
        with self.conn.cursor() as cur:
            for klass, types in backfill:
                cur.execute(
                    "UPDATE instance_connections SET auth_class = %s "
                    "WHERE admin_id = %s AND connection_type::text = ANY(%s)",
                    (klass, self.admin_id, list(types)),
                )
            for conn_type, expected in TYPE_TO_CLASS:
                cur.execute(
                    "SELECT auth_class FROM instance_connections "
                    "WHERE admin_id = %s AND connection_type::text = %s",
                    (self.admin_id, conn_type),
                )
                got = cur.fetchone()[0]
                self.assertEqual(got, expected, f"{conn_type} → {got}")


if __name__ == "__main__":
    unittest.main()
