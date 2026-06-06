"""Arc 10 re-open Gap 5 -- luciel_audit_archiver role privilege pins.

The Arc 10 migration creates a dedicated luciel_audit_archiver Postgres
role (BYPASSRLS, narrowly granted: SELECT + UPDATE on admin_audit_logs
only). This file pins the grant matrix at the migration level so any
future migration that loosens the role's surface fails CI.

These tests run sandbox-style (no live DB). They assert on the shipped
migration source. The e2e harness in workspace/arc10/e2e_script.py
(Suite C) ran the actual privilege probes against prod RDS and passed;
this file is the regression net.

Doctrine origin: D-arc10-c61-vision-divergence-on-audit-immutability-
2026-05-27. Arc 9 C6.1 declared 'forward-only audit log immutability,
even the ops role cannot mutate'. Vision 6.5 / 7 says 'audit log
archived to cold storage for legal retention window' with tier-
conditional retention (30d Free / 1y Pro / 7y Enterprise). Per Vision
10's doctrine anchor, Vision wins. The audit-tier retention worker
needs UPDATE access (to stamp cold_archived_at) but must not be able
to break the chain (no DELETE, no INSERT, no DDL).

Pins:

  C1  Role name is exactly 'luciel_audit_archiver' -- not
      'audit_archiver' or any other variant. The SSM secret + the
      ECS task secrets reference this literal name.

  C2  CREATE ROLE statement specifies BYPASSRLS. Without it the role
      cannot see rows for admins whose RLS context is not active.

  C3  GRANT lines exist for SELECT + UPDATE on admin_audit_logs.
      NOT for any other table.

  C4  No GRANT INSERT, no GRANT DELETE, no GRANT TRUNCATE on
      admin_audit_logs. The audit chain is append-only via the
      narrow archiver role's UPDATE (cold_archived_at stamp only).

  C5  REVOKE CREATE on schema public from the role. Defense-in-depth
      against the role creating new tables or owning objects.

  C6  Downgrade path: the role drop is symmetric (revokes grants
      first, then drops the role). PG refuses to drop a role that
      holds grants or owns objects, so order matters.

  C7  Password is sourced from env var ARC10_AUDIT_ARCHIVER_PASSWORD,
      never embedded in the migration source. If the env var is
      unset, the role is created with no password (cannot log in).

  C8  Role + grants are distinct from luciel_ops (Arc 9 C6.1). The
      C6.1 'forward-only' posture holds for luciel_ops; only this
      new archiver role can UPDATE.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    REPO_ROOT / "app" / "migrations" / "versions" / "arc10_lifecycle_subsystem.py"
)
C61_MIGRATION_PATH = (
    REPO_ROOT / "app" / "migrations" / "versions" / "arc9_c6_1_luciel_ops_role.py"
)


def _src() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def _extract_archiver_tables_block(src: str) -> str:
    """Return the text between the outer parens of _ARCHIVER_AUDIT_TABLES_RU.

    A naive regex '[^)]*' stops at the first ')' which (in this
    migration) appears inside a comment that uses parentheses. We walk
    the source from the tuple's opening '(' and track paren depth so
    we capture the full tuple body even when comments contain parens.
    """
    marker = "_ARCHIVER_AUDIT_TABLES_RU"
    start = src.find(marker)
    assert start >= 0, f"{marker} not found in migration source"
    paren_open = src.find("(", start)
    assert paren_open >= 0, "opening paren for tuple not found"
    depth = 1
    i = paren_open + 1
    while i < len(src) and depth > 0:
        ch = src[i]
        # Skip past Python string literals so a paren inside a string
        # doesn't confuse the counter.
        if ch in "'\"":
            quote = ch
            i += 1
            while i < len(src) and src[i] != quote:
                if src[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        if ch == "#":
            # Skip to end of line.
            nl = src.find("\n", i)
            i = nl if nl >= 0 else len(src)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    assert depth == 0, "unbalanced parens in _ARCHIVER_AUDIT_TABLES_RU"
    return src[paren_open + 1 : i - 1]


# ---------------------------------------------------------------------
# C1: role name constant.
# ---------------------------------------------------------------------

def test_archiver_role_name_constant():
    src = _src()
    pat = re.search(
        r"_ARCHIVER_ROLE\s*=\s*['\"]luciel_audit_archiver['\"]",
        src,
    )
    assert pat, (
        "Migration must declare _ARCHIVER_ROLE = 'luciel_audit_archiver'. "
        "This literal is referenced by the SSM SecureString name and the "
        "ECS task definition secret. Renaming desynchronises three "
        "systems at once."
    )


# ---------------------------------------------------------------------
# C2: BYPASSRLS on role creation.
# ---------------------------------------------------------------------

def test_create_role_specifies_bypassrls():
    src = _src()
    # The CREATE ROLE happens inside an EXECUTE format() pattern.
    # Look for the BYPASSRLS keyword in proximity to the role creation.
    assert (
        "CREATE ROLE" in src
        and "BYPASSRLS" in src
        and "luciel_audit_archiver" in src
    ), "luciel_audit_archiver must be created with BYPASSRLS."
    # And it must be on the CREATE-ROLE line (not just in a comment).
    pat = re.search(
        r"EXECUTE\s+['\"]CREATE\s+ROLE\s+\{_ARCHIVER_ROLE\}\s+WITH\s+LOGIN\s+BYPASSRLS",
        src,
    )
    assert pat, (
        "CREATE ROLE statement must include 'WITH LOGIN BYPASSRLS' "
        "literally. Without BYPASSRLS the archiver cannot see rows for "
        "admins whose RLS context isn't active in the worker session."
    )


# ---------------------------------------------------------------------
# C3: SELECT + UPDATE grants on admin_audit_logs.
# ---------------------------------------------------------------------

def test_grant_select_update_on_admin_audit_logs():
    src = _src()
    # Extract the tuple body. Parens-in-comments inside the tuple make
    # the obvious '[^)]*' regex stop early; instead, locate the tuple
    # start and walk forward balancing parens.
    table_block = _extract_archiver_tables_block(src)
    assert '"admin_audit_logs"' in table_block, (
        "_ARCHIVER_AUDIT_TABLES_RU must contain 'admin_audit_logs' "
        "(plural). Singular admin_audit_log was the bug fixed in the "
        "f56874f commit; do not regress."
    )

    # And the grant execution loop.
    pat = re.search(
        r"GRANT\s+SELECT,\s*UPDATE\s+ON\s+\{tbl\}\s+TO\s+\{_ARCHIVER_ROLE\}",
        src,
    )
    assert pat, (
        "GRANT SELECT, UPDATE ON {tbl} TO {_ARCHIVER_ROLE} must be the "
        "exact privilege set. SELECT alone breaks the archiver "
        "(can't stamp cold_archived_at). Adding DELETE breaks chain "
        "integrity."
    )


def test_archiver_tables_set_is_exactly_admin_audit_logs():
    """The role's surface is admin_audit_logs and nothing else. If a
    future migration adds another table to _ARCHIVER_AUDIT_TABLES_RU
    without rotating the role's name, the doctrine drift should fail
    CI here."""
    src = _src()
    table_block = _extract_archiver_tables_block(src)
    # Strip out comment lines so a parens-bearing comment doesn't trick
    # the table-name extraction.
    no_comments = re.sub(r"#[^\n]*\n", "\n", table_block)
    table_names = re.findall(r'"([^"]+)"', no_comments)
    assert table_names == ["admin_audit_logs"], (
        f"_ARCHIVER_AUDIT_TABLES_RU must be exactly ('admin_audit_logs',). "
        f"Got {table_names}. Adding more tables expands the role's "
        "blast radius; refuse silently. Doctrine drift -- file a new "
        "design note before changing."
    )


# ---------------------------------------------------------------------
# C4: no INSERT, no DELETE, no TRUNCATE grants.
# ---------------------------------------------------------------------

@pytest.mark.parametrize("forbidden", ["INSERT", "DELETE", "TRUNCATE"])
def test_no_forbidden_grants_on_archiver(forbidden: str):
    src = _src()
    # Look for a GRANT line that includes the forbidden privilege AND
    # references the archiver role. Use word-boundary regex to avoid
    # matching the privilege name appearing in comments / docstrings.
    pat = re.search(
        rf"GRANT[^;]*\b{forbidden}\b[^;]*TO\s+\{{?_ARCHIVER_ROLE\}}?",
        src,
    )
    assert not pat, (
        f"luciel_audit_archiver MUST NOT have GRANT {forbidden}. "
        f"Found grant pattern: {pat.group(0) if pat else 'none'}. "
        "Vision 6.5 + chain integrity: the role can ONLY stamp "
        "cold_archived_at via UPDATE; it cannot append, remove, or "
        "truncate audit rows."
    )


# ---------------------------------------------------------------------
# C5: REVOKE CREATE on schema public.
# ---------------------------------------------------------------------

def test_revoke_create_on_schema_public():
    src = _src()
    pat = re.search(
        r"REVOKE\s+CREATE\s+ON\s+SCHEMA\s+public\s+FROM\s+\{_ARCHIVER_ROLE\}",
        src,
    )
    assert pat, (
        "Defense-in-depth: REVOKE CREATE ON SCHEMA public FROM "
        "luciel_audit_archiver. Without this, the role could (in "
        "principle) create new tables in public and become an owner, "
        "which makes the future DROP ROLE refuse."
    )


# ---------------------------------------------------------------------
# C6: downgrade revokes before drop.
# ---------------------------------------------------------------------

def test_downgrade_revokes_grants_before_drop():
    """PG refuses to drop a role that holds grants. The downgrade()
    path must REVOKE all grants, then DROP. Order matters."""
    src = _src()
    # Locate the downgrade() function body.
    pat = re.search(r"def downgrade\(\)(.*)", src, re.DOTALL)
    assert pat, "downgrade() not found"
    body = pat.group(1)

    revoke_idx = body.find("REVOKE")
    drop_idx = body.find("DROP ROLE")
    assert revoke_idx >= 0, "downgrade must REVOKE before DROP"
    assert drop_idx >= 0, "downgrade must DROP ROLE"
    assert revoke_idx < drop_idx, (
        "REVOKE statements must come before DROP ROLE in downgrade(). "
        "PG refuses to drop a role that still holds grants."
    )


def test_downgrade_does_not_touch_luciel_ops():
    """luciel_ops was created by Arc 9 C6.1; Arc 10 only reuses it via
    the paired code change in retention.py. The Arc 10 downgrade must
    NOT drop or alter luciel_ops -- that's outside its lifecycle."""
    src = _src()
    pat = re.search(r"def downgrade\(\)(.*)", src, re.DOTALL)
    body = pat.group(1)
    # Allow comments referencing luciel_ops but no actual DDL touching it.
    # Strip comment lines first.
    body_no_comments = re.sub(r"#[^\n]*\n", "\n", body)
    body_no_docstrings = re.sub(r'""".*?"""', "", body_no_comments, flags=re.DOTALL)
    assert "DROP ROLE luciel_ops" not in body_no_docstrings
    assert "REVOKE" not in body_no_docstrings or "luciel_ops" not in body_no_docstrings, (
        "Arc 10 downgrade must not REVOKE from luciel_ops. That role is "
        "owned by Arc 9 C6.1's migration lifecycle."
    )


# ---------------------------------------------------------------------
# C7: password sourced from env, never embedded.
# ---------------------------------------------------------------------

def test_password_sourced_from_env_var():
    src = _src()
    assert 'os.environ.get("ARC10_AUDIT_ARCHIVER_PASSWORD")' in src, (
        "Migration must read the role password from "
        "ARC10_AUDIT_ARCHIVER_PASSWORD env var. The ECS migration-"
        "runner task definition injects this from SSM SecureString at "
        "alembic-upgrade time."
    )


def test_no_password_literal_committed():
    """A literal PASSWORD '...' in the migration source would be a
    credential commit. The migration MUST always read from env."""
    src = _src()
    # Look for ALTER ROLE ... PASSWORD followed by a quoted literal
    # (not a format() / current_setting() expression).
    pat = re.search(
        r"ALTER\s+ROLE[^;]*PASSWORD\s+['\"][^%][^'\"]+['\"]",
        src,
    )
    assert not pat, (
        "Found a literal PASSWORD assignment in the migration source: "
        f"{pat.group(0) if pat else ''}. "
        "Passwords MUST flow through env -> set_config -> current_setting "
        "so they never enter git."
    )


# ---------------------------------------------------------------------
# C8: archiver role is distinct from luciel_ops.
# ---------------------------------------------------------------------

def test_archiver_role_not_luciel_ops():
    """The Arc 9 C6.1 'forward-only' posture for luciel_ops holds.
    Audit retention got its own role specifically to keep that
    invariant."""
    src = _src()
    # The role name constant is luciel_audit_archiver (C1 already
    # asserts this). Belt-and-braces: confirm CREATE ROLE doesn't
    # also touch luciel_ops.
    assert "CREATE ROLE luciel_ops" not in src, (
        "Arc 10 migration MUST NOT create luciel_ops. That role belongs "
        "to Arc 9 C6.1's lifecycle. Arc 10 creates a SEPARATE role "
        "(luciel_audit_archiver) so the C6.1 'forward-only ops' "
        "posture is preserved."
    )


def test_c61_migration_exists():
    """Sanity: the Arc 9 C6.1 migration is still in the tree. If a
    future migration consolidates roles, the C6.1 lifecycle would
    move and this test should catch the implicit assumption shift."""
    assert C61_MIGRATION_PATH.exists(), (
        "arc9_c6_1_luciel_ops_role.py is referenced by Arc 10's "
        "doctrine. If it's been removed, Arc 10's reasoning about "
        "'luciel_ops is owned by Arc 9' has to be re-derived."
    )
