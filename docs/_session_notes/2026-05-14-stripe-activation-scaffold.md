# Stripe Live-Mode Activation Scaffold — 2026-05-14 (v2 — Step 30a.2 refresh)

**Purpose.** A pre-filled document for the Stripe activation form. Drafted by your AI partner from session memory, codebase source-of-truth, and the user-background context. Fields with verifiable answers from system context are filled. Fields requiring private input from you are marked `<<<YOU>>>`. Fields requiring a business decision from you are marked `<<<DECIDE>>>`.

**v2 refresh (2026-05-14, post Step 30a.2):** trial copy refreshed to match the uniform 90-day paid-intro policy that shipped today (replaces per-tier 14/7/0d). Pricing table extended to seven entries (six recurring + one one-time intro fee). Cancellation copy verified truthful against the now-shipped retention worker.

**Workflow.** Read top to bottom. Fill `<<<YOU>>>` fields inline (your information, never leaves your machine — this file is in `_session_notes/` which we'll gitignore). Resolve `<<<DECIDE>>>` fields with me before opening the Stripe form. Then copy-paste into Stripe.

**Source-of-truth callouts.** Every structural fact below is sourced from a specific file. If you see something that looks wrong, the source citation tells you exactly where to push back.

---

## Section 1 — Business identity

This is the legal entity Stripe will pay out to. Canadian sole-prop on your personal identity (no business registered yet) — your name *is* the business name for tax/legal purposes.

| Field | Value | Source |
|---|---|---|
| Country of operation | Canada | user_background |
| Business type | **Individual / sole proprietorship** | per "Canadian sole-prop on personal name, no business registered yet" |
| Legal business name | Aryan Singh | user_background (`Name: Aryan Singh`) |
| Doing-business-as (DBA) | Luciel | working brand name |
| Industry | Software / SaaS | per product description |
| MCC (Merchant Category Code) | **7372 — Computer Programming, Data Processing** | most precise for a multi-vertical B2B subscription SaaS platform (5734 — Computer Software Stores — leans retail / boxed-software and is less aligned with the cross-vertical judgment-layer product). Stripe usually auto-suggests; if it offers a list, pick 7372. |
| Business website | `<<<DECIDE>>>` — see note below | — |

**Business-website note.** You have two candidate URLs:
- `https://api.vantagemind.ai` — the API base, but Stripe wants a customer-facing site, not an API endpoint
- `https://d1t84i96t71fsi.cloudfront.net/widget.js` — also not a customer-facing site

Stripe's underwriting team **will** click the URL. A direct CloudFront distribution URL or an `api.` subdomain often triggers manual review (looks like staging). Best options in order:

1. If `vantagemind.ai` (apex) has a real landing page that explains Luciel, what it does, the pricing tiers, and a contact path — use that.
2. If not, **register a quick placeholder page on vantagemind.ai before submitting** the form. Even a one-page site with product description + pricing + contact email is enough to clear underwriting. This is a real prerequisite, not a nice-to-have.
3. Worst case: use `https://api.vantagemind.ai` and expect manual review and probable "send us a customer-facing site" email back from Stripe.

**My recommendation:** answer this `<<<DECIDE>>>` before form-submit. Spending an extra hour on a single landing page on vantagemind.ai today saves a 24-hour Stripe-review-bounce loop tomorrow.

---

## Section 2 — Personal identity (sole-prop owner)

This is **you**, since you are the legal entity. All fields are private; fill inline, do not commit this file.

| Field | Value | Source |
|---|---|---|
| Full legal name | Aryan Singh | user_background |
| Date of birth | `<<<YOU>>>` | not in memory |
| Home address (street, city, postal code) | `<<<YOU>>>` — Markham, Ontario, Canada is in memory; need street + postal | user_background (city only) |
| Phone number | `<<<YOU>>>` | not in memory |
| Email | `aryans.www@gmail.com` | user_background |
| Social Insurance Number (SIN) | `<<<YOU>>>` | sensitive — required for Canadian payouts and CRA tax reporting (T4A) |

**SIN warning.** Type your SIN by hand into the Stripe form, do not paste from anywhere. Do not type your SIN into this scaffold either. Type it directly into Stripe at submit time. The scaffold's `<<<YOU>>>` marker is a reminder, not a value to fill.

**Address note.** Stripe will use your home address as the registered business address (correct for sole-prop). They send a verification postcard or ask for a utility bill / bank statement in your name at that address. Pick the address you can substantiate with a document if asked.

---

## Section 3 — Product / business description

These are the prose fields Stripe's underwriting team reads. They drive risk classification and review queue speed. Drafted to be specific, accurate, and risk-aligned with B2B SaaS norms.

### "Describe what your business sells / what your product does"

> Luciel is a B2B SaaS platform that delivers a domain-adaptive AI judgment layer for knowledge-work professionals and the teams and companies they belong to. The product is a single core intelligence with a fixed reasoning and trust-discipline layer (the "Soul") and a configurable knowledge / tools / workflow layer (the "Profession") that adapts the same intelligence to each customer's vertical. Customers deploy Luciel through three scope tiers — Individual (one professional), Team (small team / department), and Company (whole organization) — and interact through an embeddable chat widget and a programmatic API, with additional channels (SMS, email, voice) on the near-term roadmap. The first vertical we sell into is residential real estate in Canada, where Luciel helps licensed agents and small teams qualify leads, follow up on stale leads, and prepare for client conversations; legal, mortgage, and other professional-services verticals are on the published roadmap and reuse the same core platform. Pricing is subscription-based per scope tier with monthly and annual cadences.

### "Who are your customers?"

> B2B knowledge-work professionals and the teams / companies they work in. At launch (today), the active beachhead is licensed real estate agents and small real estate teams operating in Canada — vetted at signup via brokerage affiliation and provincial license number where applicable. The Team and Company tiers extend the same platform to multi-seat real estate teams and brokerages, and the platform is designed to expand into adjacent professional-services verticals (legal, mortgage, etc.) over the next 12 months as additional Profession layers ship. No consumer-facing transactions — Luciel is strictly B2B.

### "How do customers find you and sign up?"

> Direct outbound sales (founder-led) into the real-estate beachhead, referrals from existing customers, and inbound from the marketing website (www.vantagemind.ai). Signup is self-serve via Stripe Checkout with email verification — a customer picks a tier (Individual, Team, or Company) and a cadence (monthly or annual), pays the one-time intro fee, and starts their 90-day evaluation window. No anonymous purchases.

### "What is your refund / cancellation policy?"

> Customers can cancel their subscription at any time from the account portal. Cancellation takes effect at the end of the current billing period (the monthly or annual cadence the customer chose at checkout). After cancellation, customer access is revoked and the customer's tenant is deactivated across all scope-bearing layers (tenant, domains, agents, sessions, conversations, identity claims, messages, embed keys, scope assignments, Luciel instances). Customer data is retained in deactivated form for 90 days for audit and reactivation purposes, after which it is hard-purged by an automated retention worker that runs nightly at 08:00 UTC. Audit logs are retained beyond the purge for regulatory compliance. The one-time $100 CAD intro fee charged at signup is non-refundable. No prorated refunds for partial billing periods — this is standard for SaaS subscriptions and is disclosed at checkout.

> *Sourced honestly against today's code (post Step 30a.2, 2026-05-14): cancellation cascade now covers ten layers atomically (`AdminService.deactivate_tenant_with_cascade`); the retention worker (`run_retention_purge` on Celery beat at 08:00 UTC) hard-deletes tenants whose `deactivated_at` timestamp is older than 90 days, walking leaf-first through the FK graph. AdminAuditLog survives the purge by design. The wording above is now fully truthful against code — the prior "not yet hard-purged" caveat from v1 of this scaffold is closed.*

> *Decision point: do we offer any annual-subscription refund window (e.g. 14-day money-back guarantee on annual prepay), on top of the 90-day paid-intro trial that already exists? `<<<DECIDE>>>`. The 90-day trial gives the customer a long evaluation window where the recurring rate has not yet charged; an additional annual-refund window is a separate operational policy (handled via Stripe Dashboard refund), not a code change.*

### "Do you offer trials?"

> Yes — paid-intro trials at signup. First-time customers pay a one-time $100 CAD intro fee at checkout, then receive a uniform 90-day trial on the recurring subscription of any of the six (tier, cadence) pairs (Individual / Team / Company × monthly / annual). The trial converts to the recurring rate on day 91 unless the customer cancels. A payment method is required at checkout and is charged for the intro fee immediately; the recurring rate is not charged until the trial converts. Customers who cancel and later rejoin do not receive the intro offer again — the standard recurring rate applies from day one of any second subscription.

> *Source-of-truth: `app/services/billing_service.py` post Step 30a.2 (2026-05-14) — `INTRO_TRIAL_DAYS=90`, `BillingService.is_first_time_customer(email)` case-insensitive gate via `func.lower()`, Stripe Option A line-item assembly in `create_checkout_session` (first-timers get intro_fee Price + recurring Price w/ trial_period_days=90; rejoiners get recurring Price only, no trial, no intro). Replaces the v1 per-tier trial policy (14d Individual-monthly / 7d Team-Company monthly / 0d annual) that v1 of this scaffold described.*

> *No decision needed — the uniform 90d paid-intro policy is the activation-form copy. Whatever this scaffold says must match the trial copy on vantagemind.ai and at the checkout page.*

### "Estimated annual revenue / processing volume"

> First 12 months: **`<<<DECIDE>>>`** CAD. (My suggestion: pick a realistic number that aligns with your runway plan. Stripe doesn't penalize honest small numbers — they penalize big numbers that don't show up. If you project $50K CAD in year one but Stripe sees $200K coming through, they trigger a manual review and possibly a reserve. Better to under-promise.)

### "Average transaction size"

> Monthly subscriptions: between `<<<DECIDE>>>` and `<<<DECIDE>>>` CAD. Annual subscriptions: between `<<<DECIDE>>>` and `<<<DECIDE>>>` CAD. (These map directly to the 6 Prices in Section 5.)

---

## Section 4 — Banking & payouts

| Field | Value | Source |
|---|---|---|
| Currency | CAD | Canadian sole-prop, Canadian customers |
| Bank account name | `<<<YOU>>>` (must match legal name on the account) | not in memory |
| Institution number (3 digits) | `<<<YOU>>>` | not in memory |
| Transit number (5 digits) | `<<<YOU>>>` | not in memory |
| Account number (7-12 digits) | `<<<YOU>>>` | not in memory |
| Payout schedule | **Standard (rolling 7-day)** | safe default; can change later |

**Banking warning.** Stripe verifies the bank account via a small test deposit (or instant verification with some institutions). The name on the bank account **must match** your legal name exactly as entered in Section 2. A "Aryan Singh" personal chequing account works. A joint account or a business-named account that isn't registered to "Aryan Singh" will be rejected.

**My recommendation:** use your primary personal chequing account at a major Canadian bank (RBC, TD, Scotia, BMO, CIBC, National Bank, Tangerine, EQ Bank). Verification is fastest with the big six.

---

## Section 5 — Stripe Prices (Phase 1 of today's slice — seven Prices post Step 30a.2)

This is what we'll do **after** activation approves. Listed here so the activation-form numbers align with the Prices we'll create.

Each Price is created in Stripe live mode and its `price_xxx` ID gets written to the corresponding SSM key. Six recurring Prices (tier × cadence) + one one-time intro-fee Price = seven Prices, seven SSM keys.

| # | Tier | Cadence | Type | Instance Cap | Stripe Price (CAD) | SSM key |
|---|---|---|---|---|---|---|
| 1 | Individual | monthly | recurring | 3 | `<<<DECIDE>>>` / month | `/luciel/prod/stripe/price_id/individual` |
| 2 | Individual | annual | recurring | 3 | `<<<DECIDE>>>` / year (~17% prepay discount) | `/luciel/prod/stripe/price_id/individual_annual` |
| 3 | Team | monthly | recurring | 10 | `<<<DECIDE>>>` / month | `/luciel/prod/stripe/price_id/team_monthly` |
| 4 | Team | annual | recurring | 10 | `<<<DECIDE>>>` / year (~17% prepay discount) | `/luciel/prod/stripe/price_id/team_annual` |
| 5 | Company | monthly | recurring | 50 | `<<<DECIDE>>>` / month | `/luciel/prod/stripe/price_id/company_monthly` |
| 6 | Company | annual | recurring | 50 | `<<<DECIDE>>>` / year (~17% prepay discount) | `/luciel/prod/stripe/price_id/company_annual` |
| 7 | (none — applies to all first-timers) | one-time | one-time | — | **$100 CAD** (locked) | `/luciel/prod/stripe/price_id/intro_fee` |

**Price #7 — intro fee — is locked to $100 CAD by the Step 30a.2 design.** All six recurring Prices need a `<<<DECIDE>>>` answer; only #7 is fixed. Create the recurring Prices with `trial_period_days=90` configured at the Stripe Price level OR at the Subscription level (Stripe Option A — we'll attach `trial_period_days=90` via the `subscription_data` block in `BillingService.create_checkout_session` at checkout time, so the Price itself can be created without a trial and the trial is applied per-subscription).

**Source citations:**
- Tier names and instance caps: `app/models/subscription.py` (TIER_INDIVIDUAL, TIER_TEAM, TIER_COMPANY; TIER_INSTANCE_CAPS = 3/10/50)
- Cadence values: `app/models/subscription.py::BILLING_CADENCE_*`
- SSM key mapping (post Step 30a.2): `app/services/billing_service.py` (the `PRICE_ID_KEY` lookup table now seven entries) and `app/core/config.py` (`stripe_price_intro_fee` setting added at Step 30a.2)
- Annual discount target: `app/models/subscription.py` ("~17% prepay incentive" — the v1 design intent)
- Intro fee + 90d trial: Step 30a.2 design — `INTRO_TRIAL_DAYS=90`, uniform across all (tier, cadence) pairs; first-time-customer gate via `BillingService.is_first_time_customer(email)`

**Annual-discount math note.** A "17% prepay discount" expressed as 12-month savings is roughly equivalent to "pay for 10 months, get 12" (10/12 = 0.833, i.e. 16.7% off). The actual annual Price you enter in Stripe is monthly × 10, not monthly × 12 × 0.83 — both are close, but the "× 10" framing is cleaner for marketing copy ("get 2 months free") and matches the v1 design intent recorded in the migration. Pick the framing that you'd rather defend on a sales call.

**Pricing-decision sanity check.** Before you decide the six dollar amounts, three questions to think about:

1. **Anchor tier.** Individual monthly is the entry point. What's the lowest price at which a Canadian solo real estate agent doing 20-30 deals/year says "yes" without thinking? That's the Individual-monthly number. (Industry comp: Follow Up Boss, Top Producer, Sierra Interactive sit at $69-$129 USD/month per user.)
2. **Team vs. Individual ratio.** Team cap is 10 instances, Individual is 3. If Individual is $X/month, Team is rarely a flat $X × 3.33 — it's typically priced at $X × 2 to $X × 2.5 to reward team adoption. Common pattern: Individual $69, Team $129, not Team $230.
3. **Company tier.** 50 instances. Often priced "call us" in enterprise SaaS, but Step 30a.1 makes Company self-serve too. Common pattern is Company at $X × 4 to $X × 6 of Individual, with the price visible because the cap (50 instances) is the qualification.

---

## Section 6 — Compliance / regulatory questions

Stripe will ask several yes/no questions in this section. Pre-filled answers:

| Question | Answer | Reasoning |
|---|---|---|
| Are you a money-services business? | No | Luciel sells software subscriptions, not payment services |
| Do you sell regulated products (firearms, pharmaceuticals, cannabis, alcohol, tobacco, adult content)? | No | — |
| Do you provide financial advice? | No | Real estate domain ≠ financial advice. Luciel does *not* recommend buy/sell decisions; it qualifies leads and books showings. |
| Do you handle escrow / hold customer funds? | No | Stripe is the payment processor; we never hold funds |
| Are your customers in regulated industries? | Yes (real estate, lightly regulated) | This is fine — real estate licensing is a normal credentialing requirement, not a Stripe-flagged industry |
| Country of customers | Canada (primary), possibly US (future) | start Canada-only; we can add USD prices later |
| Do you ship physical goods? | No | software only |

---

## Section 7 — Pre-submit checklist

Before you click submit on the Stripe activation form:

- [ ] Section 1: Business website URL resolves to a real customer-facing page that explains Luciel, lists pricing tiers, and has a contact path. If not, **stop and build the landing page first**.
- [ ] Section 2: All `<<<YOU>>>` fields filled in your head (or on paper next to you) ready to type into Stripe. SIN should NOT be typed into this scaffold.
- [ ] Section 3: All `<<<DECIDE>>>` decisions resolved (refund window, trial length, annual revenue estimate, transaction size range).
- [ ] Section 4: Banking details confirmed against your actual chequing account; account name matches "Aryan Singh" exactly.
- [ ] Section 5: All six recurring tier prices decided (we will not enter these into the activation form, but we want them decided so Section 3's "transaction size" answer aligns). The seventh Price (intro fee) is locked at $100 CAD by design.
- [ ] Section 6: No surprises (any "Yes" answer on a regulated-product question triggers extra review).
- [ ] You're awake, alert, and not under time pressure. Stripe activation is not the form to rush.

---

## Post-submit expectations

- **Best case:** instant approval (Stripe's automated underwriting clears low-risk Canadian sole-props with clean answers in seconds).
- **Common case:** 24-48 hour review, possibly with one back-and-forth email request for a supporting document (utility bill, bank statement, or screenshot of the business website).
- **Worst case:** rejection. Rare for clean sole-prop B2B SaaS but possible if the website URL doesn't resolve to something coherent, or if a regulated-product answer was triggered by mistake.

Once approved, we proceed to **Phase 1 (Stripe Prices)** and the rest of today's slice per `2026-05-14-tomorrow-slice-queue.md` Section A.

---

## Open items needing your input before Stripe form-submit

These are the `<<<DECIDE>>>` and `<<<YOU>>>` markers consolidated for fast review:

**Business decisions (`<<<DECIDE>>>`):**
1. Section 1: Business website URL — confirmed as `https://www.vantagemind.ai` (real underwriting-quality landing page exists). Apex `https://vantagemind.ai` serves a parked page; that's a separate DNS drift to fix later. Use `www.vantagemind.ai` for the activation form.
2. Section 3: Annual-subscription money-back window — yes/no, on top of the 90-day paid-intro trial that already exists? (Not a code change either way; operational policy only.)
3. ~~Section 3: Trial-policy review~~ — **resolved at Step 30a.2.** Uniform 90-day paid-intro trial ($100 CAD one-time + 90d trial on recurring) replaces the v1 per-tier policy. No decision needed; the activation-form copy in §3 above matches code.
4. Section 3: Estimated annual revenue first 12 months (CAD)?
5. Section 3: Average transaction size range, monthly and annual (depends on Section 5 pricing)?
6. Section 5: Six recurring tier prices in CAD (Individual/Team/Company × monthly/annual)? Intro fee Price #7 is locked at $100 CAD.

**Personal inputs (`<<<YOU>>>` — fill at form-submit time, not here):**
1. Date of birth
2. Home street address + postal code
3. Phone number
4. SIN (type directly into Stripe, never into any file)
5. Bank account details (institution / transit / account numbers)

**Drift status (drifts discovered while drafting v1 of this scaffold):**
- `D-vantagemind-apex-www-split-2026-05-14` — apex vantagemind.ai serves a parked page, www serves the real site. **Still open.** Submit www to Stripe today, fix DNS in a future slice.
- ~~`D-cancellation-cascade-incomplete-conversations-claims-2026-05-14`~~ — **RESOLVED at Step 30a.2 (2026-05-14).** Cascade now covers ten layers (tenants → domains → agents → scope_assignments → embed_keys → luciel_instances → sessions → conversations → identity_claims → messages), and the retention worker hard-purges after 90 days.
- ~~Retention purge missing~~ — **RESOLVED at Step 30a.2 (2026-05-14)** via the nightly Celery beat worker.

Once you've answered the 5 remaining `<<<DECIDE>>>` items (website confirmation already locked to www.vantagemind.ai; refund window; revenue estimate; transaction size; six recurring tier prices), tell me your answers and I'll lock the scaffold. Then you open the Stripe form with `<<<YOU>>>` items ready and copy-paste from the scaffold for everything else.
