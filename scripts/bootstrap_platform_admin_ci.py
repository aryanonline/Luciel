"""
CI-only platform-admin bootstrap.

Step 30d, Deliverable C.

Purpose
=======

Mint a single platform-admin API key against a freshly-migrated test
database and print the raw key to stdout, so a GitHub Actions
workflow can capture it into a shell variable for the rest of the
widget-surface E2E harness.

This is NOT a production tool. Production platform-admin keys are
minted via ``scripts/mint_platform_admin_ssm.py`` with ``ssm_write=True``,
which writes the raw key to AWS SSM (encrypted by KMS) and never lets
the plaintext touch stdout, the shell history, or CloudWatch. See that
script's module docstring for the rationale and the Step 27a/27c-final
history of why we adopted that pattern.

This script deliberately violates that doctrine -- it prints the raw
key to stdout -- because the CI environment has no AWS, no SSM, no
persistent logs that survive the run, and no operator who could
retrieve the key from anywhere else. The runner is ephemeral; the DB
is ephemeral; the key is ephemeral. The only way the rest of the E2E
harness can authenticate against the running uvicorn is via stdout
capture.

Three guardrails keep this script's blast radius bounded:

  1. Refuses to run unless ``LUCIEL_CI_ALLOW_RAW_KEY_STDOUT=yes`` is
     set in the environment. The workflow file sets this explicitly
     in the bootstrap step's ``env:`` block. Running this script in
     any other context (a developer's laptop, a production container,
     an ECS task) requires explicitly opting in -- which is loud.

  2. Refuses to run if it detects production-shaped settings. If
     ``DATABASE_URL`` contains the substring ``"rds.amazonaws.com"`` or
     ``"production"``, the script aborts before touching the DB.

  3. The minted key has the same permissions as a real platform-admin
     key (``["chat", "sessions", "admin", "platform_admin"]``) because
     the E2E harness needs to exercise the real admin endpoints.
     Future hardening: introduce a CI-only permission token that gates
     write access to a CI-only allowlist of tenant_ids. Not in scope
     for Step 30d.

Usage
=====

In CI (the only intended caller):

    LUCIEL_CI_ALLOW_RAW_KEY_STDOUT=yes \\
        python -m scripts.bootstrap_platform_admin_ci \\
        > /tmp/admin_key.txt
    ADMIN_KEY=$(cat /tmp/admin_key.txt)

Locally (only if you really mean it -- you almost certainly want to
mint via the SSM script instead):

    DATABASE_URL=postgresql://luciel:luciel@localhost:5432/luciel_e2e \\
    LUCIEL_CI_ALLOW_RAW_KEY_STDOUT=yes \\
        python -m scripts.bootstrap_platform_admin_ci

Exit codes
==========

    0  -- success; raw key was printed to stdout (and nothing else).
    1  -- the LUCIEL_CI_ALLOW_RAW_KEY_STDOUT guardrail refused.
    2  -- the production-shape guardrail tripped.
    3  -- the underlying ApiKeyService.create_key raised.
"""

from __future__ import annotations

import os
import sys


_GUARD_ENV_VAR = "LUCIEL_CI_ALLOW_RAW_KEY_STDOUT"
_GUARD_ENV_VALUE = "yes"

# Substrings in DATABASE_URL that almost certainly indicate a non-CI
# environment. Conservative: better to false-positive and force the
# operator to use a less-loaded tool than to accidentally print a
# production key to stdout.
_PROD_DB_MARKERS = (
    "rds.amazonaws.com",
    "production",
    ".prod.",
    "prod-",
)


def _guard_env() -> None:
    val = os.environ.get(_GUARD_ENV_VAR, "")
    if val != _GUARD_ENV_VALUE:
        print(
            f"FATAL: {_GUARD_ENV_VAR} must be set to '{_GUARD_ENV_VALUE}' to "
            f"run this script. This is a CI-only tool that prints a raw API "
            f"key to stdout. For production, use "
            f"scripts/mint_platform_admin_ssm.py instead.",
            file=sys.stderr,
        )
        sys.exit(1)


def _guard_database_url() -> None:
    db_url = os.environ.get("DATABASE_URL", "")
    lowered = db_url.lower()
    for marker in _PROD_DB_MARKERS:
        if marker in lowered:
            print(
                f"FATAL: DATABASE_URL contains the production-shape marker "
                f"{marker!r}. Refusing to print a raw key to stdout against "
                f"what looks like a production database. If this is a false "
                f"positive, change the URL or use the SSM mint script.",
                file=sys.stderr,
            )
            sys.exit(2)


def main() -> int:
    _guard_env()
    _guard_database_url()

    # Imports are deferred until AFTER the guardrails so that running
    # `python scripts/bootstrap_platform_admin_ci.py` with the wrong
    # env produces the guardrail error rather than a tangentially
    # related ImportError from the app surface.
    from app.db.session import SessionLocal
    from app.services.api_key_service import ApiKeyService

    db = SessionLocal()
    try:
        svc = ApiKeyService(db)
        api_key, raw_key = svc.create_key(
            tenant_id=None,
            domain_id=None,
            agent_id=None,
            luciel_instance_id=None,
            display_name="CI E2E platform-admin (Step 30d-C)",
            permissions=["chat", "sessions", "admin", "platform_admin"],
            rate_limit=10000,
            created_by="ci-bootstrap@step-30d-c",
            auto_commit=True,
            ssm_write=False,
        )
    except Exception as exc:  # noqa: BLE001 -- top-level CI guard
        print(
            f"FATAL: bootstrap mint failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        db.close()
        return 3

    if raw_key is None:
        print(
            "FATAL: ApiKeyService.create_key returned raw_key=None even "
            "though ssm_write=False. This is a service-layer bug.",
            file=sys.stderr,
        )
        db.close()
        return 3

    # Print ONLY the raw key on stdout. Anything else (metadata,
    # banners, status lines) goes to stderr so the workflow's
    # `> /tmp/admin_key.txt` capture stays clean.
    print(raw_key)
    print(
        f"bootstrap_platform_admin_ci: minted key id={api_key.id} "
        f"prefix={api_key.key_prefix}",
        file=sys.stderr,
    )
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
