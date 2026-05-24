"""Arc 7 Commit 1 Slice 2 — Mint Enterprise monthly + annual Prices, archive floor_annual.

Arc 7 doctrine pivot (2026-05-24, see arc7 commit 9585eda):

    Partner: "Since we have abuse limits for each tier I don't think we
    need to include the metering option for enterprise."

    Partner: "We agreed upon on each tier to be easily self serve so no
    need to contact sales or anything. Lets also make enterprise tier a
    monthly option as well the say way we have for pro tier."

Enterprise becomes FLAT-recurring symmetric with Pro: self-serve via
Stripe Checkout in either monthly or annual cadence, no metering, abuse
ceilings enforced in TIER_ENTITLEMENTS instead.

This script mutates Stripe Live in three steps (each Stripe call uses
an idempotency key so re-runs are safe):

  1. Mint Price ``stripe_price_enterprise_monthly`` -> $2,800 CAD/mo
     recurring/month, attached to the EXISTING Luciel Enterprise
     Product (prod_UZXsY0kLJsFfop). Idempotency key:
     ``arc7-mint-price-enterprise-monthly-2026-05-24``.

  2. Mint Price ``stripe_price_enterprise_annual`` -> $24,000 CAD/yr
     recurring/year, attached to the SAME Enterprise Product. This
     replaces the prior ``enterprise_floor_annual`` slot at the same
     price point and on the same Product, just under the new
     vocabulary (no "floor" framing under flat-recurring). Idempotency
     key: ``arc7-mint-price-enterprise-annual-2026-05-24``.

  3. Archive (active=False) the prior Price
     ``price_1TaOmPRytQVRVXw7ozfKMFps`` (the Arc 6 ``enterprise_floor_annual``
     slot). This is REVERSIBLE -- Stripe lets you re-activate any
     archived Price -- and is the standard Stripe pattern for
     superseding a recurring Price. Existing Subscriptions on the old
     Price continue to bill normally; only new Checkout sessions stop
     being able to select it. Per partner doctrine "whatever we ship
     out in our code and prod and schema must be aligned with this
     vision" the floor_annual slot must not be reachable post-pivot.

After Stripe mutations land, the script SSM-puts the two new Price IDs
under ``/luciel/production/stripe_price_enterprise_monthly`` and
``/luciel/production/stripe_price_enterprise_annual``. Both are written
as Type=SecureString with Overwrite=True for re-run safety. The prior
``/luciel/production/stripe_price_enterprise_floor_annual`` SSM param
is NOT deleted here -- it gets removed at Arc 7 Commit 2 alongside the
``subscriptions.billing_model`` column drop, so any in-flight rollback
within the same arc remains possible.

Pre/post controls (matches Arc 6 Commit 4 safety pattern):

  - Pre-mutation: assert intro_fee Price + Product still active=True,
    AND assert the Pro/Enterprise V2 Products + the 2 V2 Pro Prices
    minted at Arc 6 Commit 4 are still active. Any of these flipping
    inactive between Arc 6 and Arc 7 would indicate Stripe-side drift
    we must investigate before adding new Prices.
  - Post-mutation: re-assert the same controls (intro_fee still active,
    pro_monthly + pro_annual still active), AND assert the two new
    Prices are active and the archived Price is now active=False.

Posture:
  AWS creds:    env-only (caller exports AWS_ACCESS_KEY_ID +
                AWS_SECRET_ACCESS_KEY before invoking python). Never
                written to disk or any workspace file.
  Stripe key:   loaded from SSM SecureString at
                ``/luciel/production/stripe_secret_key`` via the
                LucielSandboxStripeScope grant. NEVER echoed to stderr,
                stdout, or any artifact. Mode-locked to sk_live_ before
                any Stripe API call.
  Audit JSON:   written to /home/user/workspace/luciel/arc7-out/
                ``arc7-commit1-slice2-stripe-mint-poststate.json``
                for the audit record. Only Price IDs (non-secret) and
                SSM param names land in the JSON; the Stripe Live key
                is never serialised.

Re-run posture:
  Stripe idempotency keys make the two new Price creates safe to re-run.
  The archive step uses an active=False modify which is also idempotent
  (no-op on a Price that is already inactive).

SSM scope-expansion requirement (5-gate protocol):
  The luciel-sandbox-agent IAM principal does NOT hold ssm:PutParameter
  by default. Partner must attach the policy
  ``LucielSandboxSsmStripeEnterpriseSymmetry`` (drafted under
  /home/user/workspace/luciel/arc7-out/iam/) via Console before running
  this script, and detach immediately after the script completes.
  The script will fail-fast at the SSM step if the policy is not
  attached -- this is the intended posture.
"""

