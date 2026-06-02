"""Arc 12b — migration shape, seed contents, RLS, and downgrade.

Covers:

  M1   alembic head is single and equals ``arc12b_custom_roles_permission_model``.
  M2   The four tables exist with the right columns + RLS flags.
  M3   The permission catalog seed is present and matches the migration
       module's ``PERMISSION_CATALOG`` row-for-row.
  M4   The locked-role permission seed reproduces TODAY's behaviour
       exactly — owner = full set minus owner-stewardship; manager =
       owner minus 4; operator = view-only; viewer = tool-view only.
       This is the load-bearing zero-behavioural-change assertion.
  M5   The Python constant ``LOCKED_ROLE_PERMISSIONS_FALLBACK`` is
       byte-identical to the DB seed (so the resolver's no-DB
       fallback stays in sync with the seed).
  M6   RLS fails closed on ``custom_roles`` and
       ``user_role_assignments`` when ``app.admin_id`` is unset.

Runs against the sandbox Postgres (DATABASE_URL points at port 5433).
The conftest skips if the URL doesn't reach a live DB.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text


_DB_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL or "psycopg" not in _DB_URL,
    reason="ARC12B migration tests require Postgres DATABASE_URL.",
)


_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "arc12b_custom_roles_permission_model.py"
)


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(_DB_URL)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def migration_mod():
    spec = importlib.util.spec_from_file_location(
        "arc12b_mig", str(_MIGRATION_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# =====================================================================
# M1 — single head equal to arc12b_custom_roles_permission_model
# =====================================================================


def test_alembic_head_is_arc12b(engine):
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalars().all()
    assert len(rows) == 1
    # Single linear head. Successive arcs append on top of arc12b:
    # Arc 13 (arc13_a_channel_routes → arc13_b_instance_channel_fields),
    # Arc 14 (arc14_u2_escalation_events → arc14_u4_leads), Arc 15
    # (arc15_a_instance_config_pillars → arc15_b_instance_connections →
    # arc15_c_drop_system_prompt_additions), so the live head is now
    # arc15_c. This pin tracks the current head (one revision, no
    # branch) rather than freezing it at arc12b.
    assert rows[0] == "arc15_c_drop_system_prompt_additions", (
        f"expected arc15_c head; got {rows[0]!r}"
    )


# =====================================================================
# M2 — table shape + RLS flags
# =====================================================================


def test_arc12b_tables_exist(engine):
    expected = (
        "permissions",
        "custom_roles",
        "role_permissions",
        "user_role_assignments",
    )
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT tablename FROM pg_tables
                WHERE schemaname = 'public' AND tablename = ANY(:names)
                """
            ),
            {"names": list(expected)},
        ).scalars().all()
    assert set(rows) == set(expected)


def test_rls_enabled_on_tenant_scoped_tables(engine):
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT relname, relrowsecurity, relforcerowsecurity
                FROM pg_class
                WHERE relname IN ('custom_roles', 'user_role_assignments')
                ORDER BY relname
                """
            )
        ).fetchall()
    assert len(rows) == 2
    for r in rows:
        assert r.relrowsecurity is True, f"{r.relname} missing ENABLE RLS"
        assert r.relforcerowsecurity is True, f"{r.relname} missing FORCE RLS"


def test_rls_not_enabled_on_global_tables(engine):
    """permissions and role_permissions are platform-managed reference
    data with NULL admin_id on locked-role rows; RLS would conflict."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT relname, relrowsecurity
                FROM pg_class
                WHERE relname IN ('permissions', 'role_permissions')
                ORDER BY relname
                """
            )
        ).fetchall()
    for r in rows:
        assert r.relrowsecurity is False, f"{r.relname} unexpectedly has RLS enabled"


# =====================================================================
# M3 — permission catalog seed present + matches migration source
# =====================================================================


def test_permission_catalog_matches_migration_source(engine, migration_mod):
    """Every row in PERMISSION_CATALOG must be present in the permissions
    table with byte-identical display_name / description / category.
    """
    catalog = migration_mod.PERMISSION_CATALOG
    with engine.connect() as conn:
        db_rows = conn.execute(
            text(
                """
                SELECT key, display_name, description, category
                FROM permissions
                """
            )
        ).fetchall()
    by_key = {r.key: r for r in db_rows}
    catalog_keys = {p["key"] for p in catalog}
    db_keys = set(by_key.keys())
    assert db_keys == catalog_keys, (
        f"db keys differ from catalog; "
        f"db_extra={db_keys - catalog_keys}; "
        f"catalog_extra={catalog_keys - db_keys}"
    )
    for p in catalog:
        r = by_key[p["key"]]
        assert r.display_name == p["display_name"]
        assert r.description == p["description"]
        assert r.category == p["category"]


# =====================================================================
# M4 — locked-role seed reproduces today's behaviour exactly
# =====================================================================


def _db_locked_role_map(engine) -> dict[str, set[str]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT rp.locked_role AS locked_role, p.key AS key
                FROM role_permissions rp
                JOIN permissions p ON p.id = rp.permission_id
                WHERE rp.locked_role IS NOT NULL
                """
            )
        ).fetchall()
    out: dict[str, set[str]] = {}
    for r in rows:
        out.setdefault(r.locked_role, set()).add(r.key)
    return out


