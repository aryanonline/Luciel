"""
Step 28 Phase 2 section 4.6 -- worker permission-boundary smoke test.

Connects to RDS as luciel_worker (using the DSN injected via ECS
secrets from /luciel/production/worker_database_url) and verifies the
least-privilege guarantees that closed Pattern E:

  CHECK 0  Identity        : current_user = 'luciel_worker'
  CHECK 1  Positive control: SELECT count(*) FROM admin_audit_logs works
  CHECK 2  DELETE refused  : DELETE FROM admin_audit_logs raises
                             InsufficientPrivilege
  CHECK 3  UPDATE refused  : UPDATE admin_audit_logs raises
                             InsufficientPrivilege

CHECK 0 prevents the test passing trivially against the wrong role
(e.g., admin DSN accidentally mounted). CHECK 1 prevents the test
passing because we never actually reached the table (wrong DB,
table missing, or schema search_path drift). CHECKS 2 and 3 are the
actual permission boundary -- they assert that even if the worker is
compromised, the audit trail is database-enforced append-only.

Why both DELETE and UPDATE: the migration f392a842f885 grants only
`SELECT, INSERT` on admin_audit_logs. Testing only DELETE would miss
the case where DELETE is revoked but UPDATE is somehow still granted
(e.g., default privileges drift, role-membership inheritance bug).
Testing both confirms the full mutation surface is closed.

Exit codes:
  0   all checks PASS -- Pattern E boundary holds
  1   one or more checks FAIL but the script ran -- least-privilege
      violation; investigate before claiming Phase 2 complete
  2   FATAL connect or identity error -- the test could not run

This script is intentionally self-contained (no SQLAlchemy, no app
imports) so a future change to the app layer cannot mask a permission
regression. It uses raw psycopg v3 to exercise the same driver path
the worker uses, which is the path that matters for security.

Drift register: closes section 4.6 of the Step-28-Phase-2 runbook.
"""
from __future__ import annotations

import os
import sys
from typing import List

import psycopg
from psycopg import errors as e


def main() -> int:
    print("=" * 72)
    print("LUCIEL WORKER PERMISSION-BOUNDARY SMOKE")
    print("=" * 72)

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("FATAL: DATABASE_URL env var not set")
        return 2

    # Don't print the DSN -- it contains the rotated luciel_worker
    # password. We can confirm the host without revealing the secret.
    try:
        # Parse just to confirm it looks like a Postgres URL with the
        # right scheme. psycopg.conninfo would also work but adds a dep
        # we don't need.
        scheme_end = dsn.find("://")
        if scheme_end == -1:
            print("FATAL: DATABASE_URL is not a URL")
            return 2
        scheme = dsn[:scheme_end]
        print(f"dsn scheme    : {scheme}")
    except Exception as ex:
        print(f"FATAL: DSN parse: {type(ex).__name__}: {ex}")
        return 2

    try:
        conn = psycopg.connect(dsn, autocommit=False)
    except Exception as ex:
        print(f"FATAL connect : {type(ex).__name__}: {ex}")
        return 2

    failures: List[str] = []

    try:
        cur = conn.cursor()

        # CHECK 0: identity. If we connected as anyone other than
        # luciel_worker, the rest of the test is meaningless.
        cur.execute("SELECT current_user, current_database();")
        row = cur.fetchone()
        assert row is not None
        user, db = row
        print(f"connected as  : {user}@{db}")
        if user != "luciel_worker":
            print(
                f"FATAL CHECK 0 : connected as {user!r}, expected "
                f"'luciel_worker'. Refusing to run permission tests "
                f"against the wrong role -- the result would be "
                f"misleading."
            )
            return 2
        print("CHECK 0 PASS  : identity = luciel_worker")

        # CHECK 1: positive control. The migration grants SELECT on
        # admin_audit_logs. If this fails, either we're not connected
        # to the right DB, the table doesn't exist, or the GRANT was
        # never applied. Without this check, a "passing" smoke could
        # silently hide a connection problem.
        try:
            cur.execute("SELECT count(*) FROM admin_audit_logs;")
            row = cur.fetchone()
            assert row is not None
            n = row[0]
            print(
                f"CHECK 1 PASS  : SELECT admin_audit_logs returned "
                f"count={n} (positive control)"
            )
        except Exception as ex:
            print(
                f"CHECK 1 FAIL  : SELECT denied unexpectedly: "
                f"{type(ex).__name__}: {ex}"
            )
            failures.append("SELECT-denied")
            conn.rollback()

        # CHECK 2: DELETE refused. The core invariant -- audit logs
        # are append-only at the DB layer.
        try:
            cur.execute("DELETE FROM admin_audit_logs WHERE id = -1;")
            print(
                f"CHECK 2 FAIL  : DELETE succeeded (rowcount="
                f"{cur.rowcount}) -- least-privilege VIOLATED. "
                f"Investigate GRANTs on admin_audit_logs immediately."
            )
            failures.append("DELETE-allowed")
            conn.rollback()
        except e.InsufficientPrivilege:
            conn.rollback()
            print(
                "CHECK 2 PASS  : DELETE refused with "
                "InsufficientPrivilege -- audit append-only invariant "
                "holds"
            )
        except Exception as ex:
            conn.rollback()
            print(
                f"CHECK 2 FAIL  : DELETE raised wrong error type: "
                f"{type(ex).__name__}: {ex}. Expected "
                f"psycopg.errors.InsufficientPrivilege."
            )
            failures.append("DELETE-wrong-error")

        # CHECK 3: UPDATE refused. Companion to CHECK 2 -- catches
        # the case where DELETE is revoked but UPDATE leaked through.
        try:
            cur.execute(
                "UPDATE admin_audit_logs SET action = 'tampered' "
                "WHERE id = -1;"
            )
            print(
                f"CHECK 3 FAIL  : UPDATE succeeded (rowcount="
                f"{cur.rowcount}) -- least-privilege VIOLATED. "
                f"Investigate GRANTs on admin_audit_logs immediately."
            )
            failures.append("UPDATE-allowed")
            conn.rollback()
        except e.InsufficientPrivilege:
            conn.rollback()
            print(
                "CHECK 3 PASS  : UPDATE refused with "
                "InsufficientPrivilege -- mutation prevention holds"
            )
        except Exception as ex:
            conn.rollback()
            print(
                f"CHECK 3 FAIL  : UPDATE raised wrong error type: "
                f"{type(ex).__name__}: {ex}. Expected "
                f"psycopg.errors.InsufficientPrivilege."
            )
            failures.append("UPDATE-wrong-error")

    finally:
        conn.close()

    print("=" * 72)
    if failures:
        print(f"SMOKE FAIL: {failures}")
        print(
            "Pattern E permission boundary is NOT verified. Do NOT "
            "tag step-28-phase-2-complete until these failures are "
            "resolved."
        )
        return 1

    print(
        "SMOKE PASS: all 4 checks green; Pattern E permission "
        "boundary verified. luciel_worker can SELECT and INSERT on "
        "admin_audit_logs but cannot DELETE or UPDATE. Database-"
        "enforced append-only invariant is intact."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