import json
import sys
from datetime import datetime, timezone

import boto3
import stripe

NOW_ISO = datetime.now(timezone.utc).isoformat(timespec="seconds")

# --------------------------------------------------------------------------
# Known Arc 6 IDs (captured at Arc 6 Commit 4 close, b4877a5 era).
# --------------------------------------------------------------------------
ENTERPRISE_PRODUCT_ID = "prod_UZXsY0kLJsFfop"  # Luciel Enterprise (re-used here)
PRO_PRODUCT_ID = "prod_UZXsUqCuumvw1v"  # control: must still be active
PRO_MONTHLY_PRICE_ID = "price_1TaOmORytQVRVXw77yRoEC8m"  # control
PRO_ANNUAL_PRICE_ID = "price_1TaOmORytQVRVXw7ElbQotvK"  # control
ENTERPRISE_FLOOR_ANNUAL_PRICE_ID = "price_1TaOmPRytQVRVXw7ozfKMFps"  # ARCHIVE TARGET
INTRO_PRICE_ID = "price_1TXNmnRytQVRVXw7GGfyJiaj"  # public Arc 1 Pilot Intro Fee Price ID; not secret

# --------------------------------------------------------------------------
# Mode lock: this script mutates Stripe LIVE. Pull the Stripe key from
# SSM SecureString (not env) -- the sandbox-agent + LucielSandboxStripeScope
# grant ssm:GetParameter on parameter/luciel/production/stripe_* and the
# KMS-via-SSM decrypt path. This avoids requiring the operator to paste
# the sk_live_ key as an env var. Fail closed on sk_test_.
# --------------------------------------------------------------------------
_ssm_pre = boto3.client("ssm", region_name="ca-central-1")
_stripe_key_resp = _ssm_pre.get_parameter(
    Name="/luciel/production/stripe_secret_key",
    WithDecryption=True,
)
stripe.api_key = _stripe_key_resp["Parameter"]["Value"]
assert stripe.api_key.startswith("sk_live_"), (
    "stripe_secret_key from SSM is not a live-mode key (sk_live_); refusing "
    "to run because the existing minted Products + Prices live in the "
    "production account acct_1TX2BmRytQVRVXw7."
)
print(
    "[arc7-mint] loaded Stripe Live key from SSM "
    "(/luciel/production/stripe_secret_key) -- key value not echoed.",
    file=sys.stderr,
)

# --------------------------------------------------------------------------
# Pre-mutation control assertions.
# --------------------------------------------------------------------------
print("[arc7-mint] pre-mutation control assertions...", file=sys.stderr)

intro_price = stripe.Price.retrieve(INTRO_PRICE_ID)
intro_product = stripe.Product.retrieve(intro_price.product)
assert intro_price.active, f"intro_fee price {intro_price.id} NOT active pre-mint — ABORT"
assert intro_product.active, f"intro_fee product {intro_product.id} NOT active pre-mint — ABORT"

enterprise_product = stripe.Product.retrieve(ENTERPRISE_PRODUCT_ID)
assert enterprise_product.active, (
    f"Enterprise product {ENTERPRISE_PRODUCT_ID} NOT active pre-mint — "
    "Stripe-side drift between Arc 6 and Arc 7 — ABORT"
)

pro_product = stripe.Product.retrieve(PRO_PRODUCT_ID)
assert pro_product.active, f"Pro product {PRO_PRODUCT_ID} NOT active pre-mint — ABORT"

pro_monthly = stripe.Price.retrieve(PRO_MONTHLY_PRICE_ID)
pro_annual = stripe.Price.retrieve(PRO_ANNUAL_PRICE_ID)
assert pro_monthly.active, f"pro_monthly {PRO_MONTHLY_PRICE_ID} NOT active pre-mint — ABORT"
assert pro_annual.active, f"pro_annual {PRO_ANNUAL_PRICE_ID} NOT active pre-mint — ABORT"

floor_annual_pre = stripe.Price.retrieve(ENTERPRISE_FLOOR_ANNUAL_PRICE_ID)
assert floor_annual_pre.active, (
    f"floor_annual {ENTERPRISE_FLOOR_ANNUAL_PRICE_ID} already inactive pre-mint — "
    "either a prior re-run already archived it (fine), or external drift "
    "(investigate). Re-run posture: comment out this assert and proceed."
)

