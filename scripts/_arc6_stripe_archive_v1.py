"""Arc 6 Commit 2 destructive sub-step — STEP 2 of 4: archive 6 Prices + 3 Products.

DESTRUCTIVE. Each Price.modify(active=False) and Product.modify(active=False)
is idempotent (re-running has no further effect once archived). Captures
post-state for the wipe record.
"""
import os
import json
import sys
from datetime import datetime, timezone

import stripe

stripe.api_key = os.environ["ARC6_STRIPE_SECRET_KEY"]
assert stripe.api_key.startswith("sk_live_"), "must be live mode"

PRICE_IDS_TO_ARCHIVE = {
    "individual_monthly":  os.environ["ARC6_STRIPE_PRICE_INDIVIDUAL"],
    "individual_annual":   os.environ["ARC6_STRIPE_PRICE_INDIVIDUAL_ANNUAL"],
    "team_monthly":        os.environ["ARC6_STRIPE_PRICE_TEAM_MONTHLY"],
    "team_annual":         os.environ["ARC6_STRIPE_PRICE_TEAM_ANNUAL"],
    "company_monthly":     os.environ["ARC6_STRIPE_PRICE_COMPANY_MONTHLY"],
    "company_annual":      os.environ["ARC6_STRIPE_PRICE_COMPANY_ANNUAL"],
}

# Products derived from the prices to archive (collected at runtime, not hardcoded
# so we don't drift from Stripe state).
PRODUCT_IDS_TO_ARCHIVE = {
    "luciel_individual": "prod_UW5XLqvK0A2PKl",
    "luciel_team":       "prod_UW5XMLeRBpgCJH",
    "luciel_company":    "prod_UW5XobSmV02qQp",
}

# Sanity: confirm the product list matches the price set before any mutation.
expected_products = set(PRODUCT_IDS_TO_ARCHIVE.values())
seen_products = set()
for slot, price_id in PRICE_IDS_TO_ARCHIVE.items():
    p = stripe.Price.retrieve(price_id)
    seen_products.add(p.product)
assert seen_products == expected_products, f"Product mismatch! Expected {expected_products}, got {seen_products}"
print(f"[arc6-archive] Sanity OK: 6 prices map to exactly the 3 expected products {sorted(expected_products)}", file=sys.stderr)

NOW_ISO = datetime.now(timezone.utc).isoformat(timespec="seconds")

actions = []

# Step A: Archive the 6 Prices first (Stripe doesn't strictly require this order,
# but archiving the price-children before the product-parent is the canonical pattern).
for slot, price_id in PRICE_IDS_TO_ARCHIVE.items():
    pre = stripe.Price.retrieve(price_id)
    pre_active = pre.active
    if pre_active:
        result = stripe.Price.modify(price_id, active=False)
        post_active = result.active
        action_status = "archived_now"
    else:
        post_active = pre_active
        action_status = "already_archived"
    actions.append({
        "step": "A",
        "type": "price",
        "slot": slot,
        "id": price_id,
        "product": pre.product,
        "pre_active": pre_active,
        "post_active": post_active,
        "action": action_status,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    print(f"[arc6-archive] price {slot} {price_id} {pre_active} \u2192 {post_active} ({action_status})", file=sys.stderr)

# Step B: Archive the 3 Products.
for slot, product_id in PRODUCT_IDS_TO_ARCHIVE.items():
    pre = stripe.Product.retrieve(product_id)
    pre_active = pre.active
    if pre_active:
        result = stripe.Product.modify(product_id, active=False)
        post_active = result.active
        action_status = "archived_now"
    else:
        post_active = pre_active
        action_status = "already_archived"
    actions.append({
        "step": "B",
        "type": "product",
        "slot": slot,
        "id": product_id,
        "name": pre.name,
        "pre_active": pre_active,
        "post_active": post_active,
        "action": action_status,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    print(f"[arc6-archive] product {slot} {product_id} ({pre.name}) {pre_active} \u2192 {post_active} ({action_status})", file=sys.stderr)

# Step C: Verify intro_fee Price + Product still active (control).
intro_price = stripe.Price.retrieve(os.environ["ARC6_STRIPE_PRICE_INTRO_FEE"])
intro_product = stripe.Product.retrieve(intro_price.product)
assert intro_price.active, f"INTRO FEE PRICE WAS ARCHIVED! id={intro_price.id} \u2014 ABORT, this is a bug"
assert intro_product.active, f"INTRO FEE PRODUCT WAS ARCHIVED! id={intro_product.id} \u2014 ABORT, this is a bug"
print(f"[arc6-archive] intro_fee price {intro_price.id} active={intro_price.active} (control OK)", file=sys.stderr)
print(f"[arc6-archive] intro_fee product {intro_product.id} ({intro_product.name}) active={intro_product.active} (control OK)", file=sys.stderr)

out = {
    "completed_at_utc": NOW_ISO,
    "stripe_account_id": stripe.Account.retrieve()["id"],
    "actions": actions,
    "intro_fee_control": {
        "price_id": intro_price.id,
        "price_active": intro_price.active,
        "product_id": intro_product.id,
        "product_active": intro_product.active,
        "product_name": intro_product.name,
    },
}

json.dump(out, sys.stdout, indent=2, default=str)
print(file=sys.stdout)
