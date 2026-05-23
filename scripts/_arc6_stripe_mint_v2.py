"""Arc 6 Commit 4 — Mint V2 SKUs in Stripe Live + write SSM params.

Mints 3 new Stripe Prices on 2 new Stripe Products against
``acct_1TX2BmRytQVRVXw7`` (Live), then writes 3 SSM SecureString
parameters under ``/luciel/production/stripe_price_*`` carrying the
new Price IDs. The Enterprise metered Price is **deferred** per
partner direction 2026-05-23 7:14 PM EDT
("Let's not the metered part right now partner we can do this we can
notice our business booming") and is not minted in this commit.

V2 SKU shape (CANONICAL §11.7 + §14 numeric lock):
  Luciel Pro       (Product, new)
    Pro monthly    price recurring/month  $349 CAD
    Pro annual     price recurring/year   $2,990 CAD
  Luciel Enterprise (Product, new)
    Enterprise floor  price recurring/year  $24,000 CAD  (published floor)
    Enterprise metered  DEFERRED -- not minted in this commit

Retained from V1 (untouched):
  Luciel Pilot Intro Fee (Product, prod_UWQeeXJ3CwJQYu)
    intro fee     price one-time           $100 CAD  (price_1TXNmnRytQVRVXw7GGfyJiaj)

Idempotence:
  Stripe Price + Product creates are NOT idempotent by default. To make
  re-runs safe, we use idempotency keys derived from the V2 vocabulary
  (`arc6-mint-v2-luciel-pro-2026-05-23`, `arc6-mint-v2-pro-monthly-...`,
  etc.). A re-run with the same idempotency key returns the original
  object without creating a new one.

Posture:
  AWS creds: env-only (caller exports), never to disk.
  Stripe sk_live_ key: loaded from SSM via the env loader; mode-locked
  before any mutation.

Safety properties:
  - sk_live_ mode assertion before any mutation
  - Asserts intro_fee Price + Product still active=True before AND after
    (so a misconfigured archive script run cannot have silently broken
    the control)
  - Captures full post-state JSON (Product IDs, Price IDs, amounts,
    intervals, currencies) for the audit record
  - SSM PutParameters use Type=SecureString and Overwrite=True so re-runs
    do not error if the param already exists; the new V2 Price IDs are
    the source of truth and any prior SSM value gets overwritten
"""

import os
import json
import sys
from datetime import datetime, timezone

import boto3
import stripe

stripe.api_key = os.environ["ARC6_STRIPE_SECRET_KEY"]
assert stripe.api_key.startswith("sk_live_"), "must be live mode"

NOW_ISO = datetime.now(timezone.utc).isoformat(timespec="seconds")

# -----------------------------------------------------------------------------
# Pre-mutation control assertion: intro_fee Price + Product still active.
# -----------------------------------------------------------------------------
intro_price_id = os.environ["ARC6_STRIPE_PRICE_INTRO_FEE"]
intro_price = stripe.Price.retrieve(intro_price_id)
intro_product = stripe.Product.retrieve(intro_price.product)
assert intro_price.active, f"intro_fee price {intro_price.id} is NOT active pre-mint -- ABORT"
assert intro_product.active, f"intro_fee product {intro_product.id} is NOT active pre-mint -- ABORT"
print(f"[arc6-mint] pre-mint control: intro_fee price {intro_price.id} active=True, product {intro_product.id} active=True OK", file=sys.stderr)

# -----------------------------------------------------------------------------
# Step 1: create the V2 Products.
# -----------------------------------------------------------------------------
pro_product = stripe.Product.create(
    name="Luciel Pro",
    description=(
        "Pro tier for individuals, founders, and small teams. "
        "Includes the AI lead-qualification platform, the embeddable widget, "
        "and per-Admin domain allowlist (rate-capped, see CANONICAL §14)."
    ),
    metadata={
        "tier": "pro",
        "billing_model": "flat",
        "arc": "arc6",
        "vocabulary": "v2",
        "created_at_utc": NOW_ISO,
    },
    idempotency_key="arc6-mint-v2-product-luciel-pro-2026-05-23",
)
print(f"[arc6-mint] created Product (Pro): {pro_product.id}", file=sys.stderr)

