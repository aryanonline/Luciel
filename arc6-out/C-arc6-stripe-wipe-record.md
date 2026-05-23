# Arc 6 — Commit 2 — Stripe Live Wipe Record (V1 SKU teardown)

**Window:** 2026-05-23 22:50 UTC (prestate) → 22:57 UTC (poststate)
**Stripe account:** `acct_1TX2BmRytQVRVXw7` (Live)
**Operator:** `arn:aws:iam::729005488042:user/luciel-sandbox-agent`
**Doctrine:** Path A (aggressive cleanup) + AGGRESSIVE-CLEANUP carry-forward
**Posture:** prod-credential — env vars only, never to disk, never echoed, no subagents
**Scope expansion:** `LucielSandboxStripeScope` (3 Sids: SSM read/write `/luciel/production/stripe_*`, KMS via SSM)

---

## 1. Why this record exists

Commit 2 of Arc 6 retires the V1 SKU shape (Individual / Team / Company × monthly+annual = 6 recurring Prices on 3 Products) so the V2 shape (Free / Pro / Enterprise) can be minted clean in Commit 4. The session brief expected to "wipe 23 internal/test subs" — that count turned out to refer to a mixture of object types, so this record also pins the prestate triangulation to prevent doctrine drift later.

This commit has two halves:

- **Paperwork-half** (already pushed as `791e3d8`): scope-expansion policy + sandbox credential posture runbook update + preflight §7 correction + CANONICAL §17 anchor.
- **Destructive-half** (this record): the actual Stripe-side archive against Live.

---

## 2. Prestate triangulation — "what did the 23 actually refer to?"

The original Arc 6 brief said "Wipe 23 internal/test subs in Stripe Live." When we enumerated `Subscription.list(status="all")` against the Live account at 22:50 UTC, we found:

| Object type | Count | Status breakdown |
|---|---|---|
| `Subscription` | **10** | All `status=canceled` (terminal) |
| `Checkout.Session` | **13** | 10 `complete` (which created the 10 canceled subs) + 3 `expired` (never became subs) |
| **Total objects** | **23** | Reconciles the "23 internal/test subs" framing in the brief |

**Customers:** 10 total, all `aryans.www+*@gmail.com` aliases (developer-test signups during Arcs 1–5).

