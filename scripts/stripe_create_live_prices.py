"""
Step 30a.2 -- Create the 7 live-mode Stripe Prices for Luciel's self-serve
subscription surface, and emit the matching 7 SSM SecureString puts as a
PowerShell script the operator runs locally to land the Price IDs in
/luciel/production/stripe_price_*.

This script is half of the closure path for
D-stripe-credentials-never-wired-to-prod-backend-2026-05-14 (see
docs/DRIFTS.md §3). It does NOT touch SSM directly -- the SSM puts are
emitted as a generated script for the operator to run on their local
Windows / PowerShell environment with their luciel-admin IAM credentials,
mirroring the GATE 2 step-1 secret-key put that already landed.

Pattern E (Step 27c-final discipline mirrored from
scripts/mint_worker_db_password_ssm.py):
  - The Stripe live secret key is read from SSM SecureString at runtime
    (path: /luciel/production/stripe_secret_key, put separately at
    GATE 2 step 1). It is never passed on the CLI, never echoed to
    stdout, never logged.
  - The 7 newly-created Price IDs ARE printed to stdout -- they are
    public identifiers (visible in the Stripe Dashboard, transmitted in
    every checkout-session client_reference, and read into the backend
    container's environment via task-def `secrets` entries). The
    SecureString classification on the SSM puts is operational
    consistency with the rest of the /luciel/production/ namespace, not
    a secrecy claim about the IDs themselves.
  - Idempotent: re-running the script with --force-recreate generates
    fresh Prices and overwrites the emitted SSM-put script; without
    that flag, the script refuses to mint a fresh Price for a slot if
    a matching Price (same product, same nickname, same unit_amount,
    active) is already present on the account. Pattern E means we never
    delete; the prior Price would be archived (deactivated) by a future
    pricing-change pass, not destroyed here.

Usage (the only blessed path):

    python -m scripts.stripe_create_live_prices \\
        --region ca-central-1 \\
        --output-script scripts/stripe_ssm_price_puts.ps1

The script reads /luciel/production/stripe_secret_key from SSM via boto3,
calls the Stripe API to create the 7 Prices, writes the emitted SSM-put
script to --output-script, and prints a metadata summary table (Price
IDs + product names + amounts) to stdout.

Dry-run mode (--dry-run) verifies SSM read access + Stripe SDK auth (via
Account.retrieve()) but does NOT create Prices and does NOT write the
output script. Useful for argparse verification and runbook walkthroughs.

The 7 Prices created (CANONICAL_RECAP §14, locked grid):

    1. stripe_price_individual          : $30 CAD   /month  (recurring)
    2. stripe_price_individual_annual   : $300 CAD  /year   (recurring)
    3. stripe_price_team_monthly        : $300 CAD  /month  (recurring)
    4. stripe_price_team_annual         : $3,000 CAD /year   (recurring)
    5. stripe_price_company_monthly     : $2,000 CAD /month  (recurring)
    6. stripe_price_company_annual      : $20,000 CAD /year   (recurring)
    7. stripe_price_intro_fee           : $100 CAD  one-time

Each Price's parent Product is created on the same run (Stripe REST
requires a Product to be created or referenced for every Price). Product
names are human-readable ("Luciel Individual", "Luciel Team", etc.) so
the Stripe Dashboard reads cleanly without operator lookup tables.

What this script does NOT do:
  - Configure the Stripe Customer Portal (separate manual step in the
    Stripe Dashboard; the portal references the Prices created here).
  - Register the Stripe webhook endpoint (a later GATE step, after
    luciel-backend:46 lands and /api/v1/billing/webhook is reachable).
  - Put the Price IDs into SSM (the operator runs the emitted PowerShell
    script for that; this preserves the locked GATE-2 shape where the
    operator's hands touch every prod-mutating command).
  - Touch the publishable key (client-side only, hardcoded in the
    marketing-site bundle, never in backend SSM).

Cross-references:
  - Drift opening this work: D-stripe-credentials-never-wired-to-prod-
    backend-2026-05-14 (docs/DRIFTS.md §3)
  - Parent drift closed together: D-stripe-live-account-not-yet-
    activated-2026-05-13 (docs/DRIFTS.md §3, annotation 2026-05-14b)
  - Locked pricing grid: docs/CANONICAL_RECAP.md §14
  - Config slots the IDs feed: app/core/config.py lines 176-200
  - Operator runbook (this session's running plan): the GATE-2 step in
    docs/runbooks/step-30a-1-prod-deploy.md will be updated post-GATE-5
    to reflect the /luciel/production/ path convention (currently
    prescribes /luciel/prod/, which is superseded -- see drift body).
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# Imports deferred to main() so --help works without boto3/stripe installed.


# =====================================================================
# CANONICAL §14 pricing grid -- locked. Do not edit without an explicit
# CANONICAL_RECAP §14 update + drift entry. Amounts are CAD in cents
# (Stripe API convention -- unit_amount is the smallest currency unit).
# =====================================================================
CURRENCY = "cad"
DEFAULT_REGION = "ca-central-1"
DEFAULT_SECRET_KEY_SSM_PATH = "/luciel/production/stripe_secret_key"
DEFAULT_OUTPUT_SCRIPT = "scripts/stripe_ssm_price_puts.ps1"
SSM_PRICE_PATH_PREFIX = "/luciel/production/"


@dataclass(frozen=True)
class PriceSpec:
    """One row of the §14 grid plus its SSM/env-var/Stripe metadata."""

    # Slot identity -- matches app/core/config.py setting names exactly.
    config_setting: str         # e.g. "stripe_price_individual"
    ssm_param_name: str         # e.g. "/luciel/production/stripe_price_individual"
    # Stripe Product side.
    product_name: str           # human-readable, shown in Stripe Dashboard
    product_metadata_key: str   # short slug for product metadata.tier
    # Stripe Price side.
    unit_amount_cents: int      # CAD cents
    cadence: str                # "month" | "year" | "one_time"
    nickname: str               # human-readable, shown alongside the Price ID
    # Documentation hook.
    canonical_label: str        # e.g. "Individual monthly ($30 CAD)"


PRICE_SPECS: tuple[PriceSpec, ...] = (
    PriceSpec(
        config_setting="stripe_price_individual",
        ssm_param_name=SSM_PRICE_PATH_PREFIX + "stripe_price_individual",
        product_name="Luciel Individual",
        product_metadata_key="individual",
        unit_amount_cents=3000,          # $30.00 CAD
        cadence="month",
        nickname="Individual monthly",
        canonical_label="Individual monthly ($30 CAD)",
    ),
    PriceSpec(
        config_setting="stripe_price_individual_annual",
        ssm_param_name=SSM_PRICE_PATH_PREFIX + "stripe_price_individual_annual",
        product_name="Luciel Individual",
        product_metadata_key="individual",
        unit_amount_cents=30000,         # $300.00 CAD
        cadence="year",
        nickname="Individual annual",
        canonical_label="Individual annual ($300 CAD)",
    ),
    PriceSpec(
        config_setting="stripe_price_team_monthly",
        ssm_param_name=SSM_PRICE_PATH_PREFIX + "stripe_price_team_monthly",
        product_name="Luciel Team",
        product_metadata_key="team",
        unit_amount_cents=30000,         # $300.00 CAD
        cadence="month",
        nickname="Team monthly",
        canonical_label="Team monthly ($300 CAD)",
    ),
    PriceSpec(
        config_setting="stripe_price_team_annual",
        ssm_param_name=SSM_PRICE_PATH_PREFIX + "stripe_price_team_annual",
        product_name="Luciel Team",
        product_metadata_key="team",
        unit_amount_cents=300000,        # $3,000.00 CAD
        cadence="year",
        nickname="Team annual",
        canonical_label="Team annual ($3,000 CAD)",
    ),
    PriceSpec(
        config_setting="stripe_price_company_monthly",
        ssm_param_name=SSM_PRICE_PATH_PREFIX + "stripe_price_company_monthly",
        product_name="Luciel Company",
        product_metadata_key="company",
        unit_amount_cents=200000,        # $2,000.00 CAD
        cadence="month",
        nickname="Company monthly",
        canonical_label="Company monthly ($2,000 CAD)",
    ),
    PriceSpec(
        config_setting="stripe_price_company_annual",
        ssm_param_name=SSM_PRICE_PATH_PREFIX + "stripe_price_company_annual",
        product_name="Luciel Company",
        product_metadata_key="company",
        unit_amount_cents=2000000,       # $20,000.00 CAD
        cadence="year",
        nickname="Company annual",
        canonical_label="Company annual ($20,000 CAD)",
    ),
    PriceSpec(
        config_setting="stripe_price_intro_fee",
        ssm_param_name=SSM_PRICE_PATH_PREFIX + "stripe_price_intro_fee",
        product_name="Luciel Intro Fee",
        product_metadata_key="intro_fee",
        unit_amount_cents=10000,         # $100.00 CAD
        cadence="one_time",
        nickname="Intro fee (one-time, 90-day trial)",
        canonical_label="Intro fee ($100 CAD one-time)",
    ),
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Create the 7 live-mode Stripe Prices for Luciel's self-serve "
            "billing surface and emit the matching 7 SSM SecureString puts "
            "as a PowerShell script."
        ),
    )
    p.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help=f"AWS region for the SSM read (default: {DEFAULT_REGION}).",
    )
    p.add_argument(
        "--secret-key-ssm-path",
        default=DEFAULT_SECRET_KEY_SSM_PATH,
        help=(
            f"SSM path to read the Stripe live secret key from "
            f"(default: {DEFAULT_SECRET_KEY_SSM_PATH}). The value must be "
            f"a SecureString starting with 'sk_live_'. Test-mode keys "
            f"('sk_test_') are rejected -- this script is for live-mode "
            f"Prices only."
        ),
    )
    p.add_argument(
        "--output-script",
        default=DEFAULT_OUTPUT_SCRIPT,
        help=(
            f"Path to write the emitted PowerShell SSM-put script "
            f"(default: {DEFAULT_OUTPUT_SCRIPT}). The path is relative "
            f"to the repo root; the file is overwritten if it exists. "
            f"The emitted script's commands are NOT executed here -- "
            f"the operator runs the script locally with their "
            f"luciel-admin IAM credentials."
        ),
    )
    p.add_argument(
        "--force-recreate",
        action="store_true",
        help=(
            "Mint fresh Prices even if Prices matching the (product_name, "
            "unit_amount, cadence) signature already exist on the account. "
            "Without this flag, the script refuses to proceed when any "
            "slot has a pre-existing match -- this is the idempotency "
            "guard against double-minting on a re-run. The pre-existing "
            "Price would need to be archived (deactivated) via a "
            "separate Pattern E pass; this script never deletes."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Verify SSM read access + Stripe SDK auth via Account.retrieve(), "
            "print the planned Prices table, and exit. Does NOT create Prices "
            "and does NOT write --output-script. Useful for argparse "
            "verification, runbook walkthroughs, and IAM/network preflight."
        ),
    )
    return p.parse_args()


def read_secret_key_from_ssm(*, region: str, ssm_path: str) -> str:
    """Read the Stripe live secret key from SSM SecureString.

    The plaintext returned here is held only in this process's memory
    for the duration of the Stripe API calls. It is never written to
    stdout, stderr, the output script, or any log surface.

    Validates that the key starts with 'sk_live_' -- a test-mode key
    ('sk_test_') against this script would silently create test-mode
    Prices that the prod backend would then try to use against the live
    Stripe account, causing every checkout to fail with a misleading
    'No such price' error. Hard-fail at read time instead.
    """
    import boto3  # local import keeps --help fast
    from botocore.exceptions import ClientError

    ssm = boto3.client("ssm", region_name=region)
    try:
        resp = ssm.get_parameter(Name=ssm_path, WithDecryption=True)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ParameterNotFound":
            raise RuntimeError(
                f"SSM parameter {ssm_path!r} not found. The Stripe "
                f"secret key must be put to SSM (SecureString, region "
                f"{region}) before this script can run. See GATE 2 "
                f"step 1 of the Step 30a.2 closure sequence."
            ) from exc
        if code in ("AccessDeniedException", "AccessDenied"):
            raise RuntimeError(
                f"SSM read access denied for {ssm_path!r}. Update the "
                f"operator's IAM policy to allow ssm:GetParameter on "
                f"this path, then re-run."
            ) from exc
        raise RuntimeError(
            f"SSM read failed with unexpected error code {code!r}."
        ) from exc

    value = resp["Parameter"]["Value"]
    if not value.startswith("sk_live_"):
        # Don't echo the actual value -- redact to prefix-only.
        prefix = value[:8] if len(value) >= 8 else "(empty)"
        raise RuntimeError(
            f"SSM parameter {ssm_path!r} does not contain a live-mode "
            f"Stripe secret key (must start with 'sk_live_'; got "
            f"{prefix!r}...). This script only mints live-mode Prices. "
            f"To use test mode, write a one-off script -- do not "
            f"weaken this guard."
        )
    return value


def verify_stripe_auth(stripe_module) -> dict:
    """Round-trip the Stripe SDK against the live account.

    Account.retrieve() is the cheapest authenticated GET on the API.
    Returns the account object so the caller can confirm the account
    ID and verified-status before any Price mutation.

    Raises stripe.error.AuthenticationError if the secret key is
    invalid; that error is allowed to propagate to main() which wraps
    it in a clean operator-targeted message.
    """
    account = stripe_module.Account.retrieve()
    return account


def get_or_create_product(
    stripe_module,
    *,
    name: str,
    metadata_tier: str,
    force_recreate: bool,
) -> str:
    """Idempotent Product lookup keyed by name + metadata.tier.

    Searches for an existing live-mode Product whose `name` equals the
    requested name AND whose `metadata['luciel_tier']` equals the slug.
    If found, returns its id. Otherwise creates a new Product. The
    metadata key namespaces the lookup to this script's mints and
    prevents collisions with any manually-created Stripe Products that
    happen to share a name.

    --force-recreate causes a fresh Product to be created even if one
    exists. The pre-existing Product is left untouched (Pattern E:
    never delete, only deactivate via a separate pass). This is
    primarily useful when the operator wants a clean slate after a
    test-mode/live-mode mix-up.
    """
    if not force_recreate:
        # List up to 100 active live-mode products (we have at most 4
        # under this script: Individual, Team, Company, Intro Fee).
        products = stripe_module.Product.list(active=True, limit=100)
        for product in products.auto_paging_iter():
            if (
                product.name == name
                and product.metadata.get("luciel_tier") == metadata_tier
            ):
                return product.id

    product = stripe_module.Product.create(
        name=name,
        active=True,
        metadata={
            "luciel_tier": metadata_tier,
            "luciel_mint_script": "stripe_create_live_prices.py",
            "luciel_minted_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return product.id


def find_existing_price_for_spec(
    stripe_module,
    *,
    product_id: str,
    spec: PriceSpec,
) -> Optional[str]:
    """Look up a pre-existing Price matching the spec's signature.

    Match criteria: same product_id, same unit_amount, same currency,
    same recurring.interval (or one-time shape), and the Price is
    active. If a match is found, returns its id; otherwise None.

    This is the idempotency guard called when --force-recreate is NOT
    set. It lets the script be re-run safely after a partial failure
    without double-minting.
    """
    prices = stripe_module.Price.list(
        product=product_id, active=True, limit=100, currency=CURRENCY
    )
    for price in prices.auto_paging_iter():
        if price.unit_amount != spec.unit_amount_cents:
            continue
        if spec.cadence == "one_time":
            if price.recurring is None and price.type == "one_time":
                return price.id
        else:
            if price.recurring and price.recurring.get("interval") == spec.cadence:
                return price.id
    return None


def create_price_for_spec(
    stripe_module,
    *,
    product_id: str,
    spec: PriceSpec,
) -> str:
    """Create one Stripe Price for the given spec. Returns the Price id.

    Recurring vs one-time is determined by spec.cadence:
      - "month" / "year"  -> recurring with that interval
      - "one_time"        -> Price without a `recurring` block; the
                             Price's `type` ends up as "one_time"
    """
    create_kwargs: dict = {
        "product": product_id,
        "currency": CURRENCY,
        "unit_amount": spec.unit_amount_cents,
        "nickname": spec.nickname,
        "active": True,
        "metadata": {
            "luciel_config_setting": spec.config_setting,
            "luciel_ssm_param_name": spec.ssm_param_name,
            "luciel_mint_script": "stripe_create_live_prices.py",
            "luciel_minted_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    if spec.cadence == "one_time":
        # No `recurring` key -> Stripe creates a one-time Price.
        pass
    else:
        create_kwargs["recurring"] = {"interval": spec.cadence}

    price = stripe_module.Price.create(**create_kwargs)
    return price.id


def emit_ssm_put_script(
    *,
    output_path: str,
    region: str,
    price_ids: dict[str, str],
    minted_at: str,
) -> None:
    """Write the PowerShell SSM-put script the operator runs locally.

    One `aws ssm put-parameter` per spec, all under
    /luciel/production/stripe_price_*, all SecureString, all
    --overwrite (because we want re-runs to be idempotent at the SSM
    layer too -- the operator should be able to re-run this generated
    script without manual cleanup).

    The script is plain PowerShell with backtick line-continuations
    matching the GATE 2 step 1 secret-key put shape Aryan already ran.
    No bash variants; the operator's environment is Windows-only.
    """
    lines = []
    lines.append(f"# Luciel -- 7 Stripe Price ID SSM puts (Step 30a.2 GATE 2 step 2)")
    lines.append(f"# Generated by scripts/stripe_create_live_prices.py at {minted_at}")
    lines.append(f"# Region: {region}")
    lines.append(f"#")
    lines.append(f"# Run this on your local PowerShell with luciel-admin IAM creds.")
    lines.append(f"# Each command is idempotent (--overwrite); re-run safe.")
    lines.append(f"#")
    lines.append(
        f"# Cross-ref: docs/DRIFTS.md \u00a73 "
        f"D-stripe-credentials-never-wired-to-prod-backend-2026-05-14"
    )
    lines.append(f"")
    for spec in PRICE_SPECS:
        price_id = price_ids[spec.config_setting]
        lines.append(f"# {spec.canonical_label}")
        lines.append(f"aws ssm put-parameter `")
        lines.append(f'  --name "{spec.ssm_param_name}" `')
        lines.append(f'  --value "{price_id}" `')
        lines.append(f"  --type SecureString `")
        lines.append(f'  --region {region} `')
        lines.append(f"  --overwrite")
        lines.append(f"")
    lines.append(f"# Verify all 7 puts landed:")
    lines.append(f"aws ssm describe-parameters `")
    lines.append(
        f'  --parameter-filters '
        f'"Key=Name,Option=BeginsWith,Values={SSM_PRICE_PATH_PREFIX}stripe_price_" `'
    )
    lines.append(f"  --region {region}")
    lines.append(f"")

    with open(output_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))


def print_summary_table(
    *,
    account_id: str,
    price_ids: dict[str, str],
    output_script: str,
    minted_at: str,
) -> None:
    """Pretty-print the seven Price IDs + their canonical labels."""
    print("=" * 72)
    print("STRIPE LIVE PRICES CREATED")
    print("=" * 72)
    print(f"  stripe_account  : {account_id}")
    print(f"  minted_at       : {minted_at}")
    print(f"  output_script   : {output_script}")
    print("-" * 72)
    print(f"  {'config_setting':<32} | {'price_id':<32} | label")
    print("-" * 72)
    for spec in PRICE_SPECS:
        price_id = price_ids[spec.config_setting]
        print(
            f"  {spec.config_setting:<32} | {price_id:<32} | "
            f"{spec.canonical_label}"
        )
    print("=" * 72)
    print()
    print("Next step (GATE 2 step 2):")
    print(f"  Run the emitted PowerShell script locally:")
    print(f"    .\\{output_script.replace('/', chr(92))}")
    print()
    print("After all 7 puts land, the backend is ready for the :46 task-def")
    print("patch (GATE step 9) that wires 9 STRIPE_* secrets entries from")
    print(f"the {SSM_PRICE_PATH_PREFIX}stripe_* SSM namespace into the")
    print("container environment.")


def main() -> int:
    args = parse_args()

    # ----- Layer 1: SSM read for the live secret key -----
    try:
        secret_key = read_secret_key_from_ssm(
            region=args.region, ssm_path=args.secret_key_ssm_path
        )
    except RuntimeError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1

    # ----- Layer 2: Stripe SDK auth check -----
    try:
        import stripe  # local import keeps --help fast
    except ImportError:
        print(
            "FATAL: stripe SDK is not installed. Activate the project venv.",
            file=sys.stderr,
        )
        return 1

    # Configure the SDK in-process only; never write to a config file.
    stripe.api_key = secret_key
    # Pin to a known-good API version so behaviour is stable across
    # operator environments. Pulled from stripe SDK >=10.0.0 baseline.
    stripe.api_version = "2024-06-20"

    try:
        account = verify_stripe_auth(stripe)
    except stripe.error.AuthenticationError as exc:
        print(
            f"FATAL: Stripe authentication failed. The secret key at "
            f"{args.secret_key_ssm_path!r} is not valid for the live "
            f"account. Stripe error: {exc.user_message or exc}",
            file=sys.stderr,
        )
        return 1
    except stripe.error.StripeError as exc:
        print(
            f"FATAL: Stripe API error during auth check: "
            f"{type(exc).__name__}: {exc.user_message or exc}",
            file=sys.stderr,
        )
        return 1

    minted_at = datetime.now(timezone.utc).isoformat()
    account_id = account.id

    # ----- Dry-run branch -----
    if args.dry_run:
        print("=" * 72)
        print("DRY RUN -- no Stripe Prices created, no output script written")
        print("  (SSM read + Stripe auth PASSED)")
        print("=" * 72)
        print(f"  stripe_account     : {account_id}")
        print(f"  region             : {args.region}")
        print(f"  secret_key_ssm     : {args.secret_key_ssm_path}")
        print(f"  output_script      : {args.output_script} (would be written)")
        print(f"  force_recreate     : {args.force_recreate}")
        print(f"  planned_at         : {minted_at}")
        print("-" * 72)
        print("Planned Prices (CANONICAL_RECAP \u00a714 locked grid):")
        for spec in PRICE_SPECS:
            print(
                f"  {spec.config_setting:<32} | "
                f"{spec.unit_amount_cents/100:>10,.2f} CAD | "
                f"{spec.cadence:<8} | {spec.canonical_label}"
            )
        print("=" * 72)
        print("Re-run without --dry-run to actually create Prices.")
        return 0

    # ----- Real-run: create Products and Prices -----
    price_ids: dict[str, str] = {}
    products_created: dict[str, str] = {}  # product_metadata_key -> product_id

    for spec in PRICE_SPECS:
        # Per-Product idempotency: collapse 7 specs into <=4 Products by
        # (product_name, product_metadata_key). Individual monthly + annual
        # share one Product; same for Team and Company. Intro fee is its
        # own Product.
        product_cache_key = (spec.product_name, spec.product_metadata_key)
        if product_cache_key in products_created:
            product_id = products_created[product_cache_key]
        else:
            try:
                product_id = get_or_create_product(
                    stripe,
                    name=spec.product_name,
                    metadata_tier=spec.product_metadata_key,
                    force_recreate=args.force_recreate,
                )
            except stripe.error.StripeError as exc:
                print(
                    f"FATAL: failed to get_or_create Product "
                    f"{spec.product_name!r}: {type(exc).__name__}: "
                    f"{exc.user_message or exc}",
                    file=sys.stderr,
                )
                return 1
            products_created[product_cache_key] = product_id

        # Per-Price idempotency: skip if a matching live Price already
        # exists, unless --force-recreate.
        if not args.force_recreate:
            try:
                existing = find_existing_price_for_spec(
                    stripe, product_id=product_id, spec=spec
                )
            except stripe.error.StripeError as exc:
                print(
                    f"FATAL: failed to list Prices for Product "
                    f"{product_id!r}: {type(exc).__name__}: "
                    f"{exc.user_message or exc}",
                    file=sys.stderr,
                )
                return 1
            if existing is not None:
                print(
                    f"NOTE: reusing existing Price {existing} for slot "
                    f"{spec.config_setting} ({spec.canonical_label}). "
                    f"Pass --force-recreate to mint a fresh one.",
                    file=sys.stderr,
                )
                price_ids[spec.config_setting] = existing
                continue

        try:
            price_id = create_price_for_spec(
                stripe, product_id=product_id, spec=spec
            )
        except stripe.error.StripeError as exc:
            print(
                f"FATAL: failed to create Price for {spec.config_setting!r} "
                f"({spec.canonical_label}): {type(exc).__name__}: "
                f"{exc.user_message or exc}",
                file=sys.stderr,
            )
            return 1
        price_ids[spec.config_setting] = price_id

    # ----- Emit the SSM-put PowerShell script -----
    try:
        emit_ssm_put_script(
            output_path=args.output_script,
            region=args.region,
            price_ids=price_ids,
            minted_at=minted_at,
        )
    except OSError as exc:
        print(
            f"FATAL: failed to write output script to "
            f"{args.output_script!r}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    # ----- Print summary -----
    print_summary_table(
        account_id=account_id,
        price_ids=price_ids,
        output_script=args.output_script,
        minted_at=minted_at,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
