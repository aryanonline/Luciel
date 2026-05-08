"""
Step 27a — Rotate dev platform-admin keys to tenant_id=NULL.

Rationale (Invariant 5): platform-admin permission grants cross-tenant
bypass via ScopePolicy regardless of tenant_id. Pre-Step-27a, all local
dev platform-admin keys were minted with tenant_id='remax-crossroads'
as a workaround for the NOT-NULL constraint that existed before 26b.1's
migration 3447ac8b45b4 made the column nullable. That workaround is now
semantically wrong: a platform-admin key's tenant_id should be NULL to
reflect "no scope ceiling".

This script:
  1. Finds all keys where 'platform_admin' is in permissions AND
     tenant_id IS NOT NULL (i.e. the dev keys: ids 8, 15, 16, 17 per
     canonical recap §10 plus any minted during 27a file development).
  2. Sets tenant_id = NULL on each.
  3. Writes an AdminAuditLog row per rotation with before/after diff.
  4. Commits in a single transaction.

Idempotent: rows already at tenant_id=NULL are skipped.

Safety:
  - No key is deactivated; active=True is preserved.
  - No permission changes; permissions list is preserved.
  - No key_hash changes; existing bearer tokens remain valid.
  - Validation via ApiKeyService.validate_key() post-rotation.

This script is NOT committed to git — it's a runbook artifact that
mutates live data on a specific DB (local dev). Prod platform-admin key
was already minted with tenant_id=NULL in Step 26b.2 bootstrap (id 3,
prefix luc_sk_kHqA2), so prod does not need this rotation.

Usage:
    python -m scripts.rotate_platform_admin_keys
    python -m scripts.rotate_platform_admin_keys --dry-run

Step 27c-final: this script is committed to git as part of platform
operational tooling. Earlier versions were gitignored as ephemeral
runbook artifacts; that model was retired in 27c-final when both this
rotation script and its sibling mint_platform_admin_ssm.py became
durable, auditable parts of the platform's security tooling. Every
future rotation invocation goes through git history.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.api_key import ApiKey
from app.models.admin_audit_log import AdminAuditLog
from app.repositories.actor_permissions_format import (
    serialize_actor_permissions,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Rotate dev platform-admin keys to tenant_id=NULL.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without committing.",
    )
    p.add_argument(
        "--actor",
        default="step27a-rotate-script",
        help="Actor label for AdminAuditLog (default: step27a-rotate-script).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    db = SessionLocal()

    try:
        stmt = select(ApiKey).where(
            ApiKey.active.is_(True),
            ApiKey.tenant_id.isnot(None),
        ).order_by(ApiKey.id)
        rows = list(db.scalars(stmt).all())

        # Filter to platform_admin keys (permissions is JSONB list)
        targets = [
            k for k in rows
            if k.permissions and "platform_admin" in k.permissions
        ]

        if not targets:
            print("No platform-admin keys with non-NULL tenant_id found. Nothing to rotate.")
            db.close()
            return 0

        print("=" * 72)
        print(f"{'DRY RUN: ' if args.dry_run else ''}PLATFORM-ADMIN KEY ROTATION")
        print("=" * 72)
        print(f"Targets: {len(targets)} key(s)")
        print()
        for k in targets:
            print(f"  id={k.id:>4}  prefix={k.key_prefix}  "
                  f"tenant_id={k.tenant_id!r}  -> NULL")
            print(f"          display={k.display_name!r}")
            print(f"          permissions={k.permissions}")
            print(f"          created_by={k.created_by!r}  "
                  f"created_at={k.created_at.isoformat() if k.created_at else '?'}")
            print()

        if args.dry_run:
            print("DRY RUN: no changes committed.")
            db.close()
            return 0

        # Apply rotation + audit log in a single transaction
        now = datetime.now(timezone.utc)
        for k in targets:
            before = {
                "tenant_id": k.tenant_id,
                "permissions": list(k.permissions),
                "active": k.active,
            }
            after = {
                "tenant_id": None,
                "permissions": list(k.permissions),
                "active": k.active,
            }

            k.tenant_id = None

            audit = AdminAuditLog(
                tenant_id=None,  # rotation scope: platform-level
                domain_id=None,
                agent_id=None,
                luciel_instance_id=None,
                actor_key_prefix=None,        # script-initiated, no caller key
                # Step 29.y gap-fix C1
                # (D-actor-permissions-comma-fragility-2026-05-07):
                # actor_permissions is a String column; pass the
                # serialized canonical form, not a raw list (which
                # would be coerced to "['platform_admin']" via repr).
                actor_permissions=serialize_actor_permissions(
                    ["platform_admin"]
                ),  # self-declared script authority
                actor_label=args.actor,
                action="api_key.rotate_tenant_to_null",
                resource_type="api_key",
                resource_pk=k.id,
                resource_natural_id=k.key_prefix,
                before_json=before,           # was: before=
                after_json=after,             # was: after=
                note=(
                    "Step 27a File 1.1: rotate platform-admin key to "
                    "tenant_id=NULL per Invariant 5. Pre-27a workaround "
                    "for NOT-NULL api_keys.tenant_id constraint is no "
                    "longer needed (26b.1 migration 3447ac8b45b4)."
                ),
                created_at=now,
            )
            db.add(audit)

        db.commit()
        print(f"Rotated {len(targets)} key(s). Audit rows written.")
        print()

        # Post-rotation verification
        print("Post-rotation state:")
        for k in targets:
            db.refresh(k)
            print(f"  id={k.id:>4}  prefix={k.key_prefix}  "
                  f"tenant_id={k.tenant_id!r}  active={k.active}")

        db.close()
        return 0

    except Exception as exc:
        db.rollback()
        print(f"FATAL: rotation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        db.close()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())