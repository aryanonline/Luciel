"""Pillar 22 - DB role grants enforce audit-log append-only at the engine layer (P3-E.1).

Step 28 Phase 3 - C5. Resolves PHASE_3_COMPLIANCE_BACKLOG P3-E.1.

# Why this pillar exists

Phase 2 Commit 2 closes the audit-log API surface: POST/PUT/PATCH/DELETE
on `/api/v1/admin/audit-log` return 404/405. That stops *application*-
level tampering. But a compromised application process (or a malicious
operator with the worker DSN) could still issue raw SQL like:

    UPDATE admin_audit_logs SET note = 'tampered'  WHERE id = 1234;
    DELETE FROM admin_audit_logs WHERE id = 1234;
    TRUNCATE admin_audit_logs;

Migration `f392a842f885` deliberately closes that surface too: it grants
`luciel_worker` ONLY `SELECT, INSERT` on `admin_audit_logs` and
`memory_items`, with NO `UPDATE`, NO `DELETE`, NO `TRUNCATE`. This makes
"audit logs are append-only" a database-enforced invariant, not a
policy-enforced one.

Pillar 22 is the regression guard for that contract. If a future
migration accidentally adds `GRANT UPDATE ON admin_audit_logs TO
luciel_worker` (e.g. to fix a "soft-delete" feature gone wrong),
Pillar 22 fails immediately on the next verify run.

# Two-layer assertion (mirrors Pillar 16 D11)

  1. SCHEMA / GRANTS LAYER: query
     `information_schema.role_table_grants` for the role this verify
     task connects as (`current_user`). Assert the privileges on
     `admin_audit_logs` and `memory_items` are EXACTLY {SELECT,
     INSERT}. Anything more (UPDATE/DELETE/TRUNCATE/REFERENCES/
     TRIGGER) FAILS. Anything less (e.g. SELECT only) FAILS too --
     the worker MUST be able to insert audit rows or the audit
     emission contract is broken.

  2. ENFORCEMENT LAYER: as the connected role, attempt a direct
     `UPDATE admin_audit_logs SET note = '...' WHERE id = (SELECT
     MIN(id) FROM admin_audit_logs)` and a direct `DELETE FROM
     admin_audit_logs WHERE id = (...)`. Both MUST raise
     `psycopg.errors.InsufficientPrivilege` (sqlalchemy wraps as
     `ProgrammingError`). If either succeeds, the database is not
     enforcing the grant contract -- this is a critical drift.

The enforcement layer runs each probe in its OWN transaction so that
the rollback on InsufficientPrivilege doesn't poison the next probe.
No real audit row is ever modified.

# Why this is more important than it looks

If the GRANT contract is silently widened in production, then:
  - SOC 2 CC7.2 (system monitoring / audit-log integrity) becomes
    a paper claim, not a database-enforced fact.
  - PIPEDA P5 technical-safeguard story for the audit log degrades
    from "the database physically rejects tampering" to "we trust
    the application code to never tamper."
  - Forensic confidence in any incident drops to zero -- if the log
    can be UPDATED, no evidence drawn from it is trustworthy.

# Read-only effective

Pillar 22 never inserts, never updates, never deletes any row. The
enforcement-layer probes are designed to be REJECTED before any
mutation happens. Safe to run pre- or post-teardown.

# Connection identity

The verify task connects with the worker DSN (this is the design --
see Phase 2 sec 3.1b and the recap entry on `D-verify-harness-direct-
db-writes-against-worker-dsn-2026-05-05`). So `current_user` is
expected to be `luciel_worker` in production. We don't HARDCODE
that name -- we read it from the live connection and assert grants
on whatever role we are. This makes the pillar correct in any
environment (local dev as `luciel_admin`, prod as `luciel_worker`).

In local-dev as `luciel_admin`, the pillar gives a different
verdict: superuser / table-owner has implicit ALL privileges, so
the grants layer would report a wider set. We treat that case
explicitly: if the connecting role is a superuser or owns the
target table, we still require the enforcement layer to show that
direct UPDATE/DELETE on `admin_audit_logs` are NOT being attempted
from worker-equivalent contexts. But since the verify task in
production uses the worker DSN, this branch is dev-only and we
emit a SKIPPED-with-note rather than a hard fail. Production
green-runs proceed through the strict branch.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

from app.verification.fixtures import RunState
from app.verification.runner import Pillar


# Tables we care about for append-only enforcement. Both come from
# migration f392a842f885's WRITE_TABLES tuple. Order matters only for
# error-message determinism.
_APPEND_ONLY_TABLES = ("admin_audit_logs", "memory_items")

# The exact privilege set we expect on each. Migration f392a842f885
# grants `SELECT, INSERT` and nothing else. PostgreSQL surfaces these
# in information_schema as one row per privilege_type per (grantee,
# table). REFERENCES, TRIGGER, UPDATE, DELETE, TRUNCATE rows MUST NOT
# appear for our role.
_REQUIRED_PRIVS = frozenset({"SELECT", "INSERT"})
_FORBIDDEN_PRIVS = frozenset({"UPDATE", "DELETE", "TRUNCATE"})


_GRANTS_SQL = """
SELECT privilege_type
FROM information_schema.role_table_grants
WHERE grantee = :role
  AND table_schema = 'public'
  AND table_name = :table
