"""
Step 30b — Mint a customer-facing embed key for the chat widget.

Usage (tenant-wide embed key, default branding):
    python -m scripts.mint_embed_key \
        --tenant-id acme \
        --display-name "Acme prod widget (2026-05-09)" \
        --origins https://acme.com,https://www.acme.com \
        --rate-limit-per-minute 60 \
        --created-by "aryan@step30b-issuance"

Usage (domain-scoped embed key with custom branding):
    python -m scripts.mint_embed_key \
        --tenant-id acme \
        --domain-id support \
        --display-name "Acme support widget (2026-05-09)" \
        --origins https://support.acme.com \
        --rate-limit-per-minute 30 \
        --accent-color "#1A2B3C" \
        --widget-display-name "Acme Support" \
        --greeting-message "How can we help today?" \
        --created-by "aryan@step30b-issuance"

What this script does
=====================

  1. Parses CLI flags into an EmbedKeyCreate Pydantic instance. The
     SAME schema the HTTP endpoint uses validates every field --
     origin format (no wildcards, no paths, exact scheme+host[+port]),
     branding caps (display_name <=50, greeting_message <=240,
     accent_color 7-char hex), HTML rejection, dedupe. The schema is
     the single source of truth for what shape an embed key takes;
     the CLI cannot drift from the HTTP path because it validates
     through the same model.

  2. Opens a SessionLocal() and instantiates ApiKeyService. Calls
     ApiKeyService.create_key(...) with key_kind='embed', the four
     widget kwargs, and audit_ctx=AuditContext.system(
     label='mint_embed_key_cli'). The audit row lands in the same
     transaction as the api_keys INSERT (Invariant 4: audit-before-
     commit), so the audit chain is intact whether or not the commit
     succeeds.

  3. Prints metadata + the raw key to stdout exactly once. The raw
     key is shown ONLY here -- the database stores only its SHA-256
     hash. If the operator loses the printed value before pasting it
     into the customer's site, the row must be deactivated (Pattern
     E) and a new key minted. There is no recovery.

What this script does NOT do
============================

  - It does NOT write the raw key to AWS SSM. SSM is a Luciel-owned
    resource; customers cannot read it. Embed keys must be returned
    to the operator at issuance time so the operator can hand the
    raw value to the customer (one operator-mediated step, not a
    customer-facing self-service flow at v1). The service layer
    explicitly rejects ssm_write=True with key_kind='embed' to
    prevent a future caller from minting an unrecoverable embed key.

  - It does NOT email or otherwise transmit the key. Operator hands
    it off via whatever secure channel the customer has agreed to
    (1Password share, signed email, in-person at onboarding).

  - It does NOT support an interactive prompt mode. Embed keys are
    minted infrequently, by an operator who already has all required
    fields in front of them (customer name -> tenant_id, domain
    list, branding spec from the customer's brand book). A
    non-interactive flag-based CLI is auditable in shell history and
    composable with future automation; an interactive mode would
    add surface area without earning its complexity.

Reference docs
==============

  - app/schemas/api_key.py -- EmbedKeyCreate, WidgetConfig, the
    permission constant, and all field validators.
  - app/services/api_key_service.py -- create_key, including the
    embed/ssm_write mutual exclusion and the audit payload shape.
  - app/api/v1/admin.py POST /admin/embed-keys -- the HTTP sibling
    of this CLI; both paths funnel through the same schema and
    service entrypoint.
  - alembic/versions/a7c1f4e92b85_step30b_api_keys_widget_columns.py
    -- the migration that added the four widget columns.

Doc-discipline note
===================

This file is committed to git as platform operational tooling
(same pattern as scripts/mint_platform_admin_ssm.py post-Step 27c-
final). It is NOT a runbook artifact -- the verification suite and
operator runbooks both invoke it stably.
"""

from __future__ import annotations

import argparse
import sys

from app.db.session import SessionLocal
from app.repositories.admin_audit_repository import AuditContext
from app.schemas.api_key import EmbedKeyCreate, WidgetConfig
from app.services.api_key_service import ApiKeyService


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Mint a customer-facing embed key for the chat widget. "
            "Prints the raw key to stdout exactly once -- save it before "
            "the process exits."
        ),
    )
    p.add_argument(
        "--tenant-id",
        required=True,
        help=(
            "Tenant the embed key belongs to. Required; embed keys "
            "MUST be tenant-scoped."
        ),
    )
    p.add_argument(
        "--domain-id",
        default=None,
        help=(
            "Optional. When set, the key only resolves chat for that "
            "domain within the tenant."
        ),
    )
    p.add_argument(
        "--display-name",
        required=True,
        help=(
            'Operator-facing label for this key, e.g. "Acme prod widget '
            '(2026-05-09)". Distinct from --widget-display-name.'
        ),
    )
    p.add_argument(
        "--origins",
        required=True,
        help=(
            "Comma-separated list of allowed origins. Each entry is "
            "exactly scheme + host (+ optional port). No wildcards, "
            "paths, queries, or fragments. "
            "Example: https://acme.com,https://www.acme.com"
        ),
    )
    p.add_argument(
        "--rate-limit-per-minute",
        type=int,
        required=True,
        help=(
            "Per-minute burst cap on this embed key. Must be a positive "
            "integer <= 10000. The per-day rate_limit column does not "
            "apply to embed keys."
        ),
    )
    p.add_argument(
        "--accent-color",
        default=None,
        help=(
            'Optional. 7-character hex with leading #, e.g. "#1A2B3C". '
            "Sets the widget accent color. Defaults to the widget's "
            "built-in default."
        ),
    )
    p.add_argument(
        "--widget-display-name",
        default=None,
        help=(
            "Optional. Customer-facing widget header label, up to 50 "
            "characters. HTML rejected. Distinct from --display-name "
            "(which is the operator-facing label)."
        ),
    )
    p.add_argument(
        "--greeting-message",
        default=None,
        help=(
            "Optional. Greeting shown when the widget panel opens, up "
            "to 240 characters. HTML rejected."
        ),
    )
    p.add_argument(
        "--created-by",
        default=None,
        help=(
            'Audit-trail field. Operator label, e.g. '
            '"aryan@step30b-issuance".'
        ),
    )
    return p.parse_args()


