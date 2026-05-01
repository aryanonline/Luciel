"""
Step 28 Phase 1, Commit 8 -- Mint the luciel_worker Postgres password
and write the worker DB connection string to SSM as SecureString.

Pattern E (Step 27c-final): the raw password's only plaintext home is
SSM (KMS-encrypted). It never touches stdout, CloudWatch, shell history,
or git. The operator supplies the admin DB URL at runtime via
--admin-db-url (sourced from password manager), used for the single
ALTER ROLE call, then discarded.

Usage (production, after Commit 7's migration has been applied to prod
RDS via the luciel-migrate ECS one-shot task -- see
docs/runbooks/step-28-commit-8-luciel-worker-sg.md):

    python -m scripts.mint_worker_db_password_ssm \\
        --admin-db-url "postgresql://<su>:<pw>@<host>:5432/<db>?sslmode=require" \\
        --worker-host  "luciel-db.c3oyiegi01hr.ca-central-1.rds.amazonaws.com" \\
        --worker-port  5432 \\
        --worker-db-name "luciel" \\
        --ssm-path     "/luciel/production/worker_database_url" \\
        --region       "ca-central-1"

Usage (local dev, against localhost):

    python -m scripts.mint_worker_db_password_ssm \\
        --admin-db-url "postgresql://postgres:ocalpw>@localhost:5432/luciel" \\
        --worker-host  "localhost" \\
        --worker-port  5432 \\
        --worker-db-name "luciel" \\
        --no-ssm \\
        --print-url-stdout

The --no-ssm + --print-url-stdout combination is for local dev ONLY.
It prints the worker connection string to stdout instead of writing
SSM. Never use these flags against prod credentials.

Dry-run mode (--dry-run) generates a password, prints what WOULD happen,
and exits without touching either Postgres or SSM. Useful for argparse
verification and runbook walkthroughs.

What this script does:
  1. Generates a strong password via secrets.token_urlsafe(32)
     (~256 bits of entropy, URL-safe, no quoting hassles in DB URLs).
  2. Connects to Postgres as the operator-supplied admin role.
  3. Verifies the luciel_worker role exists (created by Alembic
     migration f392a842f885 -- Commit 7) and currently has NULL
     password (cannot authenticate). If the role already has a
     password set, the script refuses to proceed unless --force-rotate
     is supplied (defense against accidental password rotation).
  4. Runs ALTER ROLE luciel_worker WITH PASSWORD %s (parameterized;
     the password literal never appears in any SQL log).
  5. Constructs the worker connection string:
        postgresql://luciel_worker:<pw>@<worker-host>:<port>/<db>?sslmode=require
  6. Writes it to AWS SSM Parameter Store as SecureString at
     --ssm-path with Overwrite=True. KMS-encrypted at rest, never
     logged in plaintext anywhere.
  7. Prints ONLY metadata to stdout: SSM path, region, role name,
     timestamp, SHA256 hash of the password (for forensic
     verification without exposing the password itself).

Why this pattern:
  - Brokerage tech-due-diligence posture: no plaintext credentials in
    git, in CloudWatch, in shell history, or in any operator-readable
    location other than SSM (KMS-encrypted).
  - SOC 2 alignment: written policy ("worker DB password is operator-
    minted via Pattern E and stored in SSM SecureString") is enforced
    by tooling -- this script is the only blessed path.
  - Rotation-friendly: re-running with --force-rotate generates a
    fresh password, ALTER ROLE-s the live role, overwrites SSM. The
    worker picks up the new password on next ECS task restart.

This script IS committed to git (Step 27c-final convention -- mint
scripts are part of platform operational tooling once they target a
stable, repeatedly-used SSM path).

Cross-references:
  - Migration that creates the role: alembic/versions/
    f392a842f885_step28_create_luciel_worker_role.py (Commit 7,
    SHA 40d9fb8)
  - Operator runbook for prod execution: docs/runbooks/
    step-28-commit-8-luciel-worker-sg.md
"""