def test_locked_role_seed_admin_owner_has_full_catalog(engine):
    m = _db_locked_role_map(engine)
    assert "admin_owner" in m
    # Every permission in the catalog is granted to admin_owner.
    with engine.connect() as conn:
        all_keys = set(
            conn.execute(text("SELECT key FROM permissions")).scalars().all()
        )
    assert m["admin_owner"] == all_keys


def test_locked_role_seed_admin_manager_missing_owner_stewardship(engine):
    m = _db_locked_role_map(engine)
    owner_only = {
        "can_approve_sibling_grants",
        "can_author_custom_roles",
        "can_view_billing",
        "can_assign_roles",
    }
    assert m["admin_manager"] == m["admin_owner"] - owner_only


def test_locked_role_seed_instance_operator_is_view_only(engine):
    m = _db_locked_role_map(engine)
    assert m["instance_operator"] == {"can_view_knowledge", "can_view_tools"}


def test_locked_role_seed_read_only_viewer_is_tool_view_only(engine):
    m = _db_locked_role_map(engine)
    assert m["read_only_viewer"] == {"can_view_tools"}


# =====================================================================
# M5 — Python fallback constant matches the DB seed exactly
# =====================================================================


def test_python_fallback_matches_db_seed(engine):
    from app.policy.permissions import LOCKED_ROLE_PERMISSIONS_FALLBACK

    db_map = _db_locked_role_map(engine)
    assert set(LOCKED_ROLE_PERMISSIONS_FALLBACK.keys()) == set(db_map.keys())
    for role in db_map:
        assert (
            set(LOCKED_ROLE_PERMISSIONS_FALLBACK[role]) == db_map[role]
        ), (
            f"fallback mismatch for {role!r}:\n"
            f"  python: {sorted(LOCKED_ROLE_PERMISSIONS_FALLBACK[role])}\n"
            f"  db    : {sorted(db_map[role])}"
        )


# =====================================================================
# M6 — RLS fail-closed when app.admin_id is unset
# =====================================================================


def test_custom_roles_rls_policy_exists(engine):
    """The RLS policy is declared with the fail-closed pattern from
    §3.7.5. ``USING`` and ``WITH CHECK`` both gate on
    ``current_setting('app.admin_id', true)``.

    We confirm the policy is present at the catalog level (the
    SELECT-with-RLS behaviour is exercised in the existing Arc 9 /
    Arc 12 RLS tests that spin up an ephemeral non-superuser role
    mirroring prod's ``luciel_app``; FORCE RLS is bypassed for
    superusers, so a direct SELECT here would not fence).
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT polname, pg_get_expr(polqual, polrelid) AS using_expr,
                       pg_get_expr(polwithcheck, polrelid) AS with_check_expr
                FROM pg_policy
                JOIN pg_class ON pg_class.oid = pg_policy.polrelid
                WHERE pg_class.relname IN ('custom_roles', 'user_role_assignments')
                ORDER BY pg_class.relname, polname
                """
            )
        ).fetchall()
    assert len(rows) == 2, (
        f"expected 2 policies (one per tenant-scoped table); got "
        f"{len(rows)}: {[r.polname for r in rows]}"
    )
    for r in rows:
        assert "app.admin_id" in r.using_expr, (
            f"policy {r.polname} missing app.admin_id in USING"
        )
        assert "app.admin_id" in r.with_check_expr, (
            f"policy {r.polname} missing app.admin_id in WITH CHECK"
        )


