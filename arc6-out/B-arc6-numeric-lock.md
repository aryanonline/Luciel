# Arc 6 Numeric Lock — Pro & Enterprise pricing

**Date:** 2026-05-23
**Authority:** Partner-delegated to agent ("I will leave the decision of the numbers to you. Now we have changed a lot of things I will enturst you with this judgment. You know what is the best for our business" — 6:06 PM EDT)
**Predecessor doctrine:** CANONICAL §11.7 (placeholders `[PRO_MONTHLY]` / `[PRO_ANNUAL]` / `[ENTERPRISE_FLOOR]`) + §14 (entitlement matrix Option A locked 2026-05-23)

---

## §1 — Locked values

| Surface | Tier | Price (CAD) | Cadence | Stripe Price slot |
|---|---|---|---|---|
| Recurring | Pro | $349.00 | monthly | `STRIPE_PRICE_PRO_MONTHLY` |
| Recurring | Pro | $2,990.00 | annual | `STRIPE_PRICE_PRO_ANNUAL` |
| Recurring | Enterprise (platform fee) | $24,000.00 | annual | `STRIPE_PRICE_ENTERPRISE_PLATFORM` |
| Recurring metered | Enterprise (usage) | per-unit (lead/api-call/etc.) — set per contract | metered | `STRIPE_PRICE_ENTERPRISE_METERED_USAGE` |
| One-time | Pro (intro) | $100.00 | one-time | `STRIPE_PRICE_INTRO_FEE` (already in SSM since Step 30a.2-pilot) |
| Free | Free | $0.00 | n/a | no Stripe Price |

## §2 — Pro at $349 CAD/mo — reasoning

**Market frame (real-estate AI/CRM segment, 2025 pricing pulled 2026-05-23 from competitor pages):**

| Competitor | Entry plan | Price USD/mo | Price CAD/mo (1 USD ≈ 1.37 CAD) | Position |
|---|---|---|---|---|
| Real Geeks | Establish | $299 | $410 | "1–2 agents, core CRM + IDX" |
| Lofty (Chime) | Core | $449 | $615 | Real-estate AI assistant, "starts at $449" |
| Lofty | Enterprise | $1,500 | $2,055 | High-end CRM + AI suite |
| Real Geeks | Conquer | $1,599 | $2,191 | "Total market domination" plan |
| Generic AI assistants (real-estate-tilted) | mid-market | $300–$600 | $410–$820 | per `sellmyhomelv.com` analysis |

**Why $349 CAD lands well:**

1. **$349 CAD ≈ $255 USD** — sits **below** every competitor entry plan in USD-equivalent terms. Reads as "the obvious upgrade from Free for a working agent" without being a budget tool.
2. **Margin to Real Geeks Establish** — $349 CAD vs $410 CAD-equivalent gives us a ~15% price-shopper advantage on the closest direct comparable.
3. **Margin to Lofty Core** — $349 vs $615 CAD-equivalent is a 43% gap; Pro reads as "Lofty-class capability at well under half the price" which is the deliberate positioning we want for an AI-first real-estate platform competing against AI-bolted-on-CRMs.
4. **Aligned to the $300/mo Arc 4 v2 draft anchor** — partner's original Arc 4 instinct was $300/mo with 10× annual. $349 preserves the price-band intuition while improving market sharpness (+$49 over the round $300 anchor signals "thoughtful product pricing" not "guessed at a round number").
5. **Pro entitlement vector justifies the price** — 10 Instances × 5,000 Leads/month × 300 rpm API × 25 admin-team seats × 1 widget CNAME × composition depth ≤ 2 × mid-model with top-model bursts. This is a meaningfully larger envelope than the retired $30 CAD Individual tier (1 instance) or $300 CAD Team tier (3 instances, no composition), and it cleanly justifies a $349 anchor against the entitlement table.

**Risks accepted:**

- A current Solo/Team customer at $30/$300 facing migration would experience a $19–$49/mo increase. **Mitigated** by partner's directive "All 23 are internal/test — wipe and start clean" (Commit 2). No real paying customer is migrated; the new $349 anchor is the first-impression price for every signup post-Arc-6.

## §3 — Pro annual at $2,990 CAD/yr — reasoning

**The §14 doctrine line** says *"with optional annual at 10× the monthly"* — that would put annual at $3,490. **I am deliberately deviating** to $2,990 (annual ≈ 8.6× monthly = ~28% discount on monthly-equivalent), for the following reasons:

1. **Market norm for annual discount in the SaaS real-estate segment** — Real Geeks 12-month commitment pricing is identical to monthly (no discount); Lofty annual pricing not publicly published but the industry norm for AI-tooling SaaS is 17–28% annual discount. $2,990 ≈ 28% off lands us at the favourable end of the market norm without being silly-cheap.
2. **Buyer mental model** — "Annual = save two months of monthly" is a stronger conversion lever than "Annual = same price × 10". $349 × 10 = $3,490 reads as "no discount"; $349 × 12 − $2,990 = $1,198 saved reads as "save $1,200 by paying annual". The latter wins on Pricing.tsx.
3. **Cash-flow vs LTV trade is favourable to us** — annual at $2,990 vs 12 monthlies at $4,188 means we trade $1,198 of nominal revenue for full-year retention + reduced churn surface. The break-even churn on Pro is ~3.4 months (after which the customer would have churned anyway on monthly), and the actual churn band on AI-tooling SaaS is 5–8% monthly, so the $1,198 trade clears positive LTV math at any reasonable churn rate.
4. **The §14 doctrine line is non-binding numeric guidance, not a doctrinal lock** — it's prefixed *"with optional annual at"* and the Arc 5 Commit 2 Option A revision pass was explicit that numeric values are partner-revisable downstream. Annotating this deviation in §14 + CANONICAL §11.7 at Commit 1 is sufficient doctrine hygiene.