def _parse_origins(raw: str) -> list[str]:
    """Split the comma-separated --origins flag into a list.

    Empty-string entries (from a trailing comma) are dropped here so
    the schema validator sees a clean list. The schema validator
    handles all other normalization (lowercase, dedupe, regex).
    """
    return [entry.strip() for entry in raw.split(",") if entry.strip()]


def main() -> int:
    args = parse_args()

    # --- Build the schema instance --------------------------------------
    # Validation runs HERE, before we open a DB session. If the operator
    # passed an invalid origin or an over-length greeting, we fail fast
    # with a Pydantic ValidationError and never touch the database.
    try:
        widget = WidgetConfig(
            accent_color=args.accent_color,
            display_name=args.widget_display_name,
            greeting_message=args.greeting_message,
        )
        payload = EmbedKeyCreate(
            tenant_id=args.tenant_id,
            domain_id=args.domain_id,
            display_name=args.display_name,
            allowed_origins=_parse_origins(args.origins),
            rate_limit_per_minute=args.rate_limit_per_minute,
            widget_config=widget,
            created_by=args.created_by,
        )
    except Exception as exc:
        # Pydantic ValidationError prints a useful multi-line message;
        # we render it on stderr and exit 2 (the conventional CLI
        # "usage error" code).
        print(f"FATAL: invalid embed-key arguments: {exc}", file=sys.stderr)
        return 2

    # --- Mint via ApiKeyService -----------------------------------------
    # Mirrors POST /admin/embed-keys exactly (app/api/v1/admin.py line
    # 756). The kwargs match because both paths consume the same schema
    # -- the schema is the single source of truth for what an embed
    # key looks like, and both call sites forward those validated
    # values through identically.
    db = SessionLocal()
    svc = ApiKeyService(db)

    try:
        api_key, raw_key = svc.create_key(
            tenant_id=payload.tenant_id,
            domain_id=payload.domain_id,
            agent_id=None,
            luciel_instance_id=None,
            display_name=payload.display_name,
            # Server-set: matches the constraint encoded in
            # app.schemas.api_key.EMBED_REQUIRED_PERMISSIONS and
            # enforced by app.api.widget_deps.require_embed_key.
            permissions=["chat"],
            # rate_limit (per-day) is an admin-key concept; embed
            # keys are gated by rate_limit_per_minute. Set to 0 so
            # the per-day layer is effectively a no-op for embed
            # rows, matching the endpoint.
            rate_limit=0,
            created_by=payload.created_by,
            auto_commit=True,
            ssm_write=False,
            audit_ctx=AuditContext.system(label="mint_embed_key_cli"),
            key_kind="embed",
            allowed_origins=payload.allowed_origins,
            rate_limit_per_minute=payload.rate_limit_per_minute,
            widget_config=payload.widget_config.to_jsonb(),
        )
    except Exception as exc:
        print(
            f"FATAL: mint failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        db.close()
        return 1

    # raw_key is non-None here because ssm_write=False. The service
    # layer also rejects ssm_write=True with key_kind='embed', but we
    # belt-and-suspenders here so a future regression is loud.
    assert raw_key is not None, (
        "create_key returned None raw_key for an embed key; this should "
        "be impossible because ssm_write=False is hardcoded above and "
        "the service rejects ssm_write=True for embed keys. Investigate."
    )

    # --- Print metadata + raw key (one time) ----------------------------
    # Layout deliberately mirrors mint_platform_admin_ssm.py's metadata
    # block so an operator switching between the two scripts sees the
    # same shape. The raw key is fenced between RAW KEY START / END
    # markers so an operator's terminal-copy macro (or a clipboard
    # script) can grab it unambiguously.
    print("=" * 72)
    print("EMBED KEY MINTED")
    print("=" * 72)
    print(f"  key_id                 : {api_key.id}")
    print(f"  key_prefix             : {api_key.key_prefix}")
    print(f"  display_name           : {api_key.display_name}")
    print(f"  tenant_id              : {api_key.tenant_id}")
    print(f"  domain_id              : {api_key.domain_id}")
    print(f"  permissions            : {api_key.permissions}")
    print(f"  key_kind               : {api_key.key_kind}")
    print(f"  allowed_origins        : {api_key.allowed_origins}")
    print(f"  rate_limit_per_minute  : {api_key.rate_limit_per_minute}")
    print(f"  widget_config          : {api_key.widget_config}")
    print("=" * 72)
    print()
    print("RAW KEY START")
    print(raw_key)
    print("RAW KEY END")
    print()
    print(
        "Save the raw key NOW. It is shown only here -- the database "
        "stores only its SHA-256 hash. If lost, deactivate this row "
        "(Pattern E) and mint a new key."
    )
    print()
    print(
        "Hand the raw key to the customer via your agreed secure channel "
        "(1Password share, signed email, in-person). Do not commit it to "
        "git, do not paste it into chat, do not email it in plaintext."
    )

    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
