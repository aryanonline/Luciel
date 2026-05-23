"""Arc 6 Commit 2 destructive sub-step — STEP 1 of 4: enumerate pre-state.

READ-ONLY. No Stripe mutations. Pulls:
  - All active + trialing + past_due subscriptions in Stripe Live
  - All 6 old Prices (Individual/Team/Company monthly+annual) to confirm
    they are not archived and to capture their pre-state metadata
  - The intro_fee Price as a control (must NOT be archived)
Writes a single fenced-record block to stdout that the caller pipes into
arc6-out/C-arc6-stripe-wipe-record.md.
"""
import os
import json
import sys
import time
from datetime import datetime, timezone

import stripe

stripe.api_key = os.environ["ARC6_STRIPE_SECRET_KEY"]
assert stripe.api_key.startswith("sk_live_"), "must be live mode"

# The 6 Price IDs to archive + the intro_fee control (must NOT be archived).
PRICE_IDS_TO_ARCHIVE = {
    "individual_monthly":  os.environ["ARC6_STRIPE_PRICE_INDIVIDUAL"],
    "individual_annual":   os.environ["ARC6_STRIPE_PRICE_INDIVIDUAL_ANNUAL"],
    "team_monthly":        os.environ["ARC6_STRIPE_PRICE_TEAM_MONTHLY"],
    "team_annual":         os.environ["ARC6_STRIPE_PRICE_TEAM_ANNUAL"],
    "company_monthly":     os.environ["ARC6_STRIPE_PRICE_COMPANY_MONTHLY"],
    "company_annual":      os.environ["ARC6_STRIPE_PRICE_COMPANY_ANNUAL"],
}
INTRO_FEE_PRICE_ID = os.environ["ARC6_STRIPE_PRICE_INTRO_FEE"]

NOW_ISO = datetime.now(timezone.utc).isoformat(timespec="seconds")

out = {
    "captured_at_utc": NOW_ISO,
    "stripe_account_id": stripe.Account.retrieve()["id"],
    "subscriptions": [],
    "prices_to_archive": [],
    "intro_fee_price_control": None,
}

# 1) Enumerate ALL non-canceled subscriptions (active, trialing, past_due, unpaid, paused, incomplete).
#    The partner directive says "23 internal/test subs" — wipe all of them.
#    Using status='all' returns active + trialing + past_due + unpaid + canceled + incomplete +
#    incomplete_expired + paused; we filter out already-canceled in Python so the record only
#    captures subs we will actually act on.
TARGET_STATUSES = {"active", "trialing", "past_due", "unpaid", "incomplete", "paused"}
seen_ids = set()
for sub in stripe.Subscription.list(status="all", limit=100).auto_paging_iter():
    if sub.id in seen_ids:
        continue
    seen_ids.add(sub.id)
    if sub.status in TARGET_STATUSES:
        out["subscriptions"].append({
            "id": sub.id,
            "customer": sub.customer,
            "status": sub.status,
            "created_utc": datetime.fromtimestamp(sub.created, tz=timezone.utc).isoformat(timespec="seconds"),
            "current_period_end_utc": datetime.fromtimestamp(sub.current_period_end, tz=timezone.utc).isoformat(timespec="seconds") if hasattr(sub, "current_period_end") and sub.current_period_end else None,
            "items": [
                {
                    "price_id": item.price.id,
                    "price_nickname": item.price.nickname,
                    "unit_amount": item.price.unit_amount,
                    "currency": item.price.currency,
                    "recurring_interval": (item.price.recurring or {}).get("interval") if item.price.recurring else None,
                    "quantity": item.quantity,
                }
                for item in sub["items"].data
            ],
            "metadata": json.loads(str(sub.metadata)) if sub.metadata and str(sub.metadata).strip() else {},
        })

# 2) Confirm each of the 6 old Prices exists, is currently active, and capture its metadata.
for slot, price_id in PRICE_IDS_TO_ARCHIVE.items():
    p = stripe.Price.retrieve(price_id)
    out["prices_to_archive"].append({
        "slot": slot,
        "id": p.id,
        "product": p.product,
        "nickname": p.nickname,
        "active": p.active,
        "unit_amount": p.unit_amount,
        "currency": p.currency,
        "type": p.type,
        "recurring": {"interval": p.recurring.interval, "interval_count": p.recurring.interval_count, "usage_type": p.recurring.usage_type} if p.recurring else None,
        "created_utc": datetime.fromtimestamp(p.created, tz=timezone.utc).isoformat(timespec="seconds"),
    })

# 3) Capture intro_fee as control (must NOT be archived).
p = stripe.Price.retrieve(INTRO_FEE_PRICE_ID)
out["intro_fee_price_control"] = {
    "id": p.id,
    "product": p.product,
    "nickname": p.nickname,
    "active": p.active,
    "unit_amount": p.unit_amount,
    "currency": p.currency,
    "type": p.type,
    "recurring": {"interval": p.recurring.interval, "interval_count": p.recurring.interval_count, "usage_type": p.recurring.usage_type} if p.recurring else None,
}

# Summary print to stderr for sanity, structured JSON to stdout for piping.
print(f"[arc6-prestate] account: {out['stripe_account_id']}", file=sys.stderr)
print(f"[arc6-prestate] non-canceled subs found: {len(out['subscriptions'])}", file=sys.stderr)
print(f"[arc6-prestate] subs by status: {dict((s, sum(1 for x in out['subscriptions'] if x['status']==s)) for s in set(x['status'] for x in out['subscriptions']))}", file=sys.stderr)
print(f"[arc6-prestate] prices_to_archive: 6 confirmed active={sum(1 for p in out['prices_to_archive'] if p['active'])} archived_already={sum(1 for p in out['prices_to_archive'] if not p['active'])}", file=sys.stderr)
print(f"[arc6-prestate] intro_fee active: {out['intro_fee_price_control']['active']} (must be True)", file=sys.stderr)

json.dump(out, sys.stdout, indent=2, default=str)
print(file=sys.stdout)