print(
    "[arc7-mint] pre-mutation controls OK: intro_fee active, "
    "Pro/Enterprise products active, pro_monthly active, pro_annual active, "
    "floor_annual active (will be archived).",
    file=sys.stderr,
)

# --------------------------------------------------------------------------
# Step 1: mint Enterprise monthly Price ($2,800 CAD/mo).
# --------------------------------------------------------------------------
enterprise_monthly = stripe.Price.create(
    product=ENTERPRISE_PRODUCT_ID,
    nickname="Enterprise monthly (Arc 7 flat-symmetric)",
    unit_amount=280000,  # $2,800.00 CAD
    currency="cad",
    recurring={"interval": "month", "interval_count": 1, "usage_type": "licensed"},
    metadata={
        "tier": "enterprise",
        "cadence": "monthly",
        "billing_model": "flat",
        "list_price_cad": "2800",
        "arc": "arc7",
        "vocabulary": "v2",
        "doctrine": "flat-recurring-symmetric-with-pro",
        "created_at_utc": NOW_ISO,
    },
    idempotency_key="arc7-mint-price-enterprise-monthly-2026-05-24",
)
print(
    f"[arc7-mint] created Price (Enterprise monthly): {enterprise_monthly.id} -> $2,800 CAD/mo",
    file=sys.stderr,
)

# --------------------------------------------------------------------------
# Step 2: mint Enterprise annual Price ($24,000 CAD/yr) under new vocabulary.
# --------------------------------------------------------------------------
enterprise_annual = stripe.Price.create(
    product=ENTERPRISE_PRODUCT_ID,
    nickname="Enterprise annual (Arc 7 flat-symmetric)",
    unit_amount=2400000,  # $24,000.00 CAD
    currency="cad",
    recurring={"interval": "year", "interval_count": 1, "usage_type": "licensed"},
    metadata={
        "tier": "enterprise",
        "cadence": "annual",
        "billing_model": "flat",
        "list_price_cad": "24000",
        "discount_vs_monthly_equivalent": "approximately 28.6%",
        "arc": "arc7",
        "vocabulary": "v2",
        "doctrine": "flat-recurring-symmetric-with-pro",
        "supersedes_slot": "enterprise_floor_annual",
        "supersedes_price_id": ENTERPRISE_FLOOR_ANNUAL_PRICE_ID,
        "created_at_utc": NOW_ISO,
    },
    idempotency_key="arc7-mint-price-enterprise-annual-2026-05-24",
)
print(
    f"[arc7-mint] created Price (Enterprise annual): {enterprise_annual.id} -> $24,000 CAD/yr",
    file=sys.stderr,
)

# --------------------------------------------------------------------------
# Step 3: archive (active=False) the prior floor_annual Price.
# --------------------------------------------------------------------------
floor_annual_archived = stripe.Price.modify(
    ENTERPRISE_FLOOR_ANNUAL_PRICE_ID,
    active=False,
    metadata={
        "archived_at_utc": NOW_ISO,
        "archived_by_arc": "arc7",
        "archived_reason": "superseded_by_arc7_flat_recurring_symmetric",
        "superseded_by_price_id": enterprise_annual.id,
    },
)
assert not floor_annual_archived.active, "archive of floor_annual did not stick — ABORT"
print(
    f"[arc7-mint] archived Price (Enterprise floor_annual): {ENTERPRISE_FLOOR_ANNUAL_PRICE_ID} active=False",
    file=sys.stderr,
)

# --------------------------------------------------------------------------
# Step 4: SSM SecureString puts for the 2 new Price IDs (Overwrite=True).
# --------------------------------------------------------------------------
ssm = boto3.client("ssm", region_name="ca-central-1")

ssm_targets = [
    ("/luciel/production/stripe_price_enterprise_monthly", enterprise_monthly.id),
    ("/luciel/production/stripe_price_enterprise_annual", enterprise_annual.id),
]

ssm_results = []
for name, value in ssm_targets:
    resp = ssm.put_parameter(
        Name=name,
        Value=value,
        Type="SecureString",
        Overwrite=True,
        Description=(
            "Arc 7 Commit 1 Slice 2 Stripe Price ID (CAD). "
            "Enterprise FLAT-recurring symmetric with Pro. "
            "See CANONICAL_RECAP §17 Arc 7."
        ),
    )
    ssm_results.append({
        "name": name,
        "value": value,
        "version": resp.get("Version"),
        "tier": resp.get("Tier"),
    })
    print(
        f"[arc7-mint] put SSM {name} -> {value} (version {resp.get('Version')})",
        file=sys.stderr,
    )

