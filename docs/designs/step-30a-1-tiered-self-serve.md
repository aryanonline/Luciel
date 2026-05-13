# Step 30a.1 — Tiered self-serve (all three tiers, full close on D-billing-team-company-not-self-serve)

**Status:** Design v2 — pending implementation.
**Owning roadmap step:** Step 30a.1 (per CANONICAL_RECAP §12 Step 30a row and DRIFTS `D-billing-team-company-not-self-serve-2026-05-13`).
**Authored:** 2026-05-13 by Computer (advisor) under the trust delegation *"give that you know our full business scope partner I will leave the entire judgement onto you"*.
**v2 directive (2026-05-13 18:47 EDT):** founder elected *"I would like to ship all three tiers today"* — Company tier moved from sales-led-only to **hybrid (contact form primary + discreet skip-to-checkout link)**. Drift closes **fully** tonight rather than partially.
**Closing tag (planned):** `step-30a-1-tiered-self-serve-complete` on the doc-truthing commit (per the precedent of Step 30c `99c6eb5` / Step 24.5c / Step 31 / Step 30a / Step 31.2 / Step 32).

---

## 1. Source-of-truth grounding

This step is bound by three already-committed canonical statements. The design honours them; it does not re-litigate them.

| Source | Statement | What it forbids |
|---|---|---|
| CANONICAL_RECAP §14 | *"the tiers exist as separate products and not as seat counts."* | A `seat_count` column on `subscriptions`; per-seat metered Stripe pricing; a `team_members` invite table parallel to `ScopeAssignment`. |
| CANONICAL_RECAP §14 | *"A Team Luciel is not a bigger Individual Luciel. It can see across all the team's work, learn from all of their conversations, and act on behalf of any of them — that's a different product."* | Treating Team as Individual + multi-user. Team must ship a **domain-scope Luciel** as the differentiator. |
| ARCHITECTURE §4.7 commentary (line 551) | *"the canonical recap commits that pricing tiers map 1:1 to scope levels."* | Letting an Individual customer mint a domain-scope or tenant-scope Luciel; the tier→scope map is the single axis of differentiation. |
| ARCHITECTURE §3.2.13 (line 380) | *"Annual pricing, multi-SKU, and Team / Company self-serve are explicitly out of scope at v1 — tracked at D-billing-team-company-not-self-serve-2026-05-13, naturally landing at Step 30a.1."* | Mixing this work into any other step. This step **is** Step 30a.1. |
| ARCHITECTURE §3.2.13 (line 384) | *"Password / SSO / MFA auth. Magic-link is the v1 trust model. Step 32a … lands the broader auth story."* | Shipping Company-tier self-serve in this step, because $2k+ buyers require SSO and SSO lands at Step 32a. |
| DRIFTS line 419 (resolution path) | *"`CheckoutSessionRequest.tier` becomes `Literal["individual", "team", "company"]`, four price-id config keys, the `team` and `company` Pricing CTAs flip from waitlist `mode` to `checkout` `mode`."* | Inventing a different shape. This is the path the open drift commits to. |

The CTA-flip statement in DRIFTS line 419 is *partially* superseded by this design: **Company stays sales-led** rather than flipping to checkout, because the SSO dependency (line 384) makes a Company self-serve v1 wrong for the buyer. The doc-truthing close for `D-billing-team-company-not-self-serve` records that nuance.

---

## 2. Pricing & SKU shape (LOCKED, v2)

**Seven Stripe Price IDs** after this step lands. One is existing (Individual monthly); six are new.

| Tier | Cadence | Stripe Price ID config key | Display price (CAD) | Status after Step 30a.1 |
|---|---|---|---|---|
| Individual | monthly | `stripe_price_individual` | $30 / mo | EXISTING — unchanged |
| Individual | annual | `stripe_price_individual_annual` | $300 / yr (~17% off) | NEW |
| Team | monthly | `stripe_price_team_monthly` | $300 / mo | NEW |
| Team | annual | `stripe_price_team_annual` | $3,000 / yr (~17% off) | NEW |
| Company | monthly | `stripe_price_company_monthly` | $2,000 / mo | NEW |
| Company | annual | `stripe_price_company_annual` | $20,000 / yr (~17% off) | NEW |
| Company custom | — | (negotiated per-deal via Stripe coupons on monthly/annual base) | $2,000+ (the "+" in §14) | sales-led upsell |

