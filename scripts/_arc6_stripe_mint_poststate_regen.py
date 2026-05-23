"""Arc 6 Commit 4 — Post-mint state regeneration.

Re-reads the just-minted V2 Products and Prices from Stripe Live and
captures clean post-state JSON. Used because the initial mint script
(`_arc6_stripe_mint_v2.py`) succeeded on all mutations but crashed on
JSON serialization (Stripe SDK 15.1.0 `metadata` does not support
`.get()` — known SDK gotcha from the Arc 6 resume contract).

This script is read-only against Stripe and works from the known IDs
captured in the mint script's stderr log.
"""
import os
import json
import sys
from datetime import datetime, timezone

import boto3
import stripe

stripe.api_key = os.environ["ARC6_STRIPE_SECRET_KEY"]
assert stripe.api_key.startswith("sk_live_"), "must be live mode"

# IDs captured from the mint stderr log at 2026-05-23T23:18 UTC.
V2_IDS = {
    "products": {
        "luciel_pro": "prod_UZXsUqCuumvw1v",
        "luciel_enterprise": "prod_UZXsY0kLJsFfop",
    },
    "prices": {
        "pro_monthly": "price_1TaOmORytQVRVXw77yRoEC8m",
        "pro_annual": "price_1TaOmORytQVRVXw7ElbQotvK",
        "enterprise_floor_annual": "price_1TaOmPRytQVRVXw7ozfKMFps",
    },
}

INTRO_PRICE_ID = os.environ["ARC6_STRIPE_PRICE_INTRO_FEE"]


def _md(stripe_obj, key, default=None):
    """Stripe SDK 15.1.0 metadata accessor.

    metadata is a StripeObject. dict(metadata) raises KeyError: 0
    (StripeObject iter is positional, not dict-keyed). .get() raises
    AttributeError. Correct pattern is attribute access via getattr.
    Verified empirically on minted Product prod_UZXsUqCuumvw1v at
    2026-05-23 23:25 UTC.
    """
    return getattr(stripe_obj.metadata, key, default)


# Read the 2 Products.
products_out = []
for slot, pid in V2_IDS["products"].items():
    p = stripe.Product.retrieve(pid)
    products_out.append({
        "slot": slot,
        "id": p.id,
        "name": p.name,
        "tier": _md(p, "tier"),
        "billing_model": _md(p, "billing_model"),
        "arc": _md(p, "arc"),
        "vocabulary": _md(p, "vocabulary"),
        "created_at_utc_metadata": _md(p, "created_at_utc"),
        "active": p.active,
    })

# Read the 3 Prices.
prices_out = []
for slot, pid in V2_IDS["prices"].items():
    p = stripe.Price.retrieve(pid)
    prices_out.append({
        "slot": slot,
        "id": p.id,
        "product": p.product,
        "nickname": p.nickname,
        "unit_amount_cad_cents": p.unit_amount,
        "unit_amount_cad_dollars": p.unit_amount / 100,
        "currency": p.currency,
        "type": p.type,
        "interval": p.recurring.interval if p.recurring else None,
        "interval_count": p.recurring.interval_count if p.recurring else None,
        "usage_type": p.recurring.usage_type if p.recurring else None,
        "tier": _md(p, "tier"),
        "cadence": _md(p, "cadence"),
        "arc": _md(p, "arc"),
        "vocabulary": _md(p, "vocabulary"),
        "active": p.active,
    })

# Read the intro fee control.
intro_price = stripe.Price.retrieve(INTRO_PRICE_ID)
intro_product = stripe.Product.retrieve(intro_price.product)
assert intro_price.active and intro_product.active, "intro fee control failed at regen — INVESTIGATE"

# Read the SSM params we just put (no DescribeParameters, just GetParameter on known names).
ssm = boto3.client("ssm", region_name="ca-central-1")
ssm_targets = [
    "/luciel/production/stripe_price_pro_monthly",
    "/luciel/production/stripe_price_pro_annual",
    "/luciel/production/stripe_price_enterprise_floor_annual",
]
ssm_results = []
for name in ssm_targets:
    r = ssm.get_parameter(Name=name, WithDecryption=True)
    p = r["Parameter"]
    ssm_results.append({
        "name": p["Name"],
        "value": p["Value"],
        "type": p["Type"],
        "version": p["Version"],
        "last_modified_utc": p["LastModifiedDate"].isoformat(),
    })

out = {
    "captured_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "stripe_account_id": stripe.Account.retrieve()["id"],
    "v2_products": products_out,
    "v2_prices": prices_out,
    "ssm_writes": ssm_results,
    "intro_fee_control": {
        "price_id": intro_price.id,
        "price_active": intro_price.active,
        "product_id": intro_product.id,
        "product_active": intro_product.active,
        "product_name": intro_product.name,
    },
    "deferred_prices": [
        {
            "slot": "enterprise_metered_unit",
            "reason": "Partner direction 2026-05-23 7:14 PM EDT: defer until Enterprise pipeline materializes",
            "status": "not_minted_in_this_commit",
        },
    ],
}

json.dump(out, sys.stdout, indent=2, default=str)
print(file=sys.stdout)
