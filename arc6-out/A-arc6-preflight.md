# Arc 6 Preflight — Stripe SKU restructure

**Date:** 2026-05-23
**Predecessor:** `arc-5-tenancy-collapse-complete` (tag at `2c4b014`)
**Scope:** Retire the 4-tier (Solo/Team/Company × monthly/annual) Stripe SKU set live in prod since Step 30a.1; replace with the 3-tier Free/Pro/Enterprise shape from CANONICAL §11.7 + §14; land the `admin_widget_domains` table (the new Axis 5 sub-row deferred from Arc 5); land the Free-tier CAPTCHA gate (P1 prerequisite).

---

## §1 — Why now

The Arc 5 schema collapse (Commits 22–27) landed `subscriptions.billing_model VARCHAR(16) NULL`, `admin_tier_overrides`, and `metering_emissions` in prod RDS, and retired the four-level V1 hierarchy in favour of `Admin → Instance → Lead`. The application runtime is serving traffic against the V2 schema with the V2 ORM models, but the **Stripe surface still speaks the V1 tier vocabulary** (`TIER_INDIVIDUAL` / `TIER_TEAM` / `TIER_COMPANY` + six live Prices). The `subscriptions.billing_model` column is populated with `'flat'` for every Pro row but the value is never read because no `billing_model='hybrid'` Enterprise row exists yet. The marketing site `Pricing.tsx` still shows the 4-card grid.

Arc 6 closes this gap end-to-end so that the Stripe surface, the marketing surface, the backend service layer, the entitlement enforcement, and the canonical doctrine all read the same tier vocabulary and the same numeric values.

## §2 — Scope (commits in order)

| # | Commit | Surface |
|---|---|---|
| 1 | Preflight + numeric lock | docs (this file + B-arc6-numeric-lock.md + CANONICAL §11.7/§14) |
| 2 | Wipe internal Stripe subs + archive old Prices | Stripe Live + audit record |
| 3 | `admin_widget_domains` table (Arc 6 Revision A) | Alembic + ORM + prod RDS |
| 4 | Mint new Stripe Prices + SSM puts + config | Stripe Live + SSM + `app/core/config.py` |
| 5 | Backend Stripe-surface rewrite | `BillingService` / `TierProvisioningService` / `SubscriptionService` |
| 6 | Webhook noun rename Tenant→Admin | `billing_webhook_service.py` + Stripe metadata |
| 7 | Marketing site Pricing.tsx | Luciel-Website 3-card grid + §14 matrix |
| 8 | Free-signup route + page | `POST /api/v1/billing/signup-free` + `SignupFree.tsx` |
| 9 | Free-tier CAPTCHA gate | hCaptcha + one-per-email-and-IP soft-gate |
| 10 | Buildah + ECR push + ECS rolling deploy | Image `arc6-<sha>`, backend:86 / worker:40 |
| 11 | Doctrine close + tag | CANONICAL §17 / ARCHITECTURE / DRIFTS + `arc-6-stripe-restructure-complete` |

## §3 — Numeric decisions (full reasoning in `B-arc6-numeric-lock.md`)

- **Pro monthly:** **$349 CAD**
- **Pro annual:** **$2,990 CAD** (~28% off monthly equivalent; deviates from §14 doctrine line "annual = 10× monthly" — annotated as deliberate revision)
- **Enterprise floor (published):** **$24,000 CAD/year** (platform-fee component; metered usage on top)
- **Intro fee:** **$100 CAD one-time** on Pro signup (retained from Step 30a.2-pilot)
- **Free:** **$0**, no Stripe rows, CAPTCHA + one-per-email-and-IP gate

Partner explicitly delegated numeric judgment to the agent: *"Partner given that you our full vision now and scope I will leave the decision of the numbers to you. Now we have changed a lot of things I will enturst you with this judgment. You know what is the best for our business"* (2026-05-23 6:06 PM EDT).

## §4 — Existing-customer migration: clean slate

Partner directive 2026-05-23 6:06 PM EDT: *"All 23 are internal/test — wipe and start clean"*. Commit 2 cancels all 23 live Stripe subscriptions with `proration_behavior='none'` (no refund — these are internal test accounts), archives the 6 old Prices (Individual/Team/Company × monthly/annual), and records every customer_id + subscription_id + cancellation timestamp in `C-arc6-stripe-wipe-record.md`.

## §5 — Hard prerequisites landing inside Arc 6

- **Free-tier CAPTCHA gate** (`D-free-tier-captcha-missing-2026-05-22`, P1) — Free has API enabled at 30 rpm + 1 embed key per the §14 Option A revision; programmatic load is now part of the abuse surface, captcha + soft-gate is a hard prerequisite to Free launch. Lands at Commit 9.

## §6 — Out-of-scope (deferred to Arc 8)

- **Pro per-key + per-instance rate buckets** (`D-pro-tier-rate-limit-abuse-surface-2026-05-23`, P1) — the multiplicative composition (300 rpm × 25 seats × 10 instances × 10 keys) at Pro scale needs per-key and per-instance rate limiting in addition to per-admin. Stays OPEN at Arc 6 close, scheduled for Arc 8 WU-7.
- **Enterprise metered-usage emission implementation** — the `metering_emissions` cursor table is in prod (Arc 5 Revision A) and the `enterprise_metered_usage_CAD` Stripe Price will be minted at Commit 4, but the actual periodic emission of usage records to Stripe via `stripe.SubscriptionItem.create_usage_record` is Arc 8 WU-8 work. Arc 6 lands the Price + the column wiring; Arc 8 lands the emitter.
- **Pro→Enterprise self-serve upgrade** — Enterprise stays talk-to-sales at Arc 6. The Customer Portal upgrade flow Pro→Enterprise is roadmap.

## §7 — Five-pillar mapping for the arc

- **Maintainability:** Single tier vocabulary across schema, code, Stripe, and marketing site. No more dual-language bookkeeping.
- **Scalability:** New revenue ceiling via Enterprise hybrid (platform fee + metered). Pro 10×/2.6× larger than Solo on Instances/Leads.
- **Reliability:** Boot-safe Price ID resolution preserved (empty Price slot → 501 on `/billing/checkout`, runtime continues to boot). CAPTCHA fail-closed.
- **Traceability:** Stripe wipe audit, every commit a §17 entry, doctrine close at Commit 11.
- **Security:** Free-tier CAPTCHA gate closes a P1 abuse surface; no new IAM scope expansion (`LucielSandboxArc5EcsRollingDeploy` + `LucielSandboxArc5MigrateScope` cover the entire Arc 6 surface); Stripe Live ops run via the partner's Stripe Dashboard credential path (already in SSM since Step 30a.2-pilot GATE 3, 2026-05-15).

## §8 — Pause-between-commits discipline

Same as Arc 5 Path A. After each commit I pause for partner approval before pushing, except for the mechanical Stripe-mint + SSM-put sequence at Commit 4 which runs inline once approved as a group, and the buildah + push + register + deploy sequence at Commit 10 which runs inline once approved as a group.

## §9 — Tag

`arc-6-stripe-restructure-complete` at Commit 11 doctrine close.
