"""
Step 30d Deliverable A -- One-time audit: widget embed keys without
a scope prompt.

Why this script exists
======================

The issuance-time preflight (ScopePromptPreflight, wired into
POST /admin/embed-keys and scripts/mint_embed_key.py in Step 30d-A
commits b/c) prevents *new* domain-scoped embed keys from being issued
against a domain_configs row whose `system_prompt_additions` is NULL,
empty, or whitespace-only.

It does NOT retroactively close keys that were minted *before* the
preflight existed. Per the operator decision recorded in Step 30d
planning, we deliberately do NOT block existing widget chat turns at
runtime -- that would brick the staging widget and any customer
mid-onboarding. Instead, this script surfaces the unscoped keys so a
human operator can decide whether to deactivate-and-remint
(Pattern E), set the missing scope prompt, or accept the row.

What this script does
=====================

  1. SELECTs all active embed keys (key_kind='embed', active=True).
  2. For each, looks up the matching domain_configs row by
     (tenant_id, domain_id).
  3. Flags any key where:
        * domain_id IS NOT NULL  AND
        * (no domain_configs row OR system_prompt_additions is NULL
           / empty / whitespace).
     Tenant-wide keys (domain_id IS NULL) are excluded from the flag
     set because they are governed by TenantConfig.system_prompt, not
     by domain_configs (same exclusion the preflight applies).

  4. Prints the flagged rows in either a human-readable table or
     machine-readable JSON.

Exit codes
==========

  0 -- audit ran successfully and zero flagged rows were found.
  1 -- audit ran successfully and at least one flagged row was found.
  2 -- audit could not run (DB error, schema mismatch, etc.).

Usage
=====

    python -m scripts.audit_widget_scope
    python -m scripts.audit_widget_scope --format json
    python -m scripts.audit_widget_scope --tenant-id acme

Reference docs
==============

  - app/services/scope_prompt_preflight.py -- the issuance-time check
    this script is the runtime-side audit complement to.
  - app/api/v1/admin.py POST /admin/embed-keys -- the wired call site.
  - scripts/mint_embed_key.py -- the CLI sibling.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from sqlalchemy.orm import aliased

from app.db.session import SessionLocal
from app.models.api_key import ApiKey
from app.models.domain_config import DomainConfig


def _is_empty_prompt(value: Optional[str]) -> bool:
    """True iff value is NULL, empty, or whitespace-only.

    Mirrors the rule in ScopePromptPreflight.check so the audit and the
    preflight cannot drift on what counts as 'unscoped'.
    """

    return value is None or not value.strip()


def _collect_flagged_rows(db, tenant_id_filter: Optional[str]) -> list[dict]:
    """Return one dict per flagged embed key. Read-only."""

    dc = aliased(DomainConfig)

    q = (
        db.query(ApiKey, dc)
        .outerjoin(
            dc,
            (dc.tenant_id == ApiKey.tenant_id)
            & (dc.domain_id == ApiKey.domain_id),
        )
        .filter(ApiKey.key_kind == "embed")
        .filter(ApiKey.active.is_(True))
    )
    if tenant_id_filter:
        q = q.filter(ApiKey.tenant_id == tenant_id_filter)

    flagged: list[dict] = []
    for key, dconf in q.all():
        # Tenant-wide keys are intentionally excluded (governed by
        # TenantConfig.system_prompt at chat time; same exclusion as
        # the issuance preflight).
        if key.domain_id is None:
            continue

        if dconf is None:
            flagged.append(
                {
                    "key_id": key.id,
                    "key_prefix": key.key_prefix,
                    "display_name": key.display_name,
                    "tenant_id": key.tenant_id,
                    "domain_id": key.domain_id,
                    "reason": "missing_domain_config",
                    "domain_config_id": None,
                    "system_prompt_additions": None,
                }
            )
            continue

        if _is_empty_prompt(dconf.system_prompt_additions):
            flagged.append(
                {
                    "key_id": key.id,
                    "key_prefix": key.key_prefix,
                    "display_name": key.display_name,
                    "tenant_id": key.tenant_id,
                    "domain_id": key.domain_id,
                    "reason": "empty_system_prompt",
                    "domain_config_id": dconf.id,
                    "system_prompt_additions": dconf.system_prompt_additions,
                }
            )

    return flagged


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("No unscoped active embed keys found.")
        return

    header = (
        f"{'key_id':<8} {'prefix':<22} {'tenant_id':<24} "
        f"{'domain_id':<24} {'reason':<22} display_name"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['key_id']:<8} "
            f"{(r['key_prefix'] or '')[:22]:<22} "
            f"{(r['tenant_id'] or '')[:24]:<24} "
            f"{(r['domain_id'] or '')[:24]:<24} "
            f"{r['reason']:<22} "
            f"{r['display_name'] or ''}"
        )
    print()
    print(f"Total flagged: {len(rows)}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit active widget embed keys whose target "
            "domain_configs row is missing or has an empty "
            "system_prompt_additions. Tenant-wide keys are excluded."
        ),
    )
    parser.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="Output format. Default: table.",
    )
    parser.add_argument(
        "--tenant-id",
        default=None,
        help="Optional. Restrict the audit to a single tenant_id.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        try:
            rows = _collect_flagged_rows(db, tenant_id_filter=args.tenant_id)
        except Exception as exc:
            print(
                f"FATAL: audit query failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 2

        if args.format == "json":
            print(json.dumps(rows, indent=2, default=str))
        else:
            _print_table(rows)

        return 1 if rows else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
