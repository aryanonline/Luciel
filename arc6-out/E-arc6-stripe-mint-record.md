# Arc 6 Commit 4 — Stripe V2 SKU mint + SSM puts + config.py rewrite

**Captured:** 2026-05-23 (mint at 23:16-23:18 UTC, regen at 23:22 UTC, audit assembled 23:27 UTC EDT-evening session)
**Stripe account:** `acct_1TX2BmRytQVRVXw7` (Luciel / VantageMind LIVE mode)
**AWS account:** `729005488042` (Luciel sandbox-agent)
**Actor:** `arn:aws:iam::729005488042:user/luciel-sandbox-agent`
**Path:** A (aggressive cleanup, V1 fully retired)
**Partner directive carried:** "Let us do Path A I want to fix everything if something breaks with fix with our vision in mind."

---

## 1. Scope of this commit

Replace the V1 Stripe SKU surface (Individual / Team / Company × Monthly + Annual = 6 Prices on 3 Products, all archived in Commit 2) with the Arc 6 V2 hybrid surface locked in CANONICAL §11.7 / §14:

| Tier        | Billing model        | Annual price (CAD)  | Monthly price (CAD) | Stripe Price slot                          |
|-------------|----------------------|---------------------|---------------------|--------------------------------------------|
| Free        | $0, CAPTCHA-gated    | —                   | —                   | (no Stripe row)                            |
| Pro         | flat-rate self-serve | $2,990 / yr         | $349 / mo           | `stripe_price_pro_monthly`, `stripe_price_pro_annual` |
| Enterprise  | hybrid flat + metered| $24,000 / yr floor + metered overage | (annual only) | `stripe_price_enterprise_floor_annual` (+ `stripe_price_enterprise_metered_unit` **DEFERRED**) |

Intro fee ($100 CAD one-time, retained from V1 as control) remains at `stripe_price_intro_fee` = `price_1TXNmnRytQVRVXw7GGfyJiaj` on Product `prod_UWQeeXJ3CwJQYu` (verified active both pre- and post-mint).

---

## 2. Numeric lock (CANONICAL §11.7, immutable across Arc 6)

- Pro monthly: **$349 CAD** (34,900 cents, currency=cad, interval=month, count=1)
- Pro annual: **$2,990 CAD** (299,000 cents, currency=cad, interval=year, count=1) — ~28.6% discount vs $349×12=$4,188
- Enterprise floor: **$24,000 CAD/yr** (2,400,000 cents, currency=cad, interval=year, count=1)
- Intro fee (control): **$100 CAD one-time** (10,000 cents, type=one_time)

All amounts verified post-mint via `stripe.Price.retrieve` against minted IDs — see §6 below.

---

## 3. Pre-mint control assertion