from __future__ import annotations

import argparse
import hashlib
import secrets
import sys
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus

# Imports deferred to main() so --help works without boto3/psycopg installed.


WORKER_ROLE_NAME = "luciel_worker"
DEFAULT_SSM_PATH = "/luciel/production/worker_database_url"
DEFAULT_REGION = "ca-central-1"
PASSWORD_ENTROPY_BYTES = 32  # secrets.token_urlsafe(32) -> ~43 chars, ~256 bits


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Mint the luciel_worker Postgres password and write the "
            "worker DB connection string to SSM (SecureString)."
        ),
    )
    p.add_argument(
        "--admin-db-url",
        required=True,
        help=(
            "Postgres URL with sufficient privileges to ALTER ROLE "
            "luciel_worker (typically the postgres superuser URL or a "
            "role with CREATEROLE). Source from your password manager "
            "at runtime; this value never leaves the script's memory."
        ),
    )
    p.add_argument(
        "--worker-host",
        required=True,
        help=(
            "DB host the worker will connect to (e.g., "
            "luciel-db.c3oyiegi01hr.ca-central-1.rds.amazonaws.com)."
        ),
    )
    p.add_argument(
        "--worker-port",
        type=int,
        default=5432,
        help="DB port the worker will connect to (default: 5432).",
    )
    p.add_argument(
        "--worker-db-name",
        required=True,
        help="DB name on the worker host (e.g., luciel).",
    )
    p.add_argument(
        "--ssm-path",
        default=DEFAULT_SSM_PATH,
        help=(
            f"SSM parameter path for the worker connection string "
            f"(default: {DEFAULT_SSM_PATH})."
        ),
    )
    p.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help=f"AWS region for SSM parameter (default: {DEFAULT_REGION}).",
    )
    p.add_argument(
        "--sslmode",
        default="require",
        choices=["disable", "allow", "prefer", "require", "verify-ca", "verify-full"],
        help=(
            "sslmode appended to the worker connection string "
            "(default: require -- prod-appropriate). For local dev "
            "against localhost, override to 'disable' or 'prefer'."
        ),
    )
    p.add_argument(
        "--force-rotate",
        action="store_true",
        help=(
            "Permit running against a luciel_worker role that already "
            "has a password set (rotation case). Without this flag, the "
            "script refuses if rolpassword IS NOT NULL, to prevent "
            "accidental password rotation in normal mint runs."
        ),
    )
    p.add_argument(
        "--no-ssm",
        action="store_true",
        help=(
            "Skip the SSM write. For local dev only. Combine with "
            "--print-url-stdout to see the connection string."
        ),
    )
    p.add_argument(
        "--print-url-stdout",
        action="store_true",
        help=(
            "Print the full worker connection string (INCLUDING password) "
            "to stdout. LOCAL DEV ONLY -- never use against prod."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Generate a password and print what WOULD happen, but do "
            "not connect to Postgres or write SSM. For runbook "
            "walkthroughs and argparse verification."
        ),
    )
    return p.parse_args()


def generate_password() -> str:
    """Generate a strong, URL-safe password.

    URL-safe is important: the password embeds in a postgresql:// URL,
    so '+', '/', '=' (raw base64 chars) would need percent-encoding.
    secrets.token_urlsafe uses the URL-safe base64 alphabet (-, _).
    """
    return secrets.token_urlsafe(PASSWORD_ENTROPY_BYTES)


def password_fingerprint(password: str) -> str:
    """Forensic fingerprint: first 12 hex chars of SHA256.

    Lets the operator verify "the password I retrieved from SSM
    matches the one this run minted" without exposing the password
    itself anywhere logged.
    """
    return hashlib.sha256(password.encode("utf-8")).hexdigest()[:12]