enterprise_product = stripe.Product.create(
    name="Luciel Enterprise",
    description=(
        "Enterprise tier with hybrid billing (published floor + metered "
        "overage, metered Price deferred to a future commit). "
        "Includes uncapped domain allowlist, sales-ops provisioning, and "
        "the full §14 entitlement matrix."
    ),
    metadata={
        "tier": "enterprise",
        "billing_model": "hybrid",
        "metered_unit_deferred": "true",
        "arc": "arc6",
        "vocabulary": "v2",
        "created_at_utc": NOW_ISO,
    },
    idempotency_key="arc6-mint-v2-product-luciel-enterprise-2026-05-23",
)
print(f"[arc6-mint] created Product (Enterprise): {enterprise_product.id}", file=sys.stderr)

# -----------------------------------------------------------------------------
# Step 2: create the 3 V2 Prices.
# -----------------------------------------------------------------------------
pro_monthly = stripe.Price.create(
    product=pro_product.id,
    nickname="Pro monthly (Arc 6 V2)",
    unit_amount=34900,  # $349.00 CAD
    currency="cad",
    recurring={"interval": "month", "interval_count": 1, "usage_type": "licensed"},
    metadata={
        "tier": "pro",
        "cadence": "monthly",
        "list_price_cad": "349",
        "arc": "arc6",
        "vocabulary": "v2",
        "created_at_utc": NOW_ISO,
    },
    idempotency_key="arc6-mint-v2-price-pro-monthly-2026-05-23",
)
print(f"[arc6-mint] created Price (Pro monthly): {pro_monthly.id} -> $349 CAD/mo", file=sys.stderr)

pro_annual = stripe.Price.create(
    product=pro_product.id,
    nickname="Pro annual (Arc 6 V2)",
    unit_amount=299000,  # $2,990.00 CAD
    currency="cad",
    recurring={"interval": "year", "interval_count": 1, "usage_type": "licensed"},
    metadata={
        "tier": "pro",
        "cadence": "annual",
        "list_price_cad": "2990",
        "discount_vs_monthly_equivalent": "approximately 28%",
        "arc": "arc6",
        "vocabulary": "v2",
        "created_at_utc": NOW_ISO,
    },
    idempotency_key="arc6-mint-v2-price-pro-annual-2026-05-23",
)
print(f"[arc6-mint] created Price (Pro annual): {pro_annual.id} -> $2,990 CAD/yr", file=sys.stderr)

enterprise_floor_annual = stripe.Price.create(
    product=enterprise_product.id,
    nickname="Enterprise floor annual (Arc 6 V2)",
    unit_amount=2400000,  # $24,000.00 CAD
    currency="cad",
    recurring={"interval": "year", "interval_count": 1, "usage_type": "licensed"},
    metadata={
        "tier": "enterprise",
        "cadence": "annual",
        "billing_model": "hybrid_floor",
        "list_price_cad": "24000",
        "metered_overage_unit": "deferred",
        "arc": "arc6",
        "vocabulary": "v2",
        "created_at_utc": NOW_ISO,
    },
    idempotency_key="arc6-mint-v2-price-enterprise-floor-annual-2026-05-23",
)
print(f"[arc6-mint] created Price (Enterprise floor): {enterprise_floor_annual.id} -> $24,000 CAD/yr", file=sys.stderr)

# -----------------------------------------------------------------------------
# Step 3: write SSM SecureString params (Overwrite=True for re-run safety).
# -----------------------------------------------------------------------------
ssm = boto3.client("ssm", region_name="ca-central-1")

ssm_targets = [
    ("/luciel/production/stripe_price_pro_monthly", pro_monthly.id),
    ("/luciel/production/stripe_price_pro_annual", pro_annual.id),
    ("/luciel/production/stripe_price_enterprise_floor_annual", enterprise_floor_annual.id),
]