Before mutating Stripe, the mint script asserted the intro fee surface was still healthy (catches the case where Commit 2's archive sweep accidentally touched the wrong Product):

```
intro_price = stripe.Price.retrieve("price_1TXNmnRytQVRVXw7GGfyJiaj")
intro_product = stripe.Product.retrieve(intro_price.product)
assert intro_price.active and intro_product.active, "intro fee control failed PRE-mint"
```

**Result:** PASSED. `price_1TXNmnRytQVRVXw7GGfyJiaj.active=True`, `prod_UWQeeXJ3CwJQYu.active=True`, name="Luciel Pilot Intro Fee".

---

## 4. Mutations executed

All mutations used **idempotency keys** so the script is safely re-runnable (a re-run would return the existing object, not create a duplicate).

### 4.1 Products (2 created)

| Slot                | Stripe Product ID         | Idempotency key                                       |
|---------------------|---------------------------|-------------------------------------------------------|
| luciel_pro          | `prod_UZXsUqCuumvw1v`     | `arc6-mint-v2-product-luciel-pro-2026-05-23`          |
| luciel_enterprise   | `prod_UZXsY0kLJsFfop`     | `arc6-mint-v2-product-luciel-enterprise-2026-05-23`   |

Both products created with metadata: `arc=arc6`, `vocabulary=v2`, `created_at_utc=2026-05-23T23:16:27+00:00`, plus per-product `tier` and `billing_model` (see §6).

### 4.2 Prices (3 created)

| Slot                       | Stripe Price ID                          | Cents     | Interval        | Idempotency key                                              |
|----------------------------|------------------------------------------|-----------|-----------------|--------------------------------------------------------------|
| pro_monthly                | `price_1TaOmORytQVRVXw77yRoEC8m`         | 34,900    | month × 1       | `arc6-mint-v2-price-pro-monthly-2026-05-23`                  |
| pro_annual                 | `price_1TaOmORytQVRVXw7ElbQotvK`         | 299,000   | year × 1        | `arc6-mint-v2-price-pro-annual-2026-05-23`                   |
| enterprise_floor_annual    | `price_1TaOmPRytQVRVXw7ozfKMFps`         | 2,400,000 | year × 1        | `arc6-mint-v2-price-enterprise-floor-annual-2026-05-23`      |

All Prices: `currency=cad`, `type=recurring`, `usage_type=licensed`, `active=true`, metadata `arc=arc6`, `vocabulary=v2`, plus per-Price `tier` and `cadence`.

### 4.3 SSM SecureString puts (3 written)

| Parameter name (region ca-central-1)                                | Value (full Price ID)                  | Version | Type         | Last-modified UTC                  |
|---------------------------------------------------------------------|----------------------------------------|---------|--------------|------------------------------------|
| `/luciel/production/stripe_price_pro_monthly`                       | `price_1TaOmORytQVRVXw77yRoEC8m`       | 1       | SecureString | 2026-05-23T23:16:29.514+00:00      |
| `/luciel/production/stripe_price_pro_annual`                        | `price_1TaOmORytQVRVXw7ElbQotvK`       | 1       | SecureString | 2026-05-23T23:16:29.651+00:00      |
| `/luciel/production/stripe_price_enterprise_floor_annual`           | `price_1TaOmPRytQVRVXw7ozfKMFps`       | 1       | SecureString | 2026-05-23T23:16:29.777+00:00      |

All three SSM writes verified by `GetParameter` (read-back) in the post-state regen — no `DescribeParameters` was used (preserves `LucielSandboxStripeScope` minimality per session doctrine finding #3).

---

## 5. Post-mint control assertion

After all mutations, the mint script re-asserted the intro fee surface was still healthy (catches any accidental Product overwrite or Price deactivation as a side-effect of the V2 mint):

**Result:** PASSED. Intro fee Price + Product both still `active=true`, name unchanged.

---

## 6. Post-state evidence

Full machine-readable evidence captured at `arc6-out/E-arc6-stripe-mint-poststate.json`. Summary:

**Products:**
- `prod_UZXsUqCuumvw1v` "Luciel Pro" — `tier=pro`, `billing_model=flat`, `arc=arc6`, `vocabulary=v2`, `active=true`
- `prod_UZXsY0kLJsFfop` "Luciel Enterprise" — `tier=enterprise`, `billing_model=hybrid`, `arc=arc6`, `vocabulary=v2`, `active=true`

**Prices:**
- `price_1TaOmORytQVRVXw77yRoEC8m` "Pro monthly (Arc 6 V2)" — `tier=pro`, `cadence=monthly`, $349 CAD/month, recurring/licensed, active
- `price_1TaOmORytQVRVXw7ElbQotvK` "Pro annual (Arc 6 V2)" — `tier=pro`, `cadence=annual`, $2,990 CAD/year, recurring/licensed, active
- `price_1TaOmPRytQVRVXw7ozfKMFps` "Enterprise floor annual (Arc 6 V2)" — `tier=enterprise`, `cadence=annual`, $24,000 CAD/year, recurring/licensed, active

**Intro fee control:** `price_1TXNmnRytQVRVXw7GGfyJiaj` on `prod_UWQeeXJ3CwJQYu` "Luciel Pilot Intro Fee" — both active, unchanged.

**SSM:** 3 SecureString params at version 1 with values matching minted Price IDs exactly.

---

## 7. Mint script JSON-serialization crash + remediation

The mint script (`scripts/_arc6_stripe_mint_v2.py`, 290 lines) **succeeded on all 8 mutations** (2 Products + 3 Prices + 3 SSM puts + 2 control assertions) but crashed during the final post-state JSON write because Stripe SDK 15.1.0 metadata access semantics differ from earlier SDKs:

- **What we wrote:** `pro_product.metadata.get("tier")` (legacy pattern)
- **What SDK 15.1.0 does:** `metadata` is a `StripeObject`, not a `dict`. `.get(...)` raises `AttributeError`. `dict(metadata)` raises `KeyError: 0` because `StripeObject.__iter__` is positional, not dict-keyed.
- **Correct pattern:** `getattr(stripe_obj.metadata, "tier", None)` — attribute access on `StripeObject` resolves through the underlying JSON.

**Remediation:** Authored `scripts/_arc6_stripe_mint_poststate_regen.py` (129 lines, read-only against Stripe + SSM, hardcoded IDs from the mint script's stderr log). Initial regen had the same `dict().get()` bug, which yielded `null` for all metadata fields and was caught by a sanity-print before paperwork was finalized. Second regen used the correct `getattr` accessor and produced clean evidence. This is now the third Stripe-SDK-15.1.0 gotcha recorded in CANONICAL §17:

1. `dict(stripe_obj)` raises KeyError
2. `.to_dict_recursive()` was renamed to `_to_dict_recursive()` (private)
3. `stripe_obj.metadata.get(key)` raises AttributeError; `dict(metadata)` raises `KeyError: 0`; correct accessor is `getattr(stripe_obj.metadata, key, default)`

No Stripe state was harmed by the crash. Idempotency keys mean the mint script is still safe to re-run (it would return the existing objects, not create duplicates).

---

## 8. Enterprise metered Price — deferred per partner direction

The Enterprise tier is locked in CANONICAL §11.7 as **hybrid (flat floor + metered overage)**. The flat floor is live in Stripe as of this commit. The metered Price was **intentionally not minted** in Commit 4 per partner direction on 2026-05-23 at 7:14 PM EDT:

> "Let's not the metered part right now partner we can do this we can notice our business booming"

Until a metered Price is minted:

- Enterprise Checkout sessions use floor-only billing (`stripe_price_enterprise_floor_annual` as the sole recurring line item).
- Any per-unit overage is invoiced manually outside Stripe by ops.
- `app/core/config.py` intentionally omits the `stripe_price_enterprise_metered_unit` slot (no empty placeholder field) so a future code path cannot accidentally create a malformed Checkout with `price_id=""`.
- The deferral is recorded in three places: `config.py` Enterprise comment block, this audit record, and CANONICAL §17 Commit 4 entry.

When the partner says "go" on metered, the work to lift this deferral is:

1. `stripe.Price.create(...)` with `recurring={interval:"month", usage_type:"metered", aggregate_usage:"sum"}` + `transform_quantity` for tiered pricing (or `tiers_mode="graduated"` if we want graduated tiers)
2. `ssm:PutParameter /luciel/production/stripe_price_enterprise_metered_unit` (already in scope of `LucielSandboxStripeScope`)
3. Add `stripe_price_enterprise_metered_unit: str = ""` slot to `app/core/config.py` Enterprise comment block
4. Extend `BillingService.create_checkout_session` to append the metered Price as a second line item on Enterprise Checkouts
5. Wire `stripe.SubscriptionItem.create_usage_record` into the per-unit emitter (TBD: lead-qualification or doc-ingest as the chargeable unit — CANONICAL §14 lists candidates but doesn't pick)

---

## 9. CANONICAL §14 entitlement matrix — touch point

CANONICAL §14 currently shows the Enterprise metered Price as "TBD". This audit record references it as "DEFERRED (per partner direction 2026-05-23)" rather than "TBD", which is a more accurate state. The CANONICAL §14 surface itself will be regenerated when Commit 7 (marketing site Pricing.tsx 3-card grid + §14 entitlement matrix regenerated) lands — not this commit. Fold-forward is captured in CANONICAL §17 Commit 4 entry so the regenerator doesn't reintroduce "TBD".

---

## 10. config.py rewrite (V1 → V2)

`app/core/config.py` lines 146-225 fully rewritten:

**Removed (V1 field declarations):**
- `stripe_price_individual: str = ""`
- `stripe_price_individual_annual: str = ""`
- `stripe_price_team_monthly: str = ""`
- `stripe_price_team_annual: str = ""`
- `stripe_price_company_monthly: str = ""`
- `stripe_price_company_annual: str = ""`

**Added (V2 field declarations):**
- `stripe_price_pro_monthly: str = ""`
- `stripe_price_pro_annual: str = ""`
- `stripe_price_enterprise_floor_annual: str = ""`

**Unchanged:**
- `stripe_secret_key: str = ""`
- `stripe_publishable_key: str = ""`
- `stripe_webhook_secret: str = ""`
- `stripe_price_intro_fee: str = ""` (slot retained, Stripe Price ID retained — same `price_1TXNmnRytQVRVXw7GGfyJiaj`)

**Header comment block** fully rewritten:
- Replaced "Step 30a" framing with "Arc 6 V2 SKU surface"
- Added the Free / Pro / Enterprise tier topology table with CANONICAL §11.7 / §14 cross-reference
- Documented the Enterprise metered deferral with the partner-direction quote inline
- Documented the V1 deprecation note pointing to Arc 6 Commits 2 + 4 + 6
- Preserved the boot-safe pattern (§3.2.9) language verbatim — backend still boots with empty Stripe config and routes return 501, never 500

**Call-site refactor deferred to Commit 5.** Five other Python files still reference V1 field names (`billing_service.py`, `billing_webhook_service.py`, `scripts/stripe_create_live_prices.py`, `tests/api/test_step30a_2_first_time_gate.py`). They will be rewritten in Commit 5 (BillingService + TierProvisioningService + SubscriptionService → V2 vocab). This commit does not touch them — config.py is the single-file V2 alignment surface for Commit 4.

**Boot-safety verification at this commit:** `app/core/config.py` parses (`ast.parse` OK). The call sites that reference removed V1 fields will not break until they are reached — and they only fire on `/billing/*` routes. Since the next commit (5) is the call-site rewrite *before* the build/deploy commit (10), prod never sees a state where V2 SSM params exist but the running container references V1 attribute names. (Production is still running container `luciel-backend:85` which has both V1 config attrs and V1 SSM params intact via the existing TaskDef env injection — Commit 10 cuts over both halves atomically.)

---

## 11. Files touched in this commit

- `app/core/config.py` (-23 lines V1 fields/comments, +80 lines V2 fields/comments — net +57)
- `scripts/_arc6_stripe_mint_v2.py` (NEW, 290 lines, executed once, idempotent; retained for audit + future re-runs)
- `scripts/_arc6_stripe_mint_poststate_regen.py` (NEW, 129 lines, idempotent read-only)
- `arc6-out/E-arc6-stripe-mint-poststate.json` (NEW, machine-readable evidence)
- `arc6-out/E-arc6-stripe-mint-record.md` (NEW, this file)
- `docs/CANONICAL_RECAP.md` (+1 long §17 line — Commit 4 entry)

No prod schema, no IAM, no ECS, no buildah, no marketing-site changes in this commit.

---

## 12. Discipline checks (Path A doctrine)

- [x] **No subagent execution** — entire commit ran inline per due-diligence directive
- [x] **No creds on disk** — AWS env vars sourced from §4 paste protocol, unset after each shell, never written; Stripe secret loaded from SSM via `_arc6_load_stripe_env.sh` into shell env, never echoed
- [x] **Scope tightness preserved** — `LucielSandboxStripeScope` still 3 Sids, no `DescribeParameters`, post-state regen worked from known parameter names
- [x] **Idempotency keys on every Stripe mutation** — re-runs are safe
- [x] **Pre + post control assertion** — intro fee surface verified untouched both before and after V2 mint
- [x] **No production database touched** — only Stripe Live + SSM SecureString writes; admin_widget_domains migration still deferred to Commit 10b
- [x] **AST parse + grep sweep** — config.py parses, V1 names fully expunged from config.py
- [x] **Numeric lock honoured** — all three Prices match CANONICAL §11.7 to the cent

---

**Commit 4 status:** Stripe Live + SSM mutations LANDED; config.py rewrite LANDED; audit + CANONICAL §17 captured. Per Arc 6 doctrine (resume contract §Pause-between-commits), Commit 4 is one of the two no-pause mutation groups (the other being Commit 10's buildah+deploy) — partner pre-authorized the Stripe-Live cutover at session start ("Approve — start Commit 4 end-to-end"). Stage + commit + push proceeds without a separate push gate.

— end of record —
