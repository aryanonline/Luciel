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
        postgresql+psycopg://luciel_worker:<pw>@<worker-host>:<port>/<db>?sslmode=require
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
import os
from typing import Optional
import hashlib
import re
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

# SQLAlchemy DSN driver suffixes that raw psycopg.connect() does NOT accept.
# When the operator-supplied --admin-db-url comes from an SSM param that
# was stored in SQLAlchemy form (e.g., the existing /luciel/database-url
# is `postgresql+psycopg://...` because the running backend uses
# SQLAlchemy), we strip the suffix before handing the URL to psycopg.
SQLA_DRIVER_PREFIXES = (
    "postgresql+psycopg://",
    "postgresql+psycopg2://",
    "postgresql+asyncpg://",
)

# Pattern to redact a Postgres URL anywhere it appears in an exception
# message. Captures any postgres scheme (with or without driver suffix),
# any userinfo, host/port, db, query string. Used by
# _redact_dsn_in_message to keep credentials out of stderr/CloudWatch on
# the script's failure paths (see Pattern E discipline notes in module
# docstring).
_DSN_REDACT_RE = re.compile(
    r"postgres(?:ql)?(?:\+\w+)?://[^\s\"']+",
    re.IGNORECASE,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Mint the luciel_worker Postgres password and write the "
            "worker DB connection string to SSM (SecureString)."
        ),
    )
    # Admin DSN can come from THREE input modes:
    #   1. --admin-db-url    : legacy CLI form, retained for local dev.
    #                          Places DSN in process args (visible via
    #                          ps / Get-Process); not for production.
    #   2. --admin-db-url-stdin : production-Option-3 form used by
    #                          scripts/mint-with-assumed-role.ps1.
    #                          Read one line from stdin. P3-K (2026-05-03).
    #   3. ADMIN_DSN env var : production-Pattern-N form used by
    #                          luciel-mint Fargate task. Delivered via
    #                          task-def `secrets:` block, resolved by
    #                          ECS through the SSM endpoint inside the
    #                          VPC. The container never sees the DSN on
    #                          its argv. P3-S.a (2026-05-05).
    # Modes are mutually exclusive: the script picks --admin-db-url first
    # (most explicit), then --admin-db-url-stdin, then ADMIN_DSN env. If
    # none are set the script aborts.
    admin_dsn_group = p.add_mutually_exclusive_group(required=False)
    admin_dsn_group.add_argument(
        "--admin-db-url",
        help=(
            "Postgres URL with sufficient privileges to ALTER ROLE "
            "luciel_worker (typically the postgres superuser URL or a "
            "role with CREATEROLE). Source from your password manager "
            "at runtime; this value never leaves the script's memory. "
            "WARNING: this places the DSN in process args; prefer "
            "--admin-db-url-stdin or ADMIN_DSN env for production."
        ),
    )
    admin_dsn_group.add_argument(
        "--admin-db-url-stdin",
        action="store_true",
        help=(
            "Read the admin DB URL from stdin (one line, trailing "
            "whitespace stripped). Use this in production via the "
            "mint-with-assumed-role.ps1 helper so the DSN never "
            "lands in process args. P3-K (2026-05-03)."
        ),
    )
    # Worker connection params: required at the value level, but resolved
    # from CLI flags OR env vars (WORKER_HOST / WORKER_DB_NAME / WORKER_SSM_PATH).
    # The env-var path is what the luciel-mint Fargate task uses; the CLI-flag
    # path is what the v2 / v2.1 ceremony used. main() resolves both.
    p.add_argument(
        "--worker-host",
        default=None,
        help=(
            "DB host the worker will connect to (e.g., "
            "luciel-db.c3oyiegi01hr.ca-central-1.rds.amazonaws.com). "
            "Falls back to WORKER_HOST env var if not given."
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
        default=None,
        help=(
            "DB name on the worker host (e.g., luciel). Falls back to "
            "WORKER_DB_NAME env var if not given."
        ),
    )
    p.add_argument(
        "--ssm-path",
        default=None,
        help=(
            f"SSM parameter path for the worker connection string. "
            f"Falls back to WORKER_SSM_PATH env var if not given, "
            f"then to {DEFAULT_SSM_PATH}."
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
        default=None,
        help=(
            "Generate a password and print what WOULD happen, but do "
            "not connect to Postgres or write SSM beyond pre-flight "
            "checks (SSM-writable + DB connection-only). The pre-flight "
            "layer was added in P3-S Half 1 (2026-05-05) so dry-runs "
            "actually exercise the IAM and network paths the real run "
            "depends on. Falls back to MINT_DRY_RUN env var (truthy: "
            "'1', 'true', 'yes', case-insensitive) if not given."
        ),
    )
    return p.parse_args()


def _env_truthy(value: Optional[str]) -> bool:
    """Parse a string env value as a boolean. Truthy: 1/true/yes/on.

    Returns False for None / empty / any other value. Case-insensitive.
    Used to resolve MINT_DRY_RUN from the env when --dry-run is not on
    the CLI. Conservative parser by design: anything ambiguous is False,
    which means the caller must opt IN to dry-run, not opt OUT.
    """
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_admin_dsn(args: argparse.Namespace) -> Optional[str]:
    """Pick the admin DSN from CLI flags or env var, in priority order.

    Priority: --admin-db-url > --admin-db-url-stdin > ADMIN_DSN env.
    Returns None if none of the three are present; main() handles the
    abort path. The stdin-mode return is the literal sentinel string
    `__STDIN__`; main() reads stdin only when it sees that sentinel,
    so we don't read stdin during arg resolution (which would block).
    """
    if args.admin_db_url:
        return args.admin_db_url
    if args.admin_db_url_stdin:
        return "__STDIN__"
    env_dsn = os.environ.get("ADMIN_DSN")
    if env_dsn:
        return env_dsn
    return None


def generate_password() -> str:
    """Generate a strong, URL-safe password.

    URL-safe is important: the password embeds in a postgresql:// URL,
    so '+', '/', '=' (raw base64 chars) would need percent-encoding.
    secrets.token_urlsafe uses the URL-safe base64 alphabet (-, _).
    """
    return secrets.token_urlsafe(PASSWORD_ENTROPY_BYTES)


def _redact_dsn_in_message(msg: str) -> str:
    """Redact any Postgres DSN found in an arbitrary message string.

    Pattern E discipline: psycopg's exception messages can include the
    full connection string -- including the password -- when the URL is
    malformed. We MUST scrub before printing to stderr or any logged
    surface. This function replaces every postgres-shaped URL in the
    message with `<DSN-REDACTED>`, preserving the surrounding context so
    the operator still sees what kind of error happened.

    Incident reference: 2026-05-03 mint dry-run leaked admin DSN to
    CloudWatch via psycopg ProgrammingError on `+psycopg` driver
    prefix. See docs/recaps/2026-05-03-mint-incident.md.
    """
    return _DSN_REDACT_RE.sub("<DSN-REDACTED>", msg)


def _strip_sqla_driver_prefix(url: str) -> str:
    """Convert a SQLAlchemy-shaped DSN to a libpq-shaped DSN.

    Raw psycopg.connect() rejects `postgresql+psycopg://` etc. as
    malformed -- the `+driver` suffix is a SQLAlchemy convention, not
    libpq syntax. Strip it so we can hand the URL to psycopg directly.

    No-op if the URL already starts with plain `postgresql://`.
    """
    for prefix in SQLA_DRIVER_PREFIXES:
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return url


def preflight_ssm_writable(*, region: str, ssm_path: str) -> None:
    """Verify the caller can write to ssm_path BEFORE any DB mutation.

    Atomicity defense: the original script ordering was
    (1) ALTER ROLE in DB, then (2) put_parameter to SSM. If step (2)
    failed -- e.g., because the task IAM role lacks ssm:PutParameter --
    the DB password was already changed but SSM had stale or no value,
    leaving the worker unable to authenticate.

    This pre-flight detects the IAM gap before we touch the DB. We use
    GetParameterHistory because it requires both ssm:GetParameter and
    ssm:GetParameterHistory but does NOT require the parameter to
    already exist (it returns ParameterNotFound, which we treat as
    success -- the path is writable, just empty). Any AccessDenied
    response means the caller cannot write either; we abort.

    We deliberately do NOT use a write-then-rollback approach (e.g.,
    put_parameter + delete_parameter) because that would mutate SSM
    history -- exactly what we're trying to keep clean. A read-shaped
    permission check is sufficient because IAM policies that grant
    PutParameter on a path almost always grant GetParameter on the same
    path (the bootstrap-and-verify pattern).

    Raises RuntimeError on AccessDenied, no-op on success or
    ParameterNotFound.
    """
    import boto3  # local import keeps --help fast
    from botocore.exceptions import ClientError

    ssm = boto3.client("ssm", region_name=region)
    try:
        ssm.get_parameter_history(Name=ssm_path, MaxResults=1)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ParameterNotFound":
            # Path is empty -- writable assumption holds. The actual
            # put_parameter will create-or-overwrite cleanly.
            return
        if code in ("AccessDeniedException", "AccessDenied"):
            raise RuntimeError(
                f"SSM pre-flight failed: caller cannot read {ssm_path!r}. "
                f"This almost certainly means the IAM role also lacks "
                f"ssm:PutParameter on this path. Refusing to mutate the "
                f"DB password before SSM write capability is verified. "
                f"Update the task/operator IAM policy to allow "
                f"ssm:GetParameter, ssm:GetParameterHistory, and "
                f"ssm:PutParameter on {ssm_path!r}, then re-run."
            ) from exc
        # Any other error (throttling, region misconfig, etc.) is
        # unexpected; surface it cleanly.
        raise RuntimeError(
            f"SSM pre-flight failed with unexpected error code {code!r}. "
            f"Aborting before any DB mutation."
        ) from exc


def verify_first_mint_or_force_rotate(
    *, region: str, ssm_path: str, force_rotate: bool
) -> None:
    """Distinguish first-mint from rotation via SSM presence.

    Replaces the DB-side `rolpassword IS NULL` check that
    `verify_role_state` used to perform on `pg_authid`. RDS does not
    expose `pg_authid` to `rds_superuser`, so we cannot read the role's
    password-null state directly. Instead we use SSM presence as the
    signal, which directly enforces the tooling contract: this script
    is the only blessed minter, so SSM presence at the worker path
    means this script has minted before, hence rotation, hence
    --force-rotate is required.

    Semantics:
      - SSM `ParameterNotFound`: first-mint, proceed.
      - SSM exists, --force-rotate set: rotation, proceed.
      - SSM exists, --force-rotate NOT set: refuse, raise RuntimeError.

    Out-of-band SQL password sets are not detected by this check (the
    DB password may be set without SSM being populated). That case is
    not part of any approved workflow; the script's invariant
    enforcement is at the tooling layer, not the DB layer. See the
    `verify_role_state` docstring for the full rationale.

    Idempotent if called twice; no AWS state mutation. Same
    `ssm:GetParameter` IAM grant that `preflight_ssm_writable` already
    requires (no new IAM surface).
    """
    import boto3  # local import keeps --help fast
    from botocore.exceptions import ClientError

    ssm = boto3.client("ssm", region_name=region)
    try:
        ssm.get_parameter(Name=ssm_path)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ParameterNotFound":
            # First-mint case. Proceed.
            return
        # Anything else (AccessDenied, throttling, region misconfig) is
        # already surfaced by the prior preflight_ssm_writable call;
        # if we got here, the path is readable and the only legitimate
        # error code is ParameterNotFound. Raise loudly.
        raise RuntimeError(
            f"SSM rotation-guard check failed with unexpected error "
            f"code {code!r}. Aborting before any DB mutation."
        ) from exc

    # Parameter exists. Rotation requires explicit operator intent.
    if not force_rotate:
        raise RuntimeError(
            f"SSM parameter {ssm_path!r} already exists, indicating "
            f"luciel_worker has been minted before. Refusing to rotate "
            f"without --force-rotate. If you intend to rotate, re-run "
            f"with --force-rotate; the existing SSM value will be "
            f"overwritten and the worker will pick up the new password "
            f"on next ECS task restart. (P3-S Half 2: this guard "
            f"replaced the original DB-side pg_authid check, which is "
            f"unreadable on RDS.)"
        )


def password_fingerprint(password: str) -> str:
    """Forensic fingerprint: first 12 hex chars of SHA256.

    Lets the operator verify "the password I retrieved from SSM
    matches the one this run minted" without exposing the password
    itself anywhere logged.
    """
    return hashlib.sha256(password.encode("utf-8")).hexdigest()[:12]


# SQLAlchemy dialect prefix that the runtime worker requires.
# The repo declares `psycopg` (v3) as the Postgres driver in pyproject.toml.
# SQLAlchemy's default dialect resolution for the bare `postgresql://` scheme
# is `postgresql+psycopg2`, which fails with `ModuleNotFoundError: psycopg2`
# inside the worker container (the v2 driver is not installed).
# We MUST emit `postgresql+psycopg://` so SQLAlchemy loads the v3 driver.
#
# Drift: D-mint-script-emits-bare-postgresql-scheme-incompatible-with-psycopg-v3-2026-05-05
# Caught: 2026-05-05 P3-S Half 2 Step 6 (section 4.4) -- worker rev 6 deploy crashed
# on `from app.db.session import SessionLocal` -> create_engine(...) ->
# import psycopg2 -> ModuleNotFoundError. Two rev-6 tasks failed before
# circuit-breaker triggered explicit rollback. Zero customer impact:
# rev 5 stayed healthy throughout.
#
# Fix: prepend the SQLAlchemy dialect driver explicitly. This matches the
# scheme the existing /luciel/database-url admin DSN uses (verified
# empirically by the rev-5 backend's 8-day uptime against psycopg v3).
WORKER_DSN_SCHEME = "postgresql+psycopg://"


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

    Scheme is `postgresql+psycopg://` (NOT bare `postgresql://`) because
    the runtime worker uses SQLAlchemy with psycopg v3. See WORKER_DSN_SCHEME
    above for full rationale and drift reference.
    """
    return (
        f"{WORKER_DSN_SCHEME}{role}:{quote_plus(password)}"
        f"@{host}:{port}/{db_name}?sslmode={sslmode}"
    )


def verify_role_state(conn) -> None:
    """Confirm luciel_worker exists in Postgres.

    RDS NOTE (P3-S Half 2 fix, 2026-05-05): the original implementation
    queried `pg_authid` for `rolpassword IS NULL` to distinguish first-
    mint from rotation. On AWS RDS the `pg_authid` catalog table is NOT
    readable by `rds_superuser` (the master role) -- only true Postgres
    superusers (which RDS does not grant) can SELECT from it. Attempting
    the original query against prod RDS fails with
    `InsufficientPrivilege: permission denied for table pg_authid`
    (drift `D-mint-script-uses-pg-authid-not-readable-on-rds-2026-05-05`).

    The replacement strategy:
      1. Existence check moves to `pg_roles`, the RDS-readable sanitized
         view. Same semantics, RDS-native.
      2. The first-mint-vs-rotation guard moves OUT of this function and
         INTO the SSM-side check in `verify_first_mint_or_force_rotate`
         (called from `main()` after `preflight_ssm_writable`). The SSM
         signal is actually stronger than the original DB signal because
         it directly enforces the "this script is the only blessed
         minter" tooling contract: SSM-presence at the worker path means
         this script has minted before. Out-of-band manual SQL password
         sets bypass both checks; that case is not part of any approved
         workflow, and the recovery state (script overwrites with a
         fresh script-minted password and captures it in SSM) is the
         desired state -- restoring the single-minter invariant.

    Raises RuntimeError if the role does not exist.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = %s",
            (WORKER_ROLE_NAME,),
        )
        row = cur.fetchone()

    if row is None:
        raise RuntimeError(
            f"Role {WORKER_ROLE_NAME!r} does not exist. Apply Alembic "
            f"migration f392a842f885 (Commit 7) before running this "
            f"script."
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

    # ----- Resolve env-var fallbacks (Pattern N / luciel-mint Fargate) -----
    # The CLI-flag path (v2 / v2.1 Option 3 ceremony) and the env-var path
    # (Pattern N P3-S.a Fargate) coexist. CLI flags take precedence; env
    # vars are the fallback. Resolve here, then proceed as if the values
    # had always been on the CLI.
    if args.worker_host is None:
        args.worker_host = os.environ.get("WORKER_HOST")
    if args.worker_db_name is None:
        args.worker_db_name = os.environ.get("WORKER_DB_NAME")
    if args.ssm_path is None:
        args.ssm_path = os.environ.get("WORKER_SSM_PATH") or DEFAULT_SSM_PATH
    if args.dry_run is None:
        args.dry_run = _env_truthy(os.environ.get("MINT_DRY_RUN"))

    # Required-value gates (now applied post-resolution; argparse no longer
    # enforces required=True on these because the env path is legitimate).
    missing: list[str] = []
    if not args.worker_host:
        missing.append("--worker-host or WORKER_HOST env")
    if not args.worker_db_name:
        missing.append("--worker-db-name or WORKER_DB_NAME env")
    if missing:
        print(
            f"FATAL: missing required value(s): {'; '.join(missing)}.",
            file=sys.stderr,
        )
        return 1

    admin_dsn_source = _resolve_admin_dsn(args)
    if admin_dsn_source is None:
        print(
            "FATAL: admin DSN not provided. Use --admin-db-url, "
            "--admin-db-url-stdin, or set ADMIN_DSN env (Pattern N).",
            file=sys.stderr,
        )
        return 1

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

    # Resolve admin DSN BEFORE the dry-run branch, because the dry-run
    # now exercises a connection-only psycopg.connect(...).close() so
    # we can verify network reachability and credential validity without
    # any state mutation. P3-S Half 1 (2026-05-05); resolves drift
    # `D-mint-script-dry-run-skips-preflight-2026-05-04`.
    if admin_dsn_source == "__STDIN__":
        # Read one line from stdin; strip trailing whitespace only.
        # Empty input is a hard error; we deliberately do not echo
        # what was read (length-only feedback is the helper's job).
        raw_stdin = sys.stdin.readline().rstrip("\r\n")
        if not raw_stdin:
            print(
                "FATAL: --admin-db-url-stdin set but stdin was empty.",
                file=sys.stderr,
            )
            return 1
        if len(raw_stdin) < 20:
            # Sanity bound -- a real Postgres DSN is comfortably > 20
            # chars. This catches an accidental stray newline or empty
            # echo that bypasses the empty-string check above without
            # echoing whatever was actually piped in.
            print(
                "FATAL: --admin-db-url-stdin received fewer than 20 "
                "chars; refusing to proceed.",
                file=sys.stderr,
            )
            return 1
        admin_dsn_input = raw_stdin
    else:
        admin_dsn_input = admin_dsn_source

    # Strip SQLAlchemy driver suffix if present. The existing
    # /luciel/database-url SSM param is stored in SQLAlchemy form
    # (postgresql+psycopg://...) because the running backend uses
    # SQLAlchemy; raw psycopg requires plain postgresql://.
    admin_dsn = _strip_sqla_driver_prefix(admin_dsn_input)

    # Import psycopg now (it's needed by both dry-run preflight and real run).
    try:
        import psycopg  # local import keeps --help fast
    except ImportError:
        print(
            "FATAL: psycopg is not installed. Activate the project venv.",
            file=sys.stderr,
        )
        return 1

    # ----- Pre-flight layer (runs in BOTH dry-run and real-run paths) -----
    # Layer 1: SSM-writable. Skipped in --no-ssm (local dev, no SSM at all).
    if not args.no_ssm:
        try:
            preflight_ssm_writable(region=args.region, ssm_path=args.ssm_path)
        except RuntimeError as exc:
            # Pre-flight messages are operator-targeted and contain no
            # credentials, but redact defensively in case a downstream
            # change ever embeds a DSN in one.
            print(
                f"FATAL: {_redact_dsn_in_message(str(exc))}",
                file=sys.stderr,
            )
            return 1

        # Layer 1b: First-mint-vs-rotation guard via SSM presence. Replaces
        # the original DB-side `rolpassword IS NULL` check on `pg_authid`,
        # which is unreadable by `rds_superuser` on AWS RDS. See
        # `verify_first_mint_or_force_rotate` and `verify_role_state`
        # docstrings for the full rationale (drift
        # `D-mint-script-uses-pg-authid-not-readable-on-rds-2026-05-05`).
        try:
            verify_first_mint_or_force_rotate(
                region=args.region,
                ssm_path=args.ssm_path,
                force_rotate=args.force_rotate,
            )
        except RuntimeError as exc:
            print(
                f"FATAL: {_redact_dsn_in_message(str(exc))}",
                file=sys.stderr,
            )
            return 1

    # Layer 2: DB connect + role-existence read. Open a connection to admin
    # RDS, run the read-only `verify_role_state` SELECT against `pg_roles`,
    # then close. Both halves run in BOTH dry-run and real-run paths.
    #
    # History:
    #   * v2 / v2.1 dry-runs skipped the connect entirely. Drift
    #     `D-option-3-ceremony-cannot-reach-private-rds-from-laptop-
    #     2026-05-04` survived undetected through every prior smoke test
    #     because of that gap.
    #   * P3-S Half 1 patch (2026-05-05) added a connect-only step but did
    #     NOT execute any SQL. That patch was necessary but not sufficient:
    #     `verify_role_state`'s original `pg_authid` SELECT then failed for
    #     the FIRST TIME in Step 5 real-run on `luciel-mint:2`, after the
    #     dry-run on the same task-def had reported GREEN (drift
    #     `D-dry-run-validates-subset-of-real-run-pg-authid-not-exercised-
    #     2026-05-05`).
    #   * P3-S Half 2 patch (this commit, 2026-05-05) extends pre-flight to
    #     also run `verify_role_state(conn)`. The SELECT is read-only by
    #     construction (no transaction is opened, no DDL/DML, just a
    #     parameterized SELECT against the sanitized `pg_roles` view), so
    #     it is safe to run in dry-run. This locks in the invariant that
    #     dry-run = real-run minus state-mutating calls.
    try:
        _preflight_conn = psycopg.connect(admin_dsn)
        try:
            verify_role_state(_preflight_conn)
        finally:
            _preflight_conn.close()
    except Exception as exc:
        # Pattern E: psycopg's exception messages frequently embed the
        # full connection string -- including the password -- when the
        # URL is malformed. Redact before printing.
        sanitized = _redact_dsn_in_message(f"{type(exc).__name__}: {exc}")
        print(
            f"FATAL: pre-flight DB connect + role-state check failed: "
            f"{sanitized}",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        print("=" * 72)
        print("DRY RUN -- no Postgres or SSM writes performed")
        print(
            "  (pre-flight SSM-writable + first-mint-or-force-rotate + "
            "DB connect + role-state PASSED)"
        )
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
        print("Re-run without --dry-run (or with MINT_DRY_RUN=false) to actually mint.")
        return 0

    # Real-run admin connection. Pre-flight above already verified
    # connectivity, so this is an expected-success path; we still
    # wrap it to preserve Pattern E redaction in the failure message.
    try:
        conn = psycopg.connect(admin_dsn)
    except Exception as exc:
        # CRITICAL Pattern E: psycopg's exception messages frequently
        # embed the full connection string -- including the password --
        # when the URL is malformed. Redact before printing to any
        # surface that could be logged (stderr -> CloudWatch via the
        # awslogs driver, or shell history, or operator paste-back).
        # Incident: 2026-05-03 mint attempt leaked admin DSN this way.
        sanitized = _redact_dsn_in_message(f"{type(exc).__name__}: {exc}")
        print(
            f"FATAL: cannot connect to admin DB: {sanitized}",
            file=sys.stderr,
        )
        return 1

    try:
        # Note: verify_role_state(conn) was already executed during pre-flight
        # against `_preflight_conn`. We do NOT re-run it here -- the role
        # cannot have been dropped between the pre-flight close and the
        # real-run open without a concurrent administrative action, which
        # is out of scope for this script's threat model. Running it twice
        # would only add noise to CloudWatch.
        alter_role_password(conn, password)
    except Exception as exc:
        # Same Pattern E discipline as the connect path -- DB error
        # messages can sometimes echo connection metadata.
        sanitized = _redact_dsn_in_message(f"{type(exc).__name__}: {exc}")
        print(
            f"FATAL: role update failed: {sanitized}",
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