ssm_results = []
for name, value in ssm_targets:
    resp = ssm.put_parameter(
        Name=name,
        Value=value,
        Type="SecureString",
        Overwrite=True,
        Description="Arc 6 Commit 4 V2 Stripe Price ID (CAD). See CANONICAL §17 Arc 6 Commit 4.",
    )
    ssm_results.append({
        "name": name,
        "value_prefix": value[:12] + "..." if len(value) > 12 else value,  # never log full price ID? actually price IDs are not secret; full is fine.
        "value": value,
        "version": resp.get("Version"),
        "tier": resp.get("Tier"),
    })
    print(f"[arc6-mint] put SSM {name} -> {value} (version {resp.get('Version')})", file=sys.stderr)

# -----------------------------------------------------------------------------
# Step 4: post-mutation control assertion (intro_fee still active).
# -----------------------------------------------------------------------------
intro_price_post = stripe.Price.retrieve(intro_price_id)
intro_product_post = stripe.Product.retrieve(intro_price_post.product)
assert intro_price_post.active, f"INTRO FEE PRICE WAS DEACTIVATED -- ABORT, this is a bug"
assert intro_product_post.active, f"INTRO FEE PRODUCT WAS DEACTIVATED -- ABORT, this is a bug"
print(f"[arc6-mint] post-mint control: intro_fee price still active=True, product still active=True OK", file=sys.stderr)

# -----------------------------------------------------------------------------
# Capture full post-state for the audit record.
# -----------------------------------------------------------------------------
out = {
    "completed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "stripe_account_id": stripe.Account.retrieve()["id"],
    "v2_products": [
        {
            "slot": "luciel_pro",
            "id": pro_product.id,
            "name": pro_product.name,
            "tier": pro_product.metadata.get("tier"),
            "billing_model": pro_product.metadata.get("billing_model"),
            "active": pro_product.active,
        },
        {
            "slot": "luciel_enterprise",
            "id": enterprise_product.id,
            "name": enterprise_product.name,
            "tier": enterprise_product.metadata.get("tier"),
            "billing_model": enterprise_product.metadata.get("billing_model"),
            "active": enterprise_product.active,
        },
    ],
    "v2_prices": [
        {
            "slot": "pro_monthly",
            "id": pro_monthly.id,
            "product": pro_monthly.product,
            "unit_amount_cad_cents": pro_monthly.unit_amount,
            "unit_amount_cad_dollars": pro_monthly.unit_amount / 100,
            "currency": pro_monthly.currency,
            "interval": pro_monthly.recurring.interval,
            "interval_count": pro_monthly.recurring.interval_count,
            "usage_type": pro_monthly.recurring.usage_type,
            "active": pro_monthly.active,
        },
        {
            "slot": "pro_annual",
            "id": pro_annual.id,
            "product": pro_annual.product,
            "unit_amount_cad_cents": pro_annual.unit_amount,
            "unit_amount_cad_dollars": pro_annual.unit_amount / 100,
            "currency": pro_annual.currency,
            "interval": pro_annual.recurring.interval,
            "interval_count": pro_annual.recurring.interval_count,
            "usage_type": pro_annual.recurring.usage_type,
            "active": pro_annual.active,
        },
        {
            "slot": "enterprise_floor_annual",
            "id": enterprise_floor_annual.id,
            "product": enterprise_floor_annual.product,
            "unit_amount_cad_cents": enterprise_floor_annual.unit_amount,
            "unit_amount_cad_dollars": enterprise_floor_annual.unit_amount / 100,
            "currency": enterprise_floor_annual.currency,
            "interval": enterprise_floor_annual.recurring.interval,
            "interval_count": enterprise_floor_annual.recurring.interval_count,
            "usage_type": enterprise_floor_annual.recurring.usage_type,
            "active": enterprise_floor_annual.active,
        },
    ],
    "deferred_prices": [
        {
            "slot": "enterprise_metered_unit",
            "reason": "Partner direction 2026-05-23 7:14 PM EDT: defer until Enterprise pipeline materializes",
            "status": "not_minted_in_this_commit",
        },
    ],
    "ssm_writes": ssm_results,
    "intro_fee_control": {
        "price_id": intro_price_post.id,
        "price_active": intro_price_post.active,
        "product_id": intro_product_post.id,
        "product_active": intro_product_post.active,
        "product_name": intro_product_post.name,
    },
}

json.dump(out, sys.stdout, indent=2, default=str)
print(file=sys.stdout)