# --------------------------------------------------------------------------
# Step 5: post-mutation control assertions.
# --------------------------------------------------------------------------
print("[arc7-mint] post-mutation control assertions...", file=sys.stderr)

intro_price_post = stripe.Price.retrieve(INTRO_PRICE_ID)
intro_product_post = stripe.Product.retrieve(intro_price_post.product)
assert intro_price_post.active, "intro_fee price flipped inactive post-mint — INVESTIGATE"
assert intro_product_post.active, "intro_fee product flipped inactive post-mint — INVESTIGATE"

pro_monthly_post = stripe.Price.retrieve(PRO_MONTHLY_PRICE_ID)
pro_annual_post = stripe.Price.retrieve(PRO_ANNUAL_PRICE_ID)
assert pro_monthly_post.active, "pro_monthly flipped inactive post-mint — INVESTIGATE"
assert pro_annual_post.active, "pro_annual flipped inactive post-mint — INVESTIGATE"

enterprise_monthly_post = stripe.Price.retrieve(enterprise_monthly.id)
enterprise_annual_post = stripe.Price.retrieve(enterprise_annual.id)
assert enterprise_monthly_post.active, "newly minted enterprise_monthly NOT active — INVESTIGATE"
assert enterprise_annual_post.active, "newly minted enterprise_annual NOT active — INVESTIGATE"

floor_annual_post = stripe.Price.retrieve(ENTERPRISE_FLOOR_ANNUAL_PRICE_ID)
assert not floor_annual_post.active, (
    "floor_annual did not stay archived post-mutation — INVESTIGATE"
)

print(
    "[arc7-mint] post-mutation controls OK: intro_fee active, pro_monthly active, "
    "pro_annual active, new enterprise_monthly active, new enterprise_annual active, "
    "floor_annual archived.",
    file=sys.stderr,
)

# --------------------------------------------------------------------------
# Capture full post-state JSON for the audit record.
# --------------------------------------------------------------------------
out = {
    "completed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "stripe_account_id": stripe.Account.retrieve()["id"],
    "arc": "arc7",
    "commit": "c1-s2",
    "doctrine": "enterprise-flat-recurring-symmetric-with-pro",
    "products": {
        "luciel_enterprise": ENTERPRISE_PRODUCT_ID,
        "luciel_pro": PRO_PRODUCT_ID,
    },
    "minted_prices": [
        {
            "slot": "enterprise_monthly",
            "id": enterprise_monthly.id,
            "product": enterprise_monthly.product,
            "unit_amount_cad_cents": enterprise_monthly.unit_amount,
            "unit_amount_cad_dollars": enterprise_monthly.unit_amount / 100,
            "currency": enterprise_monthly.currency,
            "interval": enterprise_monthly.recurring.interval,
            "interval_count": enterprise_monthly.recurring.interval_count,
            "usage_type": enterprise_monthly.recurring.usage_type,
            "nickname": enterprise_monthly.nickname,
            "active": enterprise_monthly_post.active,
        },
        {
            "slot": "enterprise_annual",
            "id": enterprise_annual.id,
            "product": enterprise_annual.product,
            "unit_amount_cad_cents": enterprise_annual.unit_amount,
            "unit_amount_cad_dollars": enterprise_annual.unit_amount / 100,
            "currency": enterprise_annual.currency,
            "interval": enterprise_annual.recurring.interval,
            "interval_count": enterprise_annual.recurring.interval_count,
            "usage_type": enterprise_annual.recurring.usage_type,
            "nickname": enterprise_annual.nickname,
            "active": enterprise_annual_post.active,
        },
    ],
    "archived_prices": [
        {
            "slot": "enterprise_floor_annual",
            "id": ENTERPRISE_FLOOR_ANNUAL_PRICE_ID,
            "active_post": floor_annual_post.active,
            "superseded_by_price_id": enterprise_annual.id,
        },
    ],
    "control_prices_still_active": {
        "intro_fee": INTRO_PRICE_ID,
        "pro_monthly": PRO_MONTHLY_PRICE_ID,
        "pro_annual": PRO_ANNUAL_PRICE_ID,
    },
    "ssm_puts": ssm_results,
}

# Print full audit JSON to stdout — caller captures into arc7-out/.
print(json.dumps(out, indent=2))
print("[arc7-mint] DONE.", file=sys.stderr)