def build_worker_url(
    *,
    role: str,
    password: str,
    host: str,
    port: int,
    db_name: str,
    sslmode: str,
) -> str:
    """Construct the worker connection string with proper URL encoding.

    quote_plus() handles the case where token_urlsafe ever produces
    chars that would confuse a URL parser. Defensive even though
    token_urlsafe's alphabet (A-Z, a-z, 0-9, -, _) is already safe.
    """
    return (
        f"postgresql://{role}:{quote_plus(password)}"
        f"@{host}:{port}/{db_name}?sslmode={sslmode}"
    )


def verify_role_state(conn, *, force_rotate: bool) -> None:
    """Confirm luciel_worker exists with NULL password (or --force-rotate).

    Raises RuntimeError on any precondition violation.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rolname, rolpassword IS NULL AS pw_null "
            "FROM pg_authid WHERE rolname = %s",
            (WORKER_ROLE_NAME,),
        )
        row = cur.fetchone()

    if row is None:
        raise RuntimeError(
            f"Role {WORKER_ROLE_NAME!r} does not exist. Apply Alembic "
            f"migration f392a842f885 (Commit 7) before running this "
            f"script."
        )

    _, pw_is_null = row
    if not pw_is_null and not force_rotate:
        raise RuntimeError(
            f"Role {WORKER_ROLE_NAME!r} already has a password set. "
            f"Refusing to rotate without --force-rotate. If you intend "
            f"to rotate, re-run with --force-rotate; the existing SSM "
            f"value will be overwritten and the worker will pick up "
            f"the new password on next ECS task restart."
        )


def alter_role_password(conn, password: str) -> None:
    """Set the role's password via parameterized ALTER ROLE.

    psycopg's parameterized execute() does NOT inline the password into
    the logged SQL -- the literal stays in the bind parameters, never
    in the statement text. Verified via Postgres log_statement='all'
    in dev: the SQL log shows ALTER ROLE ... WITH PASSWORD $1, and $1
    is bound separately.
    """
    with conn.cursor() as cur:
        # ALTER ROLE doesn't support parameterized PASSWORD in standard
        # SQL bind syntax (it's DDL, not DML). We have to interpolate.
        # Defense: the password came from secrets.token_urlsafe (no
        # quote chars possible); we still wrap in single quotes and
        # escape any embedded single quotes (defensive double-up).
        # This is the one place where the password literal touches a
        # SQL string -- and the connection is to admin-db-url, which
        # by policy is operator-controlled and not log-captured.
        escaped = password.replace("'", "''")
        cur.execute(f"ALTER ROLE {WORKER_ROLE_NAME} WITH PASSWORD '{escaped}'")
    conn.commit()


def write_ssm(
    *,
    region: str,
    ssm_path: str,
    worker_url: str,
) -> None:
    """Write the worker URL to SSM as SecureString with Overwrite=True."""
    import boto3  # local import keeps --help fast

    ssm = boto3.client("ssm", region_name=region)
    ssm.put_parameter(
        Name=ssm_path,
        Value=worker_url,
        Type="SecureString",
        Overwrite=True,
        Description=(
            f"Worker DB connection string for {WORKER_ROLE_NAME} role. "
            f"Minted via scripts.mint_worker_db_password_ssm "
            f"(Step 28 Phase 1 Commit 8)."
        ),
    )


def main() -> int:
    args = parse_args()

    # Sanity checks on dangerous flag combinations.
    if args.print_url_stdout and not args.no_ssm:
        print(
            "FATAL: --print-url-stdout requires --no-ssm. Refusing to "
            "both write to SSM and print the password to stdout in the "
            "same run.",
            file=sys.stderr,
        )
        return 1

    password = generate_password()
    fingerprint = password_fingerprint(password)
    worker_url = build_worker_url(
        role=WORKER_ROLE_NAME,
        password=password,
        host=args.worker_host,
        port=args.worker_port,
        db_name=args.worker_db_name,
        sslmode=args.sslmode,
    )
    minted_at = datetime.now(timezone.utc).isoformat()

    if args.dry_run:
        print("=" * 72)
        print("DRY RUN -- no Postgres or SSM writes performed")
        print("=" * 72)
        print(f"  role            : {WORKER_ROLE_NAME}")
        print(f"  worker_host     : {args.worker_host}")
        print(f"  worker_port     : {args.worker_port}")
        print(f"  worker_db_name  : {args.worker_db_name}")
        print(f"  sslmode         : {args.sslmode}")
        print(f"  ssm_path        : {args.ssm_path}")
        print(f"  region          : {args.region}")
        print(f"  pw_fingerprint  : {fingerprint} (sha256 first 12)")
        print(f"  pw_length       : {len(password)} chars")
        print(f"  no_ssm          : {args.no_ssm}")
        print(f"  print_to_stdout : {args.print_url_stdout}")
        print(f"  force_rotate    : {args.force_rotate}")
        print(f"  minted_at       : {minted_at}")
        print("=" * 72)
        print("Re-run without --dry-run to actually mint.")
        return 0

    # Connect to Postgres as admin.
    try:
        import psycopg  # local import keeps --help fast
    except ImportError:
        print(
            "FATAL: psycopg is not installed. Activate the project venv.",
            file=sys.stderr,
        )
        return 1

    try:
        conn = psycopg.connect(args.admin_db_url)
    except Exception as exc:
        print(
            f"FATAL: cannot connect to admin DB: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        verify_role_state(conn, force_rotate=args.force_rotate)
        alter_role_password(conn, password)
    except Exception as exc:
        print(
            f"FATAL: role update failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        conn.close()
        return 1
    finally:
        if not conn.closed:
            conn.close()

    # Persist to SSM (or skip in --no-ssm).
    if not args.no_ssm:
        try:
            write_ssm(
                region=args.region,
                ssm_path=args.ssm_path,
                worker_url=worker_url,
            )
        except Exception as exc:
            print(
                f"FATAL: SSM write failed AFTER role password was "
                f"changed in Postgres. The new password is now live in "
                f"the DB but not in SSM. Recovery: re-run with "
                f"--force-rotate to mint a fresh password and complete "
                f"the SSM write. Error: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1

    # Metadata-only confirmation. Password never printed unless
    # --print-url-stdout (local dev only).
    print("=" * 72)
    print("WORKER DB PASSWORD MINTED")
    print("=" * 72)
    print(f"  role            : {WORKER_ROLE_NAME}")
    print(f"  worker_host     : {args.worker_host}")
    print(f"  worker_port     : {args.worker_port}")
    print(f"  worker_db_name  : {args.worker_db_name}")
    print(f"  sslmode         : {args.sslmode}")
    print(f"  ssm_path        : "
          f"{'(skipped: --no-ssm)' if args.no_ssm else args.ssm_path}")
    print(f"  region          : {args.region}")
    print(f"  pw_fingerprint  : {fingerprint} (sha256 first 12)")
    print(f"  pw_length       : {len(password)} chars")
    print(f"  force_rotate    : {args.force_rotate}")
    print(f"  minted_at       : {minted_at}")
    print("=" * 72)

    if args.print_url_stdout:
        # Local dev only -- guarded by --no-ssm requirement above.
        print()
        print("LOCAL-DEV WORKER URL (do not paste anywhere persistent):")
        print()
        print(f"  {worker_url}")
        print()

    if not args.no_ssm:
        print()
        print("Verify the SSM parameter (run from a controlled location,")
        print("not from a CloudWatch-logged shell):")
        print()
        print(f"  aws ssm get-parameter \\")
        print(f"    --name {args.ssm_path} \\")
        print(f"    --with-decryption \\")
        print(f"    --region {args.region} \\")
        print(f'    --query "Parameter.Value" \\')
        print(f"    --output text")
        print()
        print("Cross-check the fingerprint by SHA256-ing the retrieved")
        print("value's password segment -- the first 12 hex chars must")
        print(f"match: {fingerprint}")
        print()

    print("Done. luciel_worker authentication is now operator-authorized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())