**Why these specific numbers within the §14 bands:**
- Individual stays at $30/mo. Don't raise a shipped price without market signal.
- Team launches at $300/mo — bottom of the §14 band. Maximises first-customer conversion; raises after the 10th paying team or the first churn-on-price.
- Company launches at $2,000/mo — the §14 floor. The "+" is handled via Stripe coupons / contract amendments on top of the $2,000 base SKU, not via a separate SKU. This keeps the seven-SKU surface manageable.

**Annual cadence framing:** Uniform ~17% off (12 → 10 months equivalent) across all three tiers, a standard SaaS prepay incentive. Annual subscribers do not get mid-cycle downgrade — they get a prorated credit on next renewal if they cancel mid-term, handled entirely by Stripe Customer Portal (no code on our side).

**Trial policy:**
- Individual monthly: 14-day trial, card required (Step 30a behaviour, unchanged).
- Team monthly: 7-day trial, card required (lower than Individual because the *team Luciel* value-realisation curve is steeper — a team lead can judge whether the domain-scope Luciel reads their team's work usefully within a week).
- Company monthly: 7-day trial, card required (same logic; CFO judges the tenant-scope Luciel within a week).
- All annual cadences: **no trial**. A 14- or 7-day trial on a $3k–$20k prepay creates refund-loop risk; annual is an explicit prepay commitment.

**No `monthly_amount_cad` column.** The Stripe Price IDs *are* the source of truth for price; mirroring them in our DB would introduce a divergence risk for zero analytical benefit. The two cohort questions (*how many customers on Team monthly vs annual?*) are answered by `WHERE tier='team' GROUP BY billing_cadence`, which the new column below provides.

---

## 3. Tier ↔ scope mapping (the §4.7-line-551 commitment, made explicit)

This is the **product-differentiation table**. Tiers are scopes, scopes are tiers.

| Tier | Permitted `LucielInstance.scope_level` values | Pre-mint at signup | Hard cap (instances) |
|---|---|---|---|
| `individual` | `agent` only | 1 × agent-scope Luciel (the buyer's own) | 3 |
| `team` | `agent` + `domain` | 1 × domain-scope Luciel (the **team Luciel** — the differentiator) + 1 × agent-scope (the team lead) | 10 |
| `company` | `agent` + `domain` + `tenant` | 1 × tenant-scope Luciel (the **company Luciel**) + 1 × domain-scope (default department) + 1 × agent-scope (IT admin) | 50 (sales-configurable) |

**Why this is correct and not a violation of "tiers ≠ seats":**
The `instance_count_cap` is not a seat count — it is a deliberate per-tier ceiling that prevents pathological over-provisioning under one subscription. A Team customer paying $300/mo who somehow ends up running 200 Luciels (e.g., a script gone wrong) is a billing-integrity problem, not a service-level intent. Caps are aligned to scope-level + tier-band realism, not to "$X per Luciel".

**Why Team's differentiating product is a domain-scope Luciel:** §14 says *"see across all the team's work"*. A domain-scope `LucielInstance` reads memory across every agent under that domain (ARCHITECTURE §4.7 three-layer scope enforcement). That capability — cross-agent intelligence under one policy boundary — is what justifies $300/mo as a *different product*. It is unavailable to Individual customers (service-layer guard) and exists by default for Team customers (pre-minted at signup).

**Why Company's differentiating product is a tenant-scope Luciel:** Same argument one layer up — a tenant-scope Luciel reads across every domain under the tenant, with company-wide audit and policy. That is the §14 "every department, every team, every individual under the company's policies and audit trail" promise.

---

## 4. Schema changes (one migration, additive only, no enum work)

### 4.1 Migration `step30a_1_subscription_cadence_and_caps.py`

**Down revision:** `b8e74a3c1d52` (Step 30a's subscriptions table).
**Pattern E discipline:** additive only; no data backfill that loses information; existing rows default to `monthly` (factually correct — every existing subscription was minted as monthly per Step 30a).

```sql
ALTER TABLE subscriptions
  ADD COLUMN billing_cadence VARCHAR(16) NOT NULL DEFAULT 'monthly',
  ADD COLUMN instance_count_cap INTEGER NOT NULL DEFAULT 3;

ALTER TABLE subscriptions
  ADD CONSTRAINT ck_subscriptions_billing_cadence
    CHECK (billing_cadence IN ('monthly','annual'));

CREATE INDEX ix_subscriptions_tier_active ON subscriptions (tier, active);
```

**What is NOT in the migration:**
- No `ALTER TYPE` on `tier` — `tier` is a `String(32)` in the model (per the deliberate model comment, lines 51–54: *"kept module-level (not a PG enum) so a new tier can land without a schema migration"*). The string already accepts `'team'` and `'company'`; `ALLOWED_TIERS` already lists them.
- No `seat_count` column. §14 forbids it.
- No `conversation_cap` column. §14's product framing doesn't reference message volume; metered conversation caps would slip back toward usage-based pricing, which is the seat-count fallacy in a different costume.
- No new tables.

### 4.2 Model changes

`app/models/subscription.py`:
- Add `billing_cadence: Mapped[str]` (String(16), default `'monthly'`).
- Add `instance_count_cap: Mapped[int]` (Integer, default 3).
- Add module-level `BILLING_CADENCE_MONTHLY = "monthly"`, `BILLING_CADENCE_ANNUAL = "annual"`, `ALLOWED_BILLING_CADENCES = (...)`.
- Add module-level `TIER_INSTANCE_CAPS: dict[str, int] = {TIER_INDIVIDUAL: 3, TIER_TEAM: 10, TIER_COMPANY: 50}`.

### 4.3 Config changes

`app/core/config.py` — add six new optional settings:
```python
stripe_price_individual_annual: str = ""
stripe_price_team_monthly: str = ""
stripe_price_team_annual: str = ""
stripe_price_company_monthly: str = ""
stripe_price_company_annual: str = ""
```

`BillingService.resolve_price_id(tier, cadence)` returns the right config value — fail-closed if missing:

```python
PRICE_ID_KEY: dict[tuple[str, str], str] = {
    (TIER_INDIVIDUAL, BILLING_CADENCE_MONTHLY): "stripe_price_individual",
    (TIER_INDIVIDUAL, BILLING_CADENCE_ANNUAL):  "stripe_price_individual_annual",
    (TIER_TEAM,       BILLING_CADENCE_MONTHLY): "stripe_price_team_monthly",
    (TIER_TEAM,       BILLING_CADENCE_ANNUAL):  "stripe_price_team_annual",
    (TIER_COMPANY,    BILLING_CADENCE_MONTHLY): "stripe_price_company_monthly",
    (TIER_COMPANY,    BILLING_CADENCE_ANNUAL):  "stripe_price_company_annual",
}

def resolve_price_id(self, tier: str, cadence: str) -> str:
    key = PRICE_ID_KEY.get((tier, cadence))
    if key is None:
        raise ValueError(f"Unsupported (tier={tier!r}, cadence={cadence!r}).")
    price_id = getattr(settings, key, "") or ""
    if not price_id:
        raise BillingNotConfiguredError(
            f"Stripe price id {key} not configured for ({tier}, {cadence})."
        )
    return price_id
```

---

## 5. API surface (extend, do not add)

### 5.1 `POST /api/v1/billing/checkout` — extended

**Request body (extended):**
```json
{
  "email": "founder@boutique.example",
  "display_name": "Boutique Realty",
  "tier": "team",                       // NEW: was always 'individual'; now Literal["individual","team"]
  "billing_cadence": "monthly"          // NEW: Literal["monthly","annual"], default "monthly"
}
```

**Validation:**
- `tier` must be in `{"individual", "team", "company"}`. Any other value returns 422.
- `billing_cadence` must be in `{"monthly", "annual"}`. Default `monthly` preserves Step 30a's behaviour.
- The Stripe Price ID is resolved deterministically from the (tier, cadence) pair via `BillingService.resolve_price_id(tier, cadence)`; missing config returns 501 (boot-safe pattern, §3.2.9).

**Stripe Checkout session changes:**
- `subscription_data.trial_period_days` is set per (tier, cadence): Individual monthly = 14, Team monthly = 7, Company monthly = 7, all annual = 0. Constant table `TRIAL_DAYS: dict[tuple[str,str], int]` in `billing_service.py`.
- `payment_method_collection='always'` on every (card required even during trial).
- `subscription_data.metadata` now carries `luciel_tier` and `luciel_billing_cadence` (the webhook handler reads both onto the new `subscriptions` columns).

### 5.2 `POST /api/v1/admin/luciels` — tier-gated scope at the service layer

`AdminService.create_luciel_instance(scope_level=...)` adds a guard before instance creation:

```python
def _enforce_tier_scope(self, *, tenant_id: str, requested_scope: str) -> None:
    sub = self.billing_service.get_active_subscription_for_tenant(tenant_id)
    if not sub or not sub.is_entitled:
        raise BillingError("No active subscription.", status_code=402)
    allowed = TIER_PERMITTED_SCOPES[sub.tier]   # individual=[agent], team=[agent,domain], company=[agent,domain,tenant]
    if requested_scope not in allowed:
        raise BillingError(
            f"Scope '{requested_scope}' requires a higher tier than your current '{sub.tier}'. "
            f"Allowed scopes on this tier: {allowed}.",
            status_code=402,
        )
    # Cap check
    used = self.luciel_repo.count_active_for_tenant(tenant_id)
    if used >= sub.instance_count_cap:
        raise BillingError(
            f"Instance cap reached ({sub.instance_count_cap}). Upgrade tier to mint more.",
            status_code=402,
        )
```

`TIER_PERMITTED_SCOPES` lives in `app/models/subscription.py`:
```python
TIER_PERMITTED_SCOPES: dict[str, tuple[str, ...]] = {
    TIER_INDIVIDUAL: (SCOPE_LEVEL_AGENT,),
    TIER_TEAM: (SCOPE_LEVEL_AGENT, SCOPE_LEVEL_DOMAIN),
    TIER_COMPANY: (SCOPE_LEVEL_AGENT, SCOPE_LEVEL_DOMAIN, SCOPE_LEVEL_TENANT),
}
```

**Why service-layer only, not a DB CHECK constraint:** the join across `subscriptions.tier` and `luciel_instances.scope_level` requires correlating two unrelated tables at instance-creation time. A CHECK constraint there would be a row-level subquery (PostgreSQL forbids it on CHECK constraints) or a trigger (operationally heavier). Service-layer enforcement is the canonical Pattern E approach; a follow-up drift records this for future hardening (see §10 below — `D-tier-scope-mapping-service-layer-only-2026-05-13`).

### 5.3 `OnboardingService.onboard_tenant` — tier-aware pre-minting

Extends to accept a `tier` parameter (default `individual` preserves Step 30a behaviour). At the moment of tenant creation, additionally mints the differentiating Luciel(s):

```python
def _premint_for_tier(self, tenant: TenantConfig, tier: str, primary_user: User) -> None:
    # Always mint the agent-scope Luciel for the primary buyer/user.
    agent_luciel = self.luciel_service.create_instance(
        tenant_id=tenant.tenant_id,
        scope_level=SCOPE_LEVEL_AGENT,
        scope_owner_agent_id=primary_user.agent_id,
        display_name=f"{primary_user.display_name}'s Luciel",
    )
    if tier == TIER_INDIVIDUAL:
        return
    # Team and Company also pre-mint the differentiating cross-agent Luciel.
    if tier in (TIER_TEAM, TIER_COMPANY):
        self.luciel_service.create_instance(
            tenant_id=tenant.tenant_id,
            scope_level=SCOPE_LEVEL_DOMAIN,
            scope_owner_domain_id=tenant.default_domain_id,
            display_name=f"{tenant.display_name} Team Luciel",
        )
    if tier == TIER_COMPANY:
        self.luciel_service.create_instance(
            tenant_id=tenant.tenant_id,
            scope_level=SCOPE_LEVEL_TENANT,
            display_name=f"{tenant.display_name} Company Luciel",
        )
```

This is the **single most important code change in Step 30a.1** — it is what makes the §14 "different product" claim true in shipped code. A Team customer's signup *materially produces a different product* than an Individual customer's signup, and the difference is the domain-scope Luciel that pops into being at the webhook moment.

### 5.4 `GET /api/v1/billing/me` — extended response

The 11-field `SubscriptionStatusResponse` becomes 13 fields:
- `billing_cadence: Literal["monthly", "annual"]`
- `instance_count_cap: int`

Existing 11 fields unchanged. Cookie-bridge consumers (the Step 32 Dashboard) read these new fields to gate UI.

### 5.5 No new endpoints

Specifically:
- **No `/api/v1/team/invite`** — adding a teammate is `POST /api/v1/admin/luciels` with `scope_level='agent'` and a new `teammate_email` parameter. The webhook handler (no, *the request-path handler*) auto-provisions the User/Agent/ScopeAssignment rows for the teammate's email and sends them a magic-link email. Same magic-link service as Step 30a.
- **No `/api/v1/team/members`** — listing teammates is `GET /api/v1/dashboard/agents` (an existing Step 31 endpoint, scope-filtered by the cookie's tenant).
- **No `/api/v1/team/members/{id}/deactivate`** — deactivating a teammate is `POST /api/v1/admin/agents/{id}/deactivate` (existing path; uses ScopeAssignment.end_reason='DEPARTED', Pattern E).

This is the heart of the design discipline: **the scope hierarchy already shipped at Step 24.5b can carry the Team-tier product surface without inventing parallel constructs.**

---

## 6. Frontend changes (`aryanonline/Luciel-Website`)

### 6.1 `src/pages/Pricing.tsx`

- **Team tier:** flip `primary: "demo"` → `primary: "waitlist"` with `mode="checkout"`. Update displayed `price` from `"$300–800"` to `"$300"` (band-bottom launch). Primary CTA = "Start free trial".
- **Company tier:** **hybrid** — primary CTA stays "Book a demo" (preserves T10 framing), but a small "Skip the call →" link sits below it routing to `/signup?tier=company&cadence=<chosen>`. Update displayed `price` from `"$2,000"` to `"$2,000"` (no `+` suffix at v1 since the SKU is concrete; the upsell "+" is sales-led via coupons against this base).
- **Monthly/Annual toggle** at the top of the tiers grid. Toggling annual updates each card's display price (`$30/$300/$2,000` → `$300/$3,000/$20,000` with "per year" subtext) and threads `cadence=annual` through every CTA's `?cadence=` query param.
- **Founder-friendly copy:** the rationale paragraph (`A Team Luciel is not a bigger Individual Luciel…`) stays verbatim — it is the §14 commitment surfaced as marketing copy and must not drift.

### 6.2 `src/pages/Signup.tsx`

- Accept `tier` (already accepted) and new `cadence` query params.
- Forward both to `POST /api/v1/billing/checkout`.
- On 422 from a `tier=company` request, redirect to `/contact?tier=company&from=signup`.

### 6.3 `src/pages/Dashboard.tsx`

- Read `tier` and `instance_count_cap` from `/billing/me`.
- **New Team tab** (between Overview and Luciels), visible iff `tier in {team, company}`:
  - Lists agents under the current tenant via `GET /api/v1/dashboard/agents`.
  - "Add teammate" button opens a form (email + display_name). Submits `POST /api/v1/admin/luciels` with `scope_level='agent'`, `teammate_email=<email>`. The backend mints the User/Agent/ScopeAssignment and pre-mints an agent-scope Luciel under that teammate. The teammate receives a magic-link email.
- The **Luciels tab** displays a scope badge per instance (Agent / Domain / Company). Team tenants see their domain-Luciel highlighted as "Team Luciel"; Company tenants additionally see the tenant-Luciel highlighted as "Company Luciel".
- **Create Luciel form** in the Luciels tab unlocks `scope_level` based on tier (closing Step 32 carve-out (iii)). Individual customers don't see the dropdown (it's hard-pinned to `agent`); Team customers see Agent / Team; Company customers see Agent / Team / Company.
- **Instance-count guard:** if `used >= instance_count_cap`, the Create button disables with tooltip *"Upgrade tier to mint more."*

### 6.4 `src/pages/Contact.tsx` (existing)

- Accept `?tier=company` and pre-fill the contact form's reason as "Company-tier inquiry".
- No new backend endpoint; existing waitlist/contact form path stays.
- The "Skip the call →" link from the Pricing Company card bypasses this page entirely — it goes directly to `/signup?tier=company`.

---

## 7. Stripe configuration steps (manual, user runs)

1. Stripe Dashboard → Products → Create five new Prices on the existing `Luciel Subscription` product (or create separate Products per tier — the model accepts either):
   - **Individual Annual:** $300 CAD, recurring yearly. Copy the `price_…` ID.
   - **Team Monthly:** $300 CAD, recurring monthly. Copy the `price_…` ID.
   - **Team Annual:** $3,000 CAD, recurring yearly. Copy the `price_…` ID.
   - **Company Monthly:** $2,000 CAD, recurring monthly. Copy the `price_…` ID.
   - **Company Annual:** $20,000 CAD, recurring yearly. Copy the `price_…` ID.
2. AWS Secrets Manager → update the Stripe secret (or whichever Stripe-secret path the prod env reads) with five new keys:
   - `STRIPE_PRICE_INDIVIDUAL_ANNUAL`
   - `STRIPE_PRICE_TEAM_MONTHLY`
   - `STRIPE_PRICE_TEAM_ANNUAL`
   - `STRIPE_PRICE_COMPANY_MONTHLY`
   - `STRIPE_PRICE_COMPANY_ANNUAL`
3. ECS task definition revision needs no change beyond the env-var injection ARN (already wired at Step 30a). Restart task to pick up new env.
4. Verify each new price via `tests/e2e/step_30a_1_live_e2e.py` (a Step-30a sibling harness — see §8.2).

---

## 8. Tests

### 8.1 New contract tests (`tests/api/test_step30a_1_tiered_self_serve_shape.py`)

- **27 tests**, modelled on Step 30a's 46-test pattern.
- Coverage:
  - `CheckoutSessionRequest.tier` Literal validation (4 tests: individual ok / team ok / company ok / invalid 422).
  - `CheckoutSessionRequest.billing_cadence` Literal validation (3 tests: monthly ok / annual ok / invalid 422).
  - `BillingService.resolve_price_id(tier, cadence)` returns correct config value for all six (tier, cadence) pairs (6 tests).
  - `BillingService.resolve_price_id` raises `BillingNotConfiguredError` when each individual price id is missing (6 tests).
  - `AdminService._enforce_tier_scope` raises 402 for individual→domain, individual→tenant, team→tenant; succeeds for individual→agent, team→agent, team→domain, company→agent, company→domain, company→tenant (3 fail + 6 pass = 9 tests, but compacted into 6 parametrised cases).
  - `AdminService._enforce_tier_scope` raises 402 on `used >= instance_count_cap` (1 test).
  - `SubscriptionStatusResponse` shape includes `billing_cadence` and `instance_count_cap` (1 test).
  - `OnboardingService._premint_for_tier` produces the right scope-skeleton per tier (3 tests: individual = 1 agent; team = 1 agent + 1 domain; company = 1 agent + 1 domain + 1 tenant).

### 8.2 New live e2e harness (`tests/e2e/step_30a_1_live_e2e.py`)

- **Five-scenario harness**, env-gated on a Stripe test mode key:
  - **Scenario A:** Individual annual checkout → webhook → `billing_cadence='annual'` row, `instance_count_cap=3` → magic-link → `/billing/me` reflects annual + cap.
  - **Scenario B:** Team monthly checkout → webhook → tenant minted → **domain-scope Team Luciel pre-minted** (the §14 differentiator validated end-to-end) + agent-scope Luciel for the lead → tenant_admin adds a teammate via `POST /admin/luciels` → teammate's User/Agent/ScopeAssignment rows exist → teammate receives magic-link email.
  - **Scenario C:** Team customer attempts to mint a tenant-scope Luciel via `POST /admin/luciels` → 402 with `"Scope 'tenant' requires a higher tier"`.
  - **Scenario D:** Company monthly checkout via the skip-to-checkout link (`/signup?tier=company`) → webhook → **all three pre-mints validated** (tenant-scope Company Luciel + domain-scope Team Luciel + agent-scope buyer Luciel), `instance_count_cap=50` → CFO creates a second domain via `POST /admin/domains`, promotes a domain_admin under it, verifies create-at-or-below rule still enforced.
  - **Scenario E:** Individual customer attempts to mint a domain-scope Luciel via `POST /admin/luciels` → 402 with `"Scope 'domain' requires a higher tier"`. Same customer hits `instance_count_cap` (3) on agent-scope mints → 402 with `"Instance cap reached"`.
- Uses Stripe's `subscription_data.trial_period_days=0` override for fast webhook reach in test mode.

### 8.3 Existing tests untouched

The 46 Step 30a contract tests in `tests/api/test_step30a_billing_shape.py` continue to pass — every change in this step is additive. The pre-existing failure in `test_router_registered_on_v1_aggregate` (tracked at `D-step-30a-billing-shape-test-moderation-config-failure-2026-05-13`) is unrelated and unchanged.

---

## 9. Doc-truthing matrix (the close that this step lands)

| Doc | Section | Change |
|---|---|---|
| CANONICAL_RECAP.md | §12 (Status of plan) | New Step 30a.1 row, ✅ status, success criterion *"a team lead can find Luciel on our website, pay $300/mo or $3,000/yr, receive their team's pre-minted domain-scope Luciel, add teammates by email, change plan or cancel — all without anyone from our team being involved."* Quote the Q5 connection. |
| CANONICAL_RECAP.md | §14 (Monetization) | Update Individual row price to *"$30/mo or $300/yr"*. Update Team row price to *"$300/mo or $3,000/yr (band $300–800)"*. Keep Company row unchanged ($2,000+ contact sales). Add a new paragraph noting the annual cadence and the 7-day trial on Team monthly. |
| ARCHITECTURE.md | §3.2.13 | Update the "Currency, tax, and pricing" subsection: remove the *"Annual pricing, multi-SKU, and Team / Company self-serve are explicitly out of scope"* sentence, replace with *"All three tiers self-serve at Step 30a.1, monthly + annual cadences each. Company tier ships with a hybrid CTA (contact form primary, skip-to-checkout link below) — sales-touch is the recommended path for T10 (CANONICAL_RECAP §12) deployments, but does not technically gate checkout. SSO for the Company tier remains a separate concern lighting up at Step 32a as a forward-compatible upgrade."* Add new paragraph "Tier ↔ scope mapping" citing the table in §3 of this design. |
| DRIFTS.md | `D-billing-team-company-not-self-serve-2026-05-13` | **FULL CLOSE.** Both Team and Company self-serve land at Step 30a.1. Company self-serve is hybrid: contact form primary + discreet skip-to-checkout link. SSO remains a separate Step 32a concern (the cookie-bridge stays the v1 auth surface; future SSO is forward-compatible per ARCHITECTURE §3.2.13 line 384). Update Status to *"CLOSED 2026-05-13 — All three tiers self-serve via Stripe Checkout (Individual+Team+Company; monthly+annual cadences). Company's marketing surface prefers the contact form for sales-touch onboarding but does not technically gate checkout."* |
| DRIFTS.md | new entry | `D-tier-scope-mapping-service-layer-only-2026-05-13` — *"The tier→scope creation guard is enforced in `AdminService._enforce_tier_scope`, not via DB CHECK constraint. PostgreSQL CHECK constraints cannot subquery another table; a trigger would carry the rule but adds operational complexity. Service-layer enforcement is canonical Pattern E. A future hardening pass may add a deferred-validity trigger; tracked here for symmetry."* |
| Closing tag | git | `step-30a-1-tiered-self-serve-complete` on the doc-truthing commit. |

---

## 10. Drifts opened by this step

1. **`D-tier-scope-mapping-service-layer-only-2026-05-13`** — see §9 row. Tracks the deliberate service-layer-only enforcement of the tier→scope map.

That's the only new one. Notably **no** new auth drift, **no** new billing-shape drift, **no** new schema drift.

## 11. Drifts NOT closed by this step (carried forward, named here for symmetry)

- `D-magic-link-auth-cookie-session-2026-05-13` — Step 32a still owns the broader auth surface. Team self-serve uses the same cookie path Step 30a established.
- `D-admin-audit-logs-actor-user-id-fk-missing-2026-05-13` — unchanged; the teammate-add flow writes audit rows with `actor_label="cookie:<lead-email>"` and the FK still absent.
- `D-step-30a-billing-shape-test-moderation-config-failure-2026-05-13` — unrelated; unchanged.
- `D-rotation-procedure-laptop-dependent-2026-05-12` — unchanged; user runs the Step 30a.1 prod deploy from his laptop using the runbook precedent.
- `D-vantagemind-dns-cloudfront-mismatch-2026-05-13` (NEW, will be opened separately tonight) — the Amplify default URL and `vantagemind.ai` both serve 404s on `/pricing` and `/dashboard` from a stale S3 CloudFront origin; verification of this step's deploy must hit the Amplify default URL after DNS fix or hit the ALB directly for backend tests.

---

## 12. Execution sequence (when implementation starts)

1. **Backend PR** on `aryanonline/Luciel` branch `step-30a-1-tiered-self-serve`:
   - Commit A: model + migration + config keys (`subscription.py`, alembic, `config.py`).
   - Commit B: service-layer changes (`billing_service.py`, `billing_webhook_service.py`, `admin_service.py`, `onboarding_service.py`).
   - Commit C: route schema updates (`schemas/billing.py`, `api/v1/billing.py`, `api/v1/admin.py`).
   - Commit D: 22 contract tests + live e2e harness.
2. **Website PR** on `aryanonline/Luciel-Website` branch `step-30a-1-tiered-self-serve`:
   - Commit A: Pricing.tsx CTA flip + cadence toggle + display price updates.
   - Commit B: Signup.tsx tier/cadence pass-through.
   - Commit C: Dashboard.tsx Team tab + Add teammate flow + scope-aware Create Luciel.
   - Commit D: Vitest tests for the new components + admin gateway extensions.
3. **Stripe configuration** (manual, user runs) — §7 above.
4. **Prod deploy** (user runs from laptop, runbook in `docs/runbooks/step-30a-1-prod-deploy.md`):
   - Pre-flight: confirm alembic current = `b8e74a3c1d52` (Step 30a head).
   - Build + push image; register new task definitions; run migration; roll services.
   - Smoke test all three scenarios from §8.2 against prod (with Stripe test mode price IDs swapped for live IDs).
5. **Doc-truthing commit** — §9 matrix executed atomically.
6. Closing tag `step-30a-1-tiered-self-serve-complete` on the doc-truthing commit.

---

## 13. Open questions reserved for after first read-back

None at this draft — every decision in §2 / §3 / §4 / §5 is locked under the strictness directive and the trust delegation. The doc is read-ready.