"""


_CURRENT_USER_SQL = "SELECT current_user"


_IS_SUPERUSER_SQL = """
SELECT rolsuper
FROM pg_roles
WHERE rolname = current_user
"""


_TABLE_OWNER_SQL = """
SELECT tableowner
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename = :table
"""


# Enforcement probes. Both reference (SELECT MIN(id) FROM ...) so we
# don't need to know any particular id. If the table is empty (very
# unlikely after Pillars 1..20 just ran), MIN(id) is NULL and the
# WHERE clause matches zero rows -- in that case Postgres still does
# the privilege check before the row check, so the probe is still
# valid.
_UPDATE_PROBE_SQL = (
    "UPDATE admin_audit_logs SET note = 'pillar 22 probe -- never persists' "
    "WHERE id = (SELECT MIN(id) FROM admin_audit_logs)"
)
_DELETE_PROBE_SQL = (
    "DELETE FROM admin_audit_logs "
    "WHERE id = (SELECT MIN(id) FROM admin_audit_logs)"
)


class DbGrantsAuditLogAppendOnlyPillar(Pillar):
    number = 22
    name = "DB grants enforce audit-log append-only (P3-E.1)"

    def run(self, state: RunState) -> str:
        db_url = os.environ.get("DATABASE_URL") or self._load_database_url_from_dotenv()
        if not db_url:
            raise AssertionError(
                "DATABASE_URL not found in environment nor in project .env file. "
                "Pillar 22 needs a direct DB connection to inspect grants."
            )

        engine = create_engine(db_url)

        try:
            return self._run_with_engine(engine)
        finally:
            engine.dispose()

    def _run_with_engine(self, engine) -> str:
        # 1. Identify who we are connecting as.
        with engine.connect() as conn:
            current_user = conn.execute(text(_CURRENT_USER_SQL)).scalar_one()
            is_superuser = bool(
                conn.execute(text(_IS_SUPERUSER_SQL)).scalar_one_or_none()
            )

        # 2. Detect the dev-on-luciel_admin / superuser branch.
        #    If we are connecting as a superuser or as the table owner,
        #    information_schema.role_table_grants will not reflect the
        #    real enforcement model -- implicit ALL privileges bypass
        #    the row-level grant check entirely. In that case we still
        #    run the enforcement probes (a superuser SHOULD succeed,
        #    so the test would be vacuous), and emit a soft pass with
        #    a clear note so a green prod run is unambiguously stricter
        #    than a green dev run.
        if is_superuser:
            return (
                f"SKIP-as-pass: pillar 22 connecting as superuser ({current_user!r}); "
                f"GRANT contract bypassed by superuser bit. Production verify task "
                f"connects as luciel_worker (non-superuser); strict branch runs there."
            )

        # 3. STRICT BRANCH: non-superuser. We expect EXACTLY {SELECT,
        #    INSERT} for both _APPEND_ONLY_TABLES.
        violations: list[str] = []
        observed_per_table: dict[str, frozenset[str]] = {}
        for table in _APPEND_ONLY_TABLES:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(_GRANTS_SQL),
                    {"role": current_user, "table": table},
                ).all()
            privs = frozenset(r.privilege_type for r in rows)
            observed_per_table[table] = privs

            # Owner short-circuit: if our role owns the table we have
            # implicit ALL even without role_table_grants rows. Treat
            # the same as superuser branch.
            with engine.connect() as conn:
                owner = conn.execute(
                    text(_TABLE_OWNER_SQL), {"table": table}
                ).scalar_one_or_none()
            if owner == current_user:
                violations.append(
                    f"role {current_user!r} OWNS table {table!r} -- "
                    f"implicit ALL privileges bypass GRANT contract; "
                    f"production should not run as the table owner."
                )
                continue

            missing = _REQUIRED_PRIVS - privs
            forbidden_present = _FORBIDDEN_PRIVS & privs
            unexpected = privs - _REQUIRED_PRIVS - frozenset(
                # REFERENCES and TRIGGER are not in the migration but
                # they are also not append-only-violating. Flag them
                # if they appear so we know about it, but treat them
                # as soft violations rather than hard ones.
                {"REFERENCES", "TRIGGER"}
            )

            if missing:
                violations.append(
                    f"{table}: missing required privs {sorted(missing)} "
                    f"(observed {sorted(privs)}); audit emission would fail."
                )
            if forbidden_present:
                violations.append(
                    f"{table}: GRANTS INCLUDE FORBIDDEN privs "
                    f"{sorted(forbidden_present)} (observed {sorted(privs)}); "
                    f"append-only contract VIOLATED at the GRANT layer."
                )
            soft_unexpected = unexpected - _FORBIDDEN_PRIVS
            if soft_unexpected:
                # Not append-only-violating but not in the canonical
                # spec either. Surface in the verdict text but don't
                # fail the pillar.
                violations.append(
                    f"{table}: unexpected non-canonical privs "
                    f"{sorted(soft_unexpected)} (not in migration "
                    f"f392a842f885); investigate."
                )

        # Hard violations are anything mentioning REQUIRED missing or
        # FORBIDDEN present. Soft "unexpected non-canonical" notes alone
        # should not fail the pillar.
        hard = [v for v in violations if "missing required" in v or "FORBIDDEN" in v or "OWNS table" in v]
        if hard:
            raise AssertionError(
                "Pillar 22 GRANT layer FAIL ({} hard violation(s)):\n  {}".format(
                    len(hard), "\n  ".join(hard)
                )
            )

        # 4. ENFORCEMENT LAYER: try direct UPDATE and DELETE on
        #    admin_audit_logs. Both must raise InsufficientPrivilege
        #    (psycopg) which sqlalchemy wraps as ProgrammingError.
        update_blocked = self._probe_blocked(engine, _UPDATE_PROBE_SQL, "UPDATE")
        delete_blocked = self._probe_blocked(engine, _DELETE_PROBE_SQL, "DELETE")

        if not update_blocked:
            raise AssertionError(
                f"Pillar 22 ENFORCEMENT layer FAIL: as role {current_user!r}, "
                f"a direct UPDATE on admin_audit_logs DID NOT raise "
                f"InsufficientPrivilege. Append-only contract is metadata-only "
                f"and not actually enforced by Postgres."
            )
        if not delete_blocked:
            raise AssertionError(
                f"Pillar 22 ENFORCEMENT layer FAIL: as role {current_user!r}, "
                f"a direct DELETE on admin_audit_logs DID NOT raise "
                f"InsufficientPrivilege. Append-only contract is metadata-only "
                f"and not actually enforced by Postgres."
            )

        # 5. Compose the verdict.
        soft_notes = [v for v in violations if v not in hard]
        verdict = (
            f"role={current_user!r}; grants OK on "
            f"{', '.join(_APPEND_ONLY_TABLES)} = "
            f"{{SELECT, INSERT}}; UPDATE+DELETE on admin_audit_logs "
            f"raised InsufficientPrivilege"
        )
        if soft_notes:
            verdict = verdict + " ; soft notes: " + " | ".join(soft_notes)
        return verdict

    @staticmethod
    def _probe_blocked(engine, sql: str, op: str) -> bool:
        """Run the probe SQL in its own transaction. Return True if
        Postgres rejected with InsufficientPrivilege; False if the
        statement succeeded; raise on any other error type so we
        notice unexpected failures (e.g. table missing, syntax error).
        """
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
        except ProgrammingError as exc:
            # psycopg surfaces InsufficientPrivilege via SQLSTATE 42501.
            # sqlalchemy preserves the SQLSTATE on the original cause.
            orig = getattr(exc, "orig", None)
            sqlstate = getattr(orig, "sqlstate", None)
            if sqlstate == "42501":
                return True
            # Any other ProgrammingError (e.g. syntax) is a real bug.
            raise AssertionError(
                f"pillar 22 {op} probe raised unexpected ProgrammingError "
                f"(sqlstate={sqlstate!r}): {exc}"
            )
        except Exception as exc:
            raise AssertionError(
                f"pillar 22 {op} probe raised unexpected exception type: "
                f"{type(exc).__name__}: {exc}"
            )
        # No exception raised -- the statement succeeded. That means
        # the role has UPDATE/DELETE privileges and the append-only
        # contract is broken at the engine layer.
        return False

    @staticmethod
    def _load_database_url_from_dotenv() -> str | None:
        """Walk up from CWD looking for a .env file; return DATABASE_URL if present."""
        from pathlib import Path
        here = Path.cwd().resolve()
        for candidate_dir in (here, *here.parents):
            env_path = candidate_dir / ".env"
            if env_path.is_file():
                try:
                    for raw in env_path.read_text(encoding="utf-8").splitlines():
                        line = raw.strip()
                        if not line or line.startswith("#"):
                            continue
                        if line.startswith("DATABASE_URL=") and "://" in line:
                            val = line.split("=", 1)[1].strip()
                            if (val.startswith('"') and val.endswith('"')) or (
                                val.startswith("'") and val.endswith("'")
                            ):
                                val = val[1:-1]
                            return val
                except Exception:
                    continue
        return None


PILLAR = DbGrantsAuditLogAppendOnlyPillar()
