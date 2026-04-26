"""
Step 27a — Bootstrap a platform-admin API key via SSM-direct mint.

Usage (production durable key, Step 27c-final and onward):
    python -m scripts.mint_platform_admin_ssm \
        --display-name "Prod Platform Admin (Step 27c-final, 2026-04-26)" \
        --created-by "aryan@step27c-final" \
        --ssm-path "/luciel/production/platform-admin-key" \
        --region ca-central-1

Usage (legacy ephemeral bootstrap, Step 27a pattern, retained):
    python -m scripts.mint_platform_admin_ssm \
        --display-name "Dev Bootstrap Admin (2026-04-22)" \
        --created-by "aryan@step27a-bootstrap" \
        --region ca-central-1

Usage:
    python -m scripts.mint_platform_admin_ssm \\
        --display-name "Prod Platform Admin (2026-04-22)" \\
        --created-by "aryan@step27a-bootstrap" \\
        --region ca-central-1

This script:
  1. Calls ApiKeyService.create_key(ssm_write=True, tenant_id=None, ...)
  2. The raw key is written to AWS SSM Parameter Store as SecureString
     at /luciel/bootstrap/admin_key_<id>
  3. Prints ONLY the SSM path and key metadata to stdout — the raw key
     itself never touches stdout, logs, or the shell history.

Retrieval by the operator (separate, deliberate step):
    aws ssm get-parameter \\
        --name /luciel/bootstrap/admin_key_<id> \\
        --with-decryption \\
        --region ca-central-1 \\
        --query "Parameter.Value" \\
        --output text

After saving to your password manager, DELETE the SSM parameter:
    aws ssm delete-parameter \\
        --name /luciel/bootstrap/admin_key_<id> \\
        --region ca-central-1

Why this pattern:
  - Pre-27a bootstrap printed the raw key to stdout. In prod, that stdout
    was captured by CloudWatch (ECS task logs), creating a permanent
    searchable record of the bootstrap key's plaintext value. 26b Phase
    7.5 confirmed this exposure surface.
  - Post-27a, the raw key's only plaintext home is SSM (encrypted by AWS
    KMS), and the operator is responsible for deleting the parameter
    after reading it. CloudWatch sees only the SSM path.

This script is NOT committed to git. It is a runbook artifact. Run it
once per bootstrap-admin-key mint, then delete the local file or ignore
it via .gitignore.

Step 27c-final: this script is committed to git as part of platform
operational tooling. It mints both ephemeral bootstrap keys (via the
default SSM_BOOTSTRAP_PATH) and durable production platform-admin
keys (via --ssm-path /luciel/production/platform-admin-key). Earlier
versions of this script were gitignored as ephemeral runbook
artifacts; that model was retired in 27c-final when the verification
suite began reading the prod key from a stable SSM path on every
gate run.
"""

from __future__ import annotations

import argparse
import sys

from app.db.session import SessionLocal
from app.services.api_key_service import ApiKeyService, SSM_BOOTSTRAP_PATH


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Bootstrap a platform-admin API key via SSM-direct mint.",
    )
    p.add_argument(
        "--display-name",
        required=True,
        help='Human-readable label, e.g. "Prod Platform Admin (2026-04-22)".',
    )
    p.add_argument(
        "--created-by",
        required=True,
        help='Actor label, e.g. "aryan@step27a-bootstrap".',
    )
    p.add_argument(
        "--region",
        default="ca-central-1",
        help="AWS region for SSM parameter (default: ca-central-1).",
    )
    p.add_argument(
        "--rate-limit",
        type=int,
        default=10000,
        help="Per-minute rate limit on the key (default: 10000).",
    )
    p.add_argument(
        "--ssm-path",
        default=None,
        help=(
            "SSM parameter path for the raw key (default: "
            "/luciel/bootstrap/admin_key_<id>). Set to a stable path "
            "like /luciel/production/platform-admin-key for durable "
            "production keys read repeatedly by the verification suite."
        ),
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    db = SessionLocal()
    svc = ApiKeyService(db)

    try:
        api_key, raw_key = svc.create_key(
            tenant_id=None,
            domain_id=None,
            agent_id=None,
            luciel_instance_id=None,
            display_name=args.display_name,
            permissions=["chat", "sessions", "admin", "platform_admin"],
            rate_limit=args.rate_limit,
            created_by=args.created_by,
            auto_commit=True,
            ssm_write=True,
            ssm_region=args.region,
            ssm_path=args.ssm_path,                 # Step 27c-final
        )
    except Exception as exc:
        print(f"FATAL: mint failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        db.close()
        return 1

    assert raw_key is None, (
        "ssm_write=True must return raw_key=None — "
        "if this assertion fires, the service leaked the raw key."
    )

    ssm_path = args.ssm_path or SSM_BOOTSTRAP_PATH.format(key_id=api_key.id)

    print("=" * 72)
    print("PLATFORM-ADMIN KEY MINTED")
    print("=" * 72)
    print(f"  key_id      : {api_key.id}")
    print(f"  key_prefix  : {api_key.key_prefix}")
    print(f"  display     : {api_key.display_name}")
    print(f"  permissions : {api_key.permissions}")
    print(f"  tenant_id   : {api_key.tenant_id}  (NULL = platform-admin scope)")
    print(f"  ssm_path    : {ssm_path}")
    print(f"  ssm_region  : {args.region}")
    print("=" * 72)
    print()
    print("Retrieve the raw key (one time, then delete the SSM parameter):")
    print()
    print(f'  aws ssm get-parameter \\')
    print(f'    --name {ssm_path} \\')
    print(f'    --with-decryption \\')
    print(f'    --region {args.region} \\')
    print(f'    --query "Parameter.Value" \\')
    print(f'    --output text')
    print()
    print("Then delete the parameter:")
    print()
    print(f'  aws ssm delete-parameter \\')
    print(f'    --name {ssm_path} \\')
    print(f'    --region {args.region}')
    print()
    print("Save the raw key to your password manager before deletion.")
    print("The key cannot be recovered once the SSM parameter is deleted.")

    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())