**Reconciliation conclusion:** there are **zero live (non-terminal) subscriptions** to revoke. The "23" the brief named was object-count, not active-sub count; checkout-session objects in Stripe Live are immutable terminal records (they cannot be deleted or archived; they age out via Stripe's normal retention). The destructive action that *can* and *must* happen is archiving the V1 Prices + V1 Products so they cannot be selected for new checkouts.

Partner explicitly approved "archive Products + Prices" after I surfaced this finding (6:50 PM EDT pre-confirm exchange).

---

## 3. Prestate evidence (full JSON)

File: `arc6-out/C-arc6-stripe-prestate.json` (113 lines, captured 22:50 UTC)

Summary of the 6 Prices targeted for archive:

| Slot | Price ID | Amount (CAD) | Interval | Product |
|---|---|---|---|---|
| `individual_monthly` | `price_1TX3MGRytQVRVXw7TPKQiCAS` | $30/mo | month×1 | `prod_UW5XLqvK0A2PKl` (Luciel Individual) |
| `individual_annual` | `price_1TX3MGRytQVRVXw7408HJhsG` | $300/yr | year×1 | `prod_UW5XLqvK0A2PKl` (Luciel Individual) |
| `team_monthly` | `price_1TX3MHRytQVRVXw7LazGyxZH` | $300/mo | month×1 | `prod_UW5XMLeRBpgCJH` (Luciel Team) |
| `team_annual` | `price_1TX3MHRytQVRVXw7pVbzGSX0` | $3,000/yr | year×1 | `prod_UW5XMLeRBpgCJH` (Luciel Team) |
| `company_monthly` | `price_1TX3MIRytQVRVXw79cYZwjVw` | $2,000/mo | month×1 | `prod_UW5XobSmV02qQp` (Luciel Company) |
| `company_annual` | `price_1TX3MIRytQVRVXw7b7qXMaX1` | $20,000/yr | year×1 | `prod_UW5XobSmV02qQp` (Luciel Company) |

All 6 prices were `active=true` at prestate. All created 2026-05-14 17:47 UTC (Arc 2 mint).

**Control (must remain active):** `price_1TXNmnRytQVRVXw7GGfyJiaj` (Luciel pilot intro fee, $100 CAD one-time) on product `prod_UWQeeXJ3CwJQYu` (Luciel Pilot Intro Fee).

---

## 4. Archive script & idempotence design

Script: `scripts/_arc6_stripe_archive_v1.py` (119 lines)

Design properties:

- **Idempotent:** each `Price.modify(active=False)` / `Product.modify(active=False)` is a no-op if already archived. The script records `action="already_archived"` vs `action="archived_now"` per object.
- **Pre-mutation sanity check (line 40):** before touching anything, retrieves all 6 Prices and asserts the union of their `.product` IDs equals exactly the 3 expected product IDs. Aborts loudly on any drift, so we don't archive Prices that point to a Product we didn't expect.
- **Mode lock (line 15):** asserts `stripe.api_key.startswith("sk_live_")` so a misloaded test key cannot pretend to archive Live.
- **Control assertion (lines 99–100):** after archival, re-retrieves the intro-fee Price and its parent Product and asserts both are still `active=True`. If a bug or env-var collision had caused the script to archive the wrong thing, this assertion would fail post-mutation. Belt-and-suspenders against the most catastrophic possible drift.
- **Order:** Prices first, Products second (canonical pattern — child rows before parent rows). Stripe does not strictly require this order, but it makes the audit trail readable.
- **Output:** JSON to stdout (full action log + intro_fee control state + Stripe account ID + completion timestamp). Stderr is human-readable per-action progress.

---

## 5. Execution

### Environment loading

- AWS creds: exported via env vars from the §4 paste protocol; never written to disk.
- STS check: `sts:GetCallerIdentity` resolved to `arn:aws:iam::729005488042:user/luciel-sandbox-agent` ✓
- Stripe env: sourced from SSM via `scripts/_arc6_load_stripe_env.sh` (KMS-decrypted SecureString params under `/luciel/production/stripe_*`). Loader confirmed `sk_live_` prefix. No values echoed.

### Script run

```
$ python3 scripts/_arc6_stripe_archive_v1.py > /tmp/arc6_archive_poststate.json 2>&1
exit 0
```

Action log (from stderr, copied verbatim):

```
[arc6-archive] Sanity OK: 6 prices map to exactly the 3 expected products ['prod_UW5XLqvK0A2PKl', 'prod_UW5XMLeRBpgCJH', 'prod_UW5XobSmV02qQp']
[arc6-archive] price individual_monthly price_1TX3MGRytQVRVXw7TPKQiCAS True → False (archived_now)
[arc6-archive] price individual_annual  price_1TX3MGRytQVRVXw7408HJhsG True → False (archived_now)
[arc6-archive] price team_monthly       price_1TX3MHRytQVRVXw7LazGyxZH True → False (archived_now)
[arc6-archive] price team_annual        price_1TX3MHRytQVRVXw7pVbzGSX0 True → False (archived_now)
[arc6-archive] price company_monthly    price_1TX3MIRytQVRVXw79cYZwjVw True → False (archived_now)
[arc6-archive] price company_annual     price_1TX3MIRytQVRVXw7b7qXMaX1 True → False (archived_now)
[arc6-archive] product luciel_individual prod_UW5XLqvK0A2PKl (Luciel Individual) True → False (archived_now)
[arc6-archive] product luciel_team       prod_UW5XMLeRBpgCJH (Luciel Team)       True → False (archived_now)
[arc6-archive] product luciel_company    prod_UW5XobSmV02qQp (Luciel Company)    True → False (archived_now)
[arc6-archive] intro_fee price   price_1TXNmnRytQVRVXw7GGfyJiaj active=True (control OK)
[arc6-archive] intro_fee product prod_UWQeeXJ3CwJQYu (Luciel Pilot Intro Fee) active=True (control OK)
```

**Completed at:** `2026-05-23T22:57:50+00:00` UTC (single ~3-second window across the 9 mutations).

---

## 6. Poststate evidence (full JSON)

File: `arc6-out/C-arc6-stripe-poststate.json` (94 lines)

Summary table:

| Object | Type | Pre `active` | Post `active` | Action |
|---|---|---|---|---|
| `price_1TX3MGRytQVRVXw7TPKQiCAS` (individual_monthly) | Price | true | **false** | archived_now |
| `price_1TX3MGRytQVRVXw7408HJhsG` (individual_annual) | Price | true | **false** | archived_now |
| `price_1TX3MHRytQVRVXw7LazGyxZH` (team_monthly) | Price | true | **false** | archived_now |
| `price_1TX3MHRytQVRVXw7pVbzGSX0` (team_annual) | Price | true | **false** | archived_now |
| `price_1TX3MIRytQVRVXw79cYZwjVw` (company_monthly) | Price | true | **false** | archived_now |
| `price_1TX3MIRytQVRVXw7b7qXMaX1` (company_annual) | Price | true | **false** | archived_now |
| `prod_UW5XLqvK0A2PKl` (Luciel Individual) | Product | true | **false** | archived_now |
| `prod_UW5XMLeRBpgCJH` (Luciel Team) | Product | true | **false** | archived_now |
| `prod_UW5XobSmV02qQp` (Luciel Company) | Product | true | **false** | archived_now |
| `price_1TXNmnRytQVRVXw7GGfyJiaj` (intro fee) | Price | true | true | **control held** |
| `prod_UWQeeXJ3CwJQYu` (Luciel Pilot Intro Fee) | Product | true | true | **control held** |

9 archive mutations performed; 0 failed; 0 unintended side effects on the control object.

---

## 7. Re-runnability

Re-executing `scripts/_arc6_stripe_archive_v1.py` against the current Live state would produce:

- Sanity check: still passes (the 6 Price → 3 Product mapping is preserved even after archival; archive is a soft flag, not a delete).
- All 9 mutation rows: `action="already_archived"`, `pre_active=false`, `post_active=false`.
- Control: still `active=True`.

This means the destructive sub-step can be safely re-run for evidence regeneration without further state change. Important for any future audit replay.

---

## 8. What this does NOT touch

To be explicit about scope so future arcs do not assume anything was wiped that wasn't:

- **10 canceled `Subscription` objects:** Stripe does not allow deletion of past Subscriptions in Live mode. They remain in the account as historical records (`status=canceled`, all from `aryans.www+*@gmail.com` test customers). They do not bill, do not appear on the dashboard's "active" view, and do not affect V2 mint.
- **13 `Checkout.Session` objects:** Stripe Checkout Sessions are immutable terminal records. No archive/delete API exists. They age out via Stripe's standard retention.
- **10 `Customer` objects:** intentionally retained for audit symmetry with the Subscription history. Can be archived later if needed; not required for V2 mint.
- **Invoices, balance transactions, charges, payment intents:** untouched. Read-only historical record.

This is consistent with Stripe's product model: archival is the strongest destructive action available for SKU-level cleanup in Live mode.

---

## 9. Linkage to the rest of Arc 6

- **Unblocks Commit 4:** the V2 mint (Pro monthly $349 / Pro annual $2,990 / Enterprise floor $24,000 + retained $100 intro fee) can now happen against a clean SKU surface — no name collisions with the V1 SKUs and no risk of a stale V1 Price being picked up by a misconfigured client.
- **Frozen state:** V1 Price IDs are now permanently archived; SSM params under `/luciel/production/stripe_price_*` will be overwritten in Commit 4 to point to the new V2 Price IDs. The current SSM values (pointing to archived V1 prices) are preserved in `arc6-out/C-arc6-stripe-prestate.json` for audit trail.
- **Numeric lock held:** CANONICAL §11.7 + §14 still pin $349 / $2,990 / $24,000 / $100 (no change this commit).

---

## 10. Doctrine cross-references

- **AGGRESSIVE-CLEANUP:** satisfied — V1 SKUs cannot become a `ship-out-vs-vision` drift, since they are now non-selectable in Stripe.
- **Path A:** satisfied — fixed the underlying SKU shape rather than working around it.
- **Pause-between-commits:** this destructive-half is the second half of Commit 2 and requires a separate push approval before moving to Commit 3.
- **5-gate scope expansion:** `LucielSandboxStripeScope` was created and attached *before* any Stripe-touching action. Probe param `/luciel/production/stripe_arc6_iam_probe` is left in place until Commit 11 cleanup.
- **Prod-credential posture:** AWS creds exported via env only (never to disk, never echoed). Stripe `sk_live_` key never touched disk (loaded from SSM into env, only printed prefix `sk_live_...` for confirmation, never the value).
- **Due-diligence directive:** all execution inline, no subagents.

---

## 11. Files in this commit

- `arc6-out/C-arc6-stripe-prestate.json` — prestate evidence (already on disk from 22:50 UTC enum)
- `arc6-out/C-arc6-stripe-poststate.json` — poststate evidence (just captured)
- `arc6-out/C-arc6-stripe-wipe-record.md` — this record
- `scripts/_arc6_stripe_enumerate_prestate.py` — read-only enumerator (re-runnable)
- `scripts/_arc6_stripe_archive_v1.py` — archive script (idempotent, re-runnable for evidence regen)