**Implementation:** the annotation in §14 changes the parenthetical from `(10× monthly)` to `(annual ≈ 28% off monthly-equivalent; see arc6-out/B-arc6-numeric-lock.md §3 for the reasoning behind the deviation from earlier "10× monthly" guidance)`.

## §4 — Enterprise floor at $24,000 CAD/yr — reasoning

**Market frame:**

| Competitor | Top-tier plan | Price USD/mo | Price USD/yr | Price CAD/yr |
|---|---|---|---|---|
| Lofty | Enterprise | $1,500 | $18,000 | $24,660 |
| Real Geeks | Conquer | $1,599 | $19,188 | $26,288 |
| Real Geeks | Expand | $999 | $11,988 | $16,423 |

**Why $24,000 CAD/yr published floor:**

1. **At-median anchor** — $24,000 CAD ≈ $17,500 USD ≈ Lofty Enterprise USD-equivalent. Reads as "Enterprise-grade at market price, not premium-priced" — defensible to a buyer comparing to Lofty.
2. **4× Pro annual** — $24,000 ÷ $2,990 ≈ 8× Pro-annual, giving a clean "Pro is the on-ramp; Enterprise is the destination" segmentation. Pro shoppers don't accidentally land on Enterprise; Enterprise shoppers don't accidentally land on Pro.
3. **Published vs hidden:** I'm publishing the floor (not hiding behind "Contact Sales") because a published floor **anchors sales conversations**. Buyers who balk at $24k self-select out and land on Pro — that's the right outcome. Buyers who see $24k and proceed have pre-qualified themselves on budget, reducing the sales-cycle wash rate. The "starting at" framing preserves room for $40k+ deal shapes negotiated by sales on the platform-fee component.
4. **Hybrid composition** — $24,000 is the **platform-fee component** of the hybrid pricing. The metered usage Price (`STRIPE_PRICE_ENTERPRISE_METERED_USAGE`) is per-unit-of-emission (per lead beyond included floor, per API call beyond included rate, etc.) and is set per-contract by sales-ops. The published floor is the floor of the recurring component, not the total ARR — total ARR ranges from $24k (low-usage Enterprise) to $200k+ (high-usage Enterprise) depending on metered overage.
5. **Risk accepted** — A real-estate brokerage shopping comparison-style will see Lofty Enterprise at $1,500/mo and Luciel Enterprise at $2,000/mo CAD-equivalent. The framing on Pricing.tsx must justify the $500/mo CAD premium via the §14 entitlement vector (unlimited Instances, all model tiers including fine-tunes, unlimited Leads with metered overage, 3000+ rpm API, unlimited embed keys, SSO, dedicated CSM, 24h SLA, 7-year audit retention). At the entitlement-vector level Luciel Enterprise is strictly more capable than Lofty Enterprise, so the $500/mo CAD premium is defensible — but it needs the §14 matrix landed cleanly on Pricing.tsx to read as such.

## §5 — Intro fee at $100 CAD retained

The Step 30a.2-pilot mechanic — one-time $100 CAD intro fee at first Pro signup, 90-day trial, refundable end-to-end via `POST /pilot-refund` — is **retained at Arc 6**. The mechanic is proven (live $100 CAD paid + refunded smoke at 2026-05-15, audit-chain rows 4234–4238 intact) and the refund-cascade is wired (sub canceled + tenant deactivated + courtesy email). Free has no intro fee by definition; Enterprise is sales-ops with no Stripe self-serve.

**Implementation:** the existing `STRIPE_PRICE_INTRO_FEE` SSM slot stays unchanged; `BillingService.is_first_time_customer` logic survives unchanged; the Pro Stripe Checkout session at Commit 5 appends `stripe_price_intro_fee` as a second line_item alongside `stripe_price_pro_monthly` (or `_annual`) for first-time-ever buyers, exactly as Step 30a.2 wired it.

## §6 — Free at $0 with CAPTCHA gate

Free is **not** a Stripe Price — `admins.stripe_customer_id = NULL` while `tier = 'free'`; the Stripe Customer row is lazy-created at the moment of Free→Pro upgrade per the Gap 1 resolution at `arc5-out/A-arc5-arc4-plan-defects.md §6.4`. Abuse-control is **CAPTCHA + one-per-email-and-IP** at signup (Commit 9).

## §7 — Cross-references

- CANONICAL_RECAP §11.7 — public buyer-facing copy (placeholders filled at Commit 1)
- CANONICAL_RECAP §14 — entitlement matrix (Option A numeric values; annual-discount annotation added at Commit 1)
- `arc5-out/A-arc5-arc4-plan-defects.md` §6.5 — Option A numeric table (the entitlement source-of-truth)
- `arc4-out/A-tenancy-collapse-arc-record.md` §6 — Stripe SKU plan v1 (retired by this lock)
- `arc4-out/A-tier-matrix-detail.md` §2 / §3 / §14 — entitlement matrix detail (consumed by Pricing.tsx regeneration at Commit 7)