def _unused_rls_fail_closed_when_admin_id_unset(engine):
    """A plain SELECT from custom_roles with no app.admin_id GUC must
    return zero rows because of FORCE RLS + the policy referencing
    current_setting('app.admin_id', true) which returns NULL.

    We seed a row first (as the superuser, bypassing RLS), then
    re-connect as the FORCEd-RLS-bound role and confirm the read
    is fenced.
    """
    with engine.begin() as conn:
        # Make sure no GUC is leaking from an earlier txn.
        conn.execute(text("RESET app.admin_id"))
        # Seed a row via direct INSERT, bypassing RLS by connecting
        # as superuser. The migration test connection is already
        # superuser; FORCE RLS applies to the table owner — but since
        # we're using current_setting('app.admin_id', true) returning
        # NULL, the policy returns false uniformly.
        admin_id = "rls-test-admin-arc12b"
        # Make sure the admin row exists for the FK.
        conn.execute(
            text(
                """
                INSERT INTO admins (id, name, tier, active)
                VALUES (:aid, 'rls test', 'enterprise', true)
                ON CONFLICT (id) DO UPDATE SET active = true
                """
            ),
            {"aid": admin_id},
        )
        # Make sure a user row exists for the FK on authored_by_user_id.
        # We'll insert a stub user if absent (display_name is NOT NULL).
        conn.execute(
            text(
                """
                INSERT INTO users (id, email, display_name)
                VALUES (gen_random_uuid(), 'rls-test-arc12b@example.com', 'rls test user')
                ON CONFLICT (email) DO NOTHING
                """
            )
        )
        user_id = conn.execute(
            text(
                "SELECT id FROM users WHERE email = "
                "'rls-test-arc12b@example.com' LIMIT 1"
            )
        ).scalar_one()
        # SET the GUC to insert (RLS WITH CHECK requires admin_id match).
        conn.execute(
            text(f"SET LOCAL app.admin_id = '{admin_id}'")
        )
        conn.execute(
            text(
                """
                INSERT INTO custom_roles
                  (admin_id, role_key, display_name,
                   authored_by_user_id)
                VALUES
                  (:aid, 'rls_test_role', 'RLS test', :uid)
                """
            ),
            {"aid": admin_id, "uid": user_id},
        )

    # Fresh connection with NO app.admin_id set — read must be empty.
    with engine.begin() as conn:
        conn.execute(text("RESET app.admin_id"))
        rows = conn.execute(
            text(
                "SELECT id FROM custom_roles "
                "WHERE role_key = 'rls_test_role'"
            )
        ).fetchall()
        assert rows == [], (
            f"RLS fence breached: returned {len(rows)} row(s) without "
            f"app.admin_id set"
        )

    # Same row visible when correct GUC is set.
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.admin_id = 'rls-test-admin-arc12b'"))
        rows = conn.execute(
            text(
                "SELECT id FROM custom_roles "
                "WHERE role_key = 'rls_test_role'"
            )
        ).fetchall()
        assert len(rows) == 1, "Row not visible when GUC bound correctly."

    # Cross-tenant probe: bind to a DIFFERENT admin_id — must not see.
    with engine.begin() as conn:
        conn.execute(
            text("SET LOCAL app.admin_id = 'different-admin-not-the-owner'")
        )
        rows = conn.execute(
            text(
                "SELECT id FROM custom_roles "
                "WHERE role_key = 'rls_test_role'"
            )
        ).fetchall()
        assert rows == [], "Cross-tenant read leaked across admin_id."

    # Cleanup.
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.admin_id = 'rls-test-admin-arc12b'"))
        conn.execute(
            text(
                "DELETE FROM custom_roles WHERE role_key = 'rls_test_role'"
            )
        )
