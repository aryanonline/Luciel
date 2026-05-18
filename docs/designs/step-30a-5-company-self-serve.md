# Step 30a.5 — Company self-serve completion (org-builder UI, cascading invites)

**Status:** Design v1 — pending implementation.
**Owning roadmap step:** Step 30a.5 (per CANONICAL_RECAP §12 Step 30a.5 row and DRIFTS §3 `D-company-self-serve-incomplete-org-building-ui-missing-2026-05-16`).
**Authored:** 2026-05-18 by Computer (advisor) under the continuing trust delegation, on the doc-truthing precedent of `docs/designs/step-30a-1-tiered-self-serve.md`.
**Closing tag (planned):** `step-30a-5-company-self-serve-complete` on the doc-truthing commit (per the Step 30c `99c6eb5` / Step 24.5c / Step 31 / Step 30a / Step 31.2 / Step 32 / Step 30a.1 / Step 30a.4 precedent).
**Strict predecessors (both satisfied):** Step 30a.3 (password auth — closed 2026-05-16 via `~~D-magic-link-only-auth-no-password-fallback-2026-05-16~~`) and Step 30a.4 (Team self-serve invite UI — closed 2026-05-17 via `~~D-team-self-serve-incomplete-invite-ui-missing-2026-05-16~~`).

---

## 1. Source-of-truth grounding

This step is bound by the canonical statements below. The design honours them; it does not re-litigate them.

| Source | Statement | What it forbids |
|---|---|---|
| CANONICAL_RECAP §12 Step 30a.5 row | *"A Company customer who paid the $1,000 intro fee opens `/app`, lands on `/app/company`, creates two Domains ('Sales', 'Marketing'), invites one department lead per Domain. Each lead clicks their invite, sets a password, lands on `/app/team` scoped to their Domain, invites two agents each. Six invite emails total, all green."* | Any shape that requires founder involvement, manual SQL, or a support ticket between paid Checkout and a working two-Domain org chart. |
| CANONICAL_RECAP §13.1 T1 (sharpened 2026-05-16) | *"The whole branching is done by the customer, in one sitting, with zero founder involvement."* | Treating the Company-tier first-mile as a sales-led onboarding. Any path that gates the org-builder behind a support call. |
| CANONICAL_RECAP §14 ¶268 | *"the tiers exist as separate products and not as seat counts."* | A `seat_count` on `subscriptions`; per-seat metered Stripe pricing; treating Company as Team-with-more-users. The Company-tier differentiator is **multi-Domain org-building**, not more agents. |
| ARCHITECTURE §3.2.13 "Tier ↔ scope mapping" | *"`TIER_SCOPES = {individual: {agent}, team: {agent, domain}, company: {agent, domain, tenant}}`."* | Letting a Team-tier admin create a second Domain (the cap is `{agent, domain}` — one Domain, pre-minted at signup); letting a Company-tier admin create a tenant-scope Luciel without the Company-tier provisioning the schema for it. |
| ARCHITECTURE §3.2.13 "Team-invite path" (re-locked 2026-05-16) | *"Step 30a.4 lands the dedicated invite surface… `POST /api/v1/admin/invites` (creates an invite for the caller's tenant + domain, capped at `TIER_INSTANCE_CAPS[tier]`)."* | Inventing a parallel invite primitive. The Company tier's department-lead and agent invites re-use the Step 30a.4 `/admin/invites` route unchanged. |
| ARCHITECTURE §4.1 | *"Scope hierarchy as the billing boundary."* | Letting one Company tenant's admin create Domains under another tenant; letting a department lead create Domains under their parent tenant. Domain creation is **tenant-scope** authority and only the Company admin holds it. |
| ARCHITECTURE §4.7 | Three-layer scope enforcement: SQL filter + post-query defense-in-depth + service-layer authz. | Bypassing the standard enforcement by hand-rolling a Company-tier-only check inline in the route. The cookied-caller resolution must flow through `_resolve_invite_actor` (or its sibling for the new `/admin/domains` route) the same way every other cookie-gated route does. |
| DRIFTS §3 `D-company-self-serve-incomplete-org-building-ui-missing-2026-05-16` Resolution path | *"Re-use the existing Step 30a.4 `POST /api/v1/admin/invites` route with an optional `domain_id` field so an invite can be pre-scoped to a specific Domain."* | Adding `domain_id` on the invite payload as a fresh field — the field is already optional on `UserInviteCreate` (Step 30a.4); this step **uses** it, it does not introduce it. |
| Past-session locked plan (2026-05-16) | *"removal of `teammate_email` overload on `POST /admin/luciel-instances` scheduled at Step 30a.5 close."* | Keeping the deprecated overload past this step's closing tag. We delete the branch in this arc and bake the removal into the doc-truthing commit. |
| Step 32 wave 2 (planned in ARCHITECTURE §3.2.13 last paragraph and §3.2.12 wave-2 stanza) | The `/app/company` route family ships at Step 32 wave 2. | Front-running Step 32 wave 2 by inventing the full `/app/*` scope-adaptive shell here. This step ships the **org-builder surface itself**; it lives behind the same Dashboard.tsx tab pattern Step 30a.4's TeamTab uses, and Step 32 wave 2 later lifts both into `/app/team` and `/app/company`. |

The last row is the load-bearing UX judgment of this design: **the Company-tier org-builder ships as a CompanyTab inside `Dashboard.tsx`, not as a new `/app/company` page**, because Step 30a.4's TeamTab already shipped that way and the §12 Step 32 row explicitly carves the `/app/*` lift into wave 2. Doing it here would front-run a load-bearing surface that Step 32 wave 2 owns end-to-end. The CANONICAL_RECAP §12 Step 30a.5 row's literal *"opens `/app`, lands on `/app/company`"* is the target end-state; the interim shape is "opens `/dashboard`, lands on the Company tab, with the same logical surface", and Step 32 wave 2 lifts both tabs into their final `/app/*` homes without a backend change. The CANONICAL_RECAP and ARCHITECTURE doc-truthing rows below carry that nuance explicitly.

---

## 2. What ships, end-to-end

A Company-tier customer (the tenant admin who paid $1,000) lands on `/dashboard` cookied from the Step 30a.3 welcome-set-password flow. The Dashboard already renders Overview + Luciels + Team + Account tabs today; this step adds a fifth tab — **Company** — that becomes the canonical org-builder surface for the Company tier. Behaviour:

1. **The Company admin** opens the Company tab. Sees a list of Domains under their tenant (initially: one Domain — the `company-luciel` default that `TierProvisioningService.pre_mint_for_tier` pre-minted at signup), with a "Create Domain" form below the list. They type "Sales", click Create — a new Domain row appears with a "Pending leads: 0 · Active leads: 0" rollup and an "Invite department lead" affordance. They click that affordance, type the lead's email + display name, click Send — a `USER_INVITED` audit row writes with `details.role='department_lead'` and `details.domain_id='sales'`, an SES welcome-set-password email goes out. They repeat for "Marketing".
2. **Each department lead** clicks the email link, lands on `/auth/set-password?token=…`, sets a password ≥ 8 chars (the Step 30a.3 page, re-used unchanged), and the `/auth/set-password` route's invite branch consumes the token, marks the invite redeemed, provisions a User + Agent + ScopeAssignment with `role='department_lead'` under the inviter's tenant + the **invite's pinned Domain** (not the inviter's default Domain — this is the Step 30a.5 carry of the `domain_id` field on `UserInvite`), emits `INVITE_REDEEMED`, mints a session cookie, redirects to `/dashboard`.
3. **The department lead** lands on `/dashboard` cookied and sees the **Team tab** (the same Step 30a.4 surface) scoped to their Domain — `_resolve_invite_actor` resolves the lead's `(tenant_id, domain_id)` from their newly-minted ScopeAssignment, the TeamTab's `listAgents(true) + listInvites()` calls filter by that Domain. The lead invites two agents through the same Step 30a.4 form. Two welcome-set-password emails go out per lead.
4. **Each agent** clicks the email link, sets a password, lands on `/dashboard` cookied with `role='teammate'` under their lead's Domain, sees the Luciels tab scoped to their own agent.
5. **The Company admin** refreshes `/dashboard/company` and sees the two Domains rolled up: "Sales: 1 active lead, 2 active agents", "Marketing: 1 active lead, 2 active agents". Total invite emails: **six**.

Six invite emails, six redemptions, six new password-set events, all green — zero founder involvement.

---

## 3. Schema changes (one Alembic migration, additive only)

### 3.1 Migration `step30a_5_user_invite_role_and_audit_actions.py`

**Down revision:** `b4d8a2e7c1f3` (Step 30a.4 owner-scope-assignment backfill, current Alembic head per the 30a.4 closure stanza).
**Pattern E discipline:** additive only; no data loss; existing `user_invites` rows backfill to `role='teammate'` (factually correct — every existing invite from Step 30a.4 was minted as a teammate invite).

```sql
ALTER TABLE user_invites
  ADD COLUMN role VARCHAR(32) NOT NULL DEFAULT 'teammate';

ALTER TABLE user_invites
  ADD CONSTRAINT ck_user_invites_role
    CHECK (role IN ('teammate', 'department_lead'));

-- No backfill UPDATE — server-side DEFAULT 'teammate' is factually correct.
```

**What is NOT in the migration:**
- No new `domain_id` column. `user_invites.domain_id` already exists as a NULLABLE FK (Step 30a.4 commit C1, migration `e7b2c9d4a18f`). Step 30a.5 *uses* it; this migration does not touch it.
- No PG ENUM type for `role`. `String(32)` matches the precedent of `tier` on `subscriptions` and `purpose` / `status` on `user_invites` — a future role string (e.g., `'platform_admin'`, `'auditor'`) can land without an `ALTER TYPE`.
- No new tables.
- No new indexes. The existing partial index `(tenant_id, status) WHERE status='pending'` from Step 30a.4 covers the Company-tab's "pending invites by Domain" query when combined with the WHERE `domain_id=…` predicate; the planner uses the partial index and filters on `domain_id` in-memory. The Company Domain cap (§5.2 below) is 50, matching the §14 Luciel instance cap; revisit indexing if a tenant approaches the cap in production.

### 3.2 Model changes

`app/models/user_invite.py`:
- Add `role: Mapped[str]` (String(32), default `'teammate'`, server-side default `'teammate'`).
- Add module-level constants `INVITE_ROLE_TEAMMATE = "teammate"`, `INVITE_ROLE_DEPARTMENT_LEAD = "department_lead"`, `ALLOWED_INVITE_ROLES = (INVITE_ROLE_TEAMMATE, INVITE_ROLE_DEPARTMENT_LEAD)`.

`app/models/admin_audit_log.py`:
- Add two new audit action constants — `ACTION_DOMAIN_CREATED = "domain_created"` and `ACTION_DOMAIN_DEACTIVATED = "domain_deactivated"` — to `ACTION_WHITELIST`. The `_DEACTIVATED` variant is added now (not on first deactivation use) so the constant exists ahead of Step 33 / Step 11 retention surfaces that may flip a Domain to `active=False` through the same Pattern E cascade `tenant_configs` uses.

### 3.3 Schema changes (Pydantic)

`app/schemas/invite.py`:
- `UserInviteCreate` adds `role: Literal["teammate", "department_lead"] = "teammate"`. Default preserves Step 30a.4 behaviour.
- `UserInviteRead` adds `role: str`.

`app/schemas/admin.py`:
- New `DomainSelfServeCreate(BaseModel)`:
  ```python
  class DomainSelfServeCreate(BaseModel):
      slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
      display_name: str = Field(min_length=2, max_length=128)
      description: str | None = Field(default=None, max_length=512)
  ```
  Deliberately distinct from `DomainConfigCreate` so the cookied self-serve surface cannot carry `tenant_id`, `system_prompt_additions`, `allowed_tools`, `policy_overrides`, `preferred_provider`, or `created_by` — those remain operator-only via the existing admin-key-gated `POST /admin/domains` route. The cookied caller's `tenant_id` is sourced from `_resolve_invite_actor`; the rest stay at their model defaults.

---

## 4. API surface

### 4.1 New cookie-gated route: `POST /api/v1/admin/domains/self-serve`

Mounted in `app/api/v1/admin.py` alongside the four Step 30a.4 `/admin/invites` routes (same cookie-gated section, same `_resolve_invite_actor` precondition).

**Path:** `POST /api/v1/admin/domains/self-serve`
**Auth:** session cookie via `_resolve_invite_actor`.
**Body:** `DomainSelfServeCreate` from §3.3.
**Returns:** 201 with `DomainConfigRead` (the existing schema — full read shape including the audit fields).

**Why a new path rather than overloading the existing `POST /admin/domains`:** The existing admin-key-gated route at `app/api/v1/admin.py` line 327 carries `DomainConfigCreate` with `tenant_id`, `domain_id`, and the full operator-side configuration surface. Overloading it with cookie-auth would require either (a) a polymorphic body schema that re-asserts the cookied tenant_id matches the body's tenant_id (audit-surface noise for zero customer value — the cookied caller already has exactly one tenant) or (b) silently dropping body fields when the auth method is cookie (bug-prone, violates the explicit-surface discipline Step 30a.4's `UserInviteCreate` shape established). The new path is a clean sibling, the same way `POST /api/v1/admin/invites` is a clean sibling to the legacy `POST /admin/luciel-instances?teammate_email=…` shape it replaced. The existing admin-key-gated `POST /admin/domains` stays for operator-side use (Step 33b enterprise tier, ops-mode tenant provisioning).

**Tier gate (Pattern E enforcement at the service layer, no DB CHECK constraint — same rationale as Step 30a.1 §6.2 `D-tier-scope-mapping-service-layer-only-2026-05-13`):** the route resolves the cookied caller's tenant_id via `_resolve_invite_actor`, then calls `AdminService._enforce_tier_scope(tenant_id=tenant_id, requested_scope=SCOPE_LEVEL_DOMAIN)`. The existing `TIER_PERMITTED_SCOPES` map already lists `domain` under `team` and `company` — but a Team-tier customer creating a *second* Domain via this surface would still hit the **Domain count cap** below. The auth-gate response is therefore tier-aware in a way that lets a Team-tier customer call the route (returns 402 with `error.code='tier_scope_not_allowed'` only for Individual) but ensures a Team-tier customer cannot create more than one Domain — which is the §14 *"separate products and not seat counts"* canon honoured at the surface level: Team gets one Domain (the differentiator), Company gets multiple Domains (the differentiator).

```python
def _enforce_self_serve_domain_create(
    self,
    *,
    tenant_id: str,
    tier: str,
) -> None:
    # Tier scope gate — Individual cannot create a Domain at all.
    self._enforce_tier_scope(
        tenant_id=tenant_id, requested_scope=SCOPE_LEVEL_DOMAIN
    )
    # Per-tier Domain count cap.
    cap = DOMAIN_COUNT_CAP_BY_TIER[tier]   # team=1, company=50
    used = self.domain_repo.count_active_for_tenant(tenant_id)
    if used >= cap:
        raise BillingError(
            f"Domain cap reached ({cap}). "
            f"Upgrade tier to create more Domains."
            if tier == TIER_TEAM
            else f"Domain cap reached ({cap}). Contact us to expand.",
            status_code=402,
        )
```

`DOMAIN_COUNT_CAP_BY_TIER` lives in `app/models/subscription.py` alongside `TIER_INSTANCE_CAPS`:

```python
DOMAIN_COUNT_CAP_BY_TIER: dict[str, int] = {
    TIER_INDIVIDUAL: 0,   # never reachable (tier scope gate fires first)
    TIER_TEAM: 1,         # team-luciel pre-minted at signup; second Domain is a Company upgrade
    TIER_COMPANY: 50,     # symmetric with TIER_INSTANCE_CAPS[TIER_COMPANY]=50
}
```

**Idempotency / duplicate slug:** the existing unique constraint on `(tenant_id, domain_id)` in `domain_configs` covers slug collisions; a duplicate POST returns 409 with `error.code='domain_slug_taken'` so the website can render a clean form-level error.

**Audit row:** the route writes one `admin_audit_log` row with `action=ACTION_DOMAIN_CREATED`, `details={domain_slug, display_name, creator_user_id}`, `tenant_id=<resolved>`, in the same SQLAlchemy transaction as the `DomainConfig` insert. Field-set per §3.2.7 of ARCHITECTURE.

**LucielInstance auto-mint deliberately not done here.** A Company admin creating a Domain does *not* automatically get a domain-scope Luciel under that Domain — the Domain is a structural container, and the question of "should every Domain have its own pre-minted Luciel?" depends on what the Company admin wants each department to do. The Company admin can mint a domain-scope Luciel later through the Luciels tab's "Create Luciel" form (Step 30a.1's tier-gated scope dropdown already supports it for Company-tier callers). Pre-minting one here would create the same "phantom Luciel" problem Step 30a.4 deliberately rejected for invites — minting on speculation rather than on intent. The pre-minted `company-luciel` tenant-scope Luciel from Step 30a.1 is the tenant's *one* shared resource; per-Domain Luciels are an explicit choice. CANONICAL_RECAP §13.1 T1's success criterion mentions only the six invite emails — not Luciel pre-mints per Domain.

### 4.2 Extended cookie-gated route: `POST /api/v1/admin/invites` — `role` and `domain_id` honoured

**The route exists from Step 30a.4. This step uses two fields that were already on the schema:**
- `role: Literal["teammate", "department_lead"]` — new in `UserInviteCreate` per §3.3 (default `'teammate'` preserves Step 30a.4 callers).
- `domain_id: str | None` — already NULLABLE on `UserInviteCreate` since Step 30a.4 commit C1; defaulted to `_resolve_invite_actor`'s `default_domain_id` when omitted (Step 30a.4 behaviour, unchanged).

**Tier gate:** A `role='department_lead'` invite is only legal for a Company-tier caller. The service-layer guard in `InviteService.create_invite` adds:

```python
if role == INVITE_ROLE_DEPARTMENT_LEAD:
    sub = billing_service.get_active_subscription_for_tenant(tenant_id)
    if not sub or sub.tier != TIER_COMPANY:
        raise InviteError(
            "Department-lead invites require the Company tier.",
            status_code=402,
        )
    if domain_id is None:
        raise InviteError(
            "Department-lead invites must specify a domain_id.",
            status_code=422,
        )
```

**Cross-Domain safety:** a department-lead invite for a Domain that does not exist under the caller's tenant returns 404 with `error.code='domain_not_found'`. The existing `_resolve_invite_actor`'s tenant-mismatch 403 already covers cross-tenant; this case is intra-tenant inter-Domain (a Company admin in tenant T inviting a lead into a Domain D that belongs to a different tenant T'). The check is one extra query: `SELECT 1 FROM domain_configs WHERE tenant_id=:t AND domain_id=:d AND active=true` inside the same transaction.

### 4.3 Redemption path: `/auth/set-password` invite branch honours `role`

The existing `/auth/set-password` route in `app/api/v1/auth.py` (Step 30a.3 + Step 30a.4) consumes the JWT, looks up the `UserInvite` row, and calls `invite_service.redeem_invite` to provision the User + Agent + ScopeAssignment. Step 30a.5 extends `redeem_invite` to read `invite.role` and stamp the provisioned `ScopeAssignment.role` accordingly:

- `role='teammate'` → `ScopeAssignment.role='teammate'` (Step 30a.4 behaviour).
- `role='department_lead'` → `ScopeAssignment.role='department_lead'`, scope pinned to the invite's `domain_id`, and an additional `Agent` is provisioned under that Domain so the new lead has an agent surface to work in.

The `department_lead` role string is already a permissible value in the existing `ScopeAssignment.role` column (it's a free `String(32)` column today — verified against `app/models/scope_assignment.py`; no schema change needed there). Three-layer scope enforcement (§4.7) treats a `department_lead` like any other Domain-scope assignment for read paths — they see their Domain's rollup, agents, invites, and Luciels, but cannot see siblings.

### 4.4 GET routes: `/api/v1/admin/domains/self-serve` (list)

New cookie-gated route: `GET /api/v1/admin/domains/self-serve`. Returns the list of `DomainConfigRead` rows under the caller's tenant (resolved via `_resolve_invite_actor`), with two extra rollup fields injected at the route level (not on the model): `pending_invites_count` and `active_agents_count`, per Domain. The Company tab on the website reads this to render the per-Domain rollup line.

A separate route rather than `/admin/domains` to avoid auth-method polymorphism on the same path (same reasoning as §4.1).

### 4.5 No new endpoints beyond §4.1 and §4.4

Specifically:
- **No `/api/v1/company/*` namespace.** The cookied surface stays under `/admin/*` because the actor is still an admin of *their own tenant* — the path prefix is the auth shape, not the audience.
- **No new `/api/v1/dashboard/company` route.** The Step 31 `/api/v1/dashboard/tenant` route already returns the tenant rollup the Company admin needs; the Company tab queries `/dashboard/tenant` (with cookie auth via Step 31.2's bridge) plus `/admin/domains/self-serve` for the Domain list. Two reads, no new aggregation.
- **No bulk-invite endpoint.** A Company admin inviting four department leads at once still calls `POST /admin/invites` four times. Bulk-invite is a UX wrapper, not a backend primitive; the website can sequence the calls if a "Bulk invite" form is ever added.

This is the central design discipline: **every Company-tier customer flow is composed of Step 30a.4 invite primitives + one new Domain-creation primitive + the existing Step 31 dashboard reads.** No parallel constructs.

---

## 5. Frontend changes (`aryanonline/Luciel-Website`)

### 5.1 `src/pages/Dashboard.tsx` — new `CompanyTab` component

Mirrors the `TeamTab` shape (lines 367–600+ of `Dashboard.tsx`), with two stacked sections:

**Section A — Domains list.** Reads `GET /admin/domains/self-serve`. Renders each Domain as a card:
```
┌─ Sales ─────────────────────────────────────────────┐
│  sales · created May 18 by Aryan                    │
│  Pending leads: 0 · Active leads: 1 · Agents: 2     │
│  [ Invite department lead ]                         │
└─────────────────────────────────────────────────────┘
```
The "Invite department lead" button opens an inline form (email + display_name), submits `POST /admin/invites` with `{invited_email, display_name, role: 'department_lead', domain_id: <this.domain_id>}`. The same Step 30a.4 toast / error-mapping flow applies.

**Section B — Create Domain form.** Two-field form (slug + display_name) below the Domain list. On submit, POSTs `/admin/domains/self-serve` and on 201 prepends the new Domain card to Section A. On 409 (`domain_slug_taken`) the form surfaces a field-level error.

The new tab is visible iff `subscription.tier === 'company'`. For Team-tier callers, the tab is hidden (their TeamTab is enough — they have exactly one Domain, pre-minted at signup, no Domain-creation needed). For Individual callers, the tab is hidden (no Domain scope).

**Per-Domain rollup queries.** The `GET /admin/domains/self-serve` response carries the per-Domain `pending_invites_count` and `active_agents_count` rollups inline (computed at the route level via two `SELECT count(*) … GROUP BY domain_id` queries — one against `user_invites`, one against `agents`). The Company tab does *not* re-query per-Domain dashboards; that's a Step 32 wave 2 concern (the multi-pane scope-adaptive shell).

### 5.2 `src/lib/admin.ts` (or equivalent admin API client)

Three new functions, mirroring the Step 30a.4 `listInvites` / `createInvite` / `resendInvite` / `revokeInvite` shape:
- `listSelfServeDomains(): Promise<SelfServeDomain[]>` → `GET /admin/domains/self-serve`.
- `createSelfServeDomain(input: { slug, display_name, description? }): Promise<SelfServeDomain>` → `POST /admin/domains/self-serve`.
- `inviteDepartmentLead(input: { invited_email, display_name, domain_id }): Promise<UserInvite>` → wraps `createInvite` with `role='department_lead'` and the explicit `domain_id`.

The `SelfServeDomain` TypeScript type mirrors `DomainConfigRead` plus the two rollup fields:
```ts
type SelfServeDomain = {
  id: number;
  tenant_id: string;
  domain_id: string;       // the slug
  display_name: string;
  description: string | null;
  active: boolean;
  created_at: string;
  // Rollups (route-level, not on model)
  pending_invites_count: number;
  active_agents_count: number;
};
```

### 5.3 `src/pages/Dashboard.tsx` — `TeamTab` is reachable by department leads

The existing `TeamTab` already reads `_resolve_invite_actor`'s `(tenant_id, default_domain_id)` via the cookied `listInvites` / `createInvite` calls. When a department lead is cookied, their default scope is `(tenant_id=<company-tenant>, domain_id=<lead's-domain>)` — so the TeamTab automatically filters to their Domain. **No code change on TeamTab itself.** This is the design dividend of Step 30a.4 having sourced tenant + domain from the cookie rather than from URL params.

The Step 30a.5 doc-truthing pass adds one inline comment in `TeamTab` naming the reuse:

```tsx
// Step 30a.5 reuse: when a department_lead is the cookied user (Company
// tier), this tab renders their Domain's invites + agents automatically
// via _resolve_invite_actor on the backend. No client-side branching.
```

### 5.4 `src/pages/Pricing.tsx` — Company CTA refresh

Per `D-marketing-product-boundary-soft-2026-05-16` (the shared marketing-copy drift between Step 30a.4 and Step 30a.5), Step 30a.5 ships:
- Company tier card primary CTA: **"Start 90-day pilot for $1,000"** (mirrors the Individual `$100` and Team `$300` CTAs now live from Step 30a.4 + the per-tier intro fee Stripe activation).
- "Book a demo" link kept as a secondary affordance below the primary CTA (the discreet self-serve path the Step 30a.1 hybrid framing committed to).
- CTA routes through `/signup?tier=company&cadence=<monthly|annual>` exactly as Team does today.

The pricing-page refresh is a copy-pass + a CTA-wiring pass, no new components. `D-marketing-product-boundary-soft-2026-05-16` closes when this ships.

### 5.5 No new pages

Specifically:
- **No `/app/company` page.** Step 32 wave 2 owns the `/app/*` route family. The Company tab lives in `Dashboard.tsx` until Step 32 wave 2 lifts it; the lift is a routing rename, not a re-render.
- **No `/app/team` page.** Same reasoning.
- **No `/onboarding/company` flow.** A Company customer who just paid lands on `/dashboard` cookied (via the Step 30a.3 `/auth/set-password` redirect) and sees the Company tab; the org-builder *is* the onboarding. A "Welcome to Luciel" hero card on the Company tab can be added later if usability research surfaces friction; v1 keeps the surface clean.

---

## 6. Doc-truthing matrix (the close this step lands)

| Doc | Section | Change |
|---|---|---|
| CANONICAL_RECAP.md | §12 Step 30a.5 row | Flip 📋 → ✅. Status cell condensed (per the `D-canonical-recap-section-12-table-overflow-2026-05-14` discipline) to ~5–8 lines naming: closing date, the two new audit actions, the `UserInvite.role` column + migration revision, the new `/admin/domains/self-serve` route family, the CompanyTab inside Dashboard.tsx, the Pricing.tsx Company CTA refresh, the `teammate_email`-overload removal on `POST /admin/luciel-instances`, and the closing tag. Verbose detail lives here in `docs/designs/step-30a-5-company-self-serve.md`. |
| CANONICAL_RECAP.md | §14 ¶268 | No edit. The three-peer-products narrative already lives there; the Company tier card's CTA refresh on the marketing site honours it without recap text changes. |
| ARCHITECTURE.md | §3.2.13 "Team-invite path" paragraph | Append a closing sentence naming Step 30a.5's two extensions: the `role` column on `user_invites`, and the cookie-gated `POST /admin/domains/self-serve` sibling that re-uses `_resolve_invite_actor`. The paragraph keeps Team-invite as the canonical implementation home for the invite primitive; Step 30a.5's department-lead path is one extra `role` value, not a new primitive. |
| ARCHITECTURE.md | §3.2.13 "Tier ↔ scope mapping" paragraph | Append a short stanza naming `DOMAIN_COUNT_CAP_BY_TIER` and the service-layer Domain-cap gate, mirroring the `TIER_INSTANCE_CAPS` / `TIER_PERMITTED_SCOPES` pattern. |
| ARCHITECTURE.md | §3.2.13 "What this design deliberately does not solve in v1" | Update the password-auth bullet to note that Step 30a.5 closes the Company-tier daily-use surface alongside Step 30a.4 (the Step 30a.3 / 30a.4 / 30a.5 trilogy is the all-tiers-working-today arc). |
| ARCHITECTURE.md | §3.2.13 "Scope-adaptive `/app` shell (Step 32 wave 2)" | One sentence appended noting that Step 30a.5's CompanyTab lives in `Dashboard.tsx` today and Step 32 wave 2 lifts it (alongside TeamTab) into `/app/company` / `/app/team` without a backend change. |
| DRIFTS.md | `D-company-self-serve-incomplete-org-building-ui-missing-2026-05-16` | **CLOSE.** Move to §5 with `~~strikethrough~~`. Closure stanza names the closing tag, the closing commits in order, the migration revision (`step30a_5_user_invite_role_and_audit_actions`), the new route + cookie-gated `/admin/domains/self-serve` family, the CompanyTab in `Dashboard.tsx`, the `teammate_email` overload removal, the two new audit action constants, and the carve-out drift below if live-evidence is deferred (Option-1 pattern from Step 30a.4 / Step 30a.2-pilot). |
| DRIFTS.md | `D-marketing-product-boundary-soft-2026-05-16` | **CLOSE.** The Step 30a.5 close ships the Company-tier Pricing CTA refresh, which is the last remaining surface for this drift (Team CTA shipped at Step 30a.4; Index/About copy refresh shipped alongside that). Move to §5 with strikethrough. |
| DRIFTS.md | New entry — `D-step-30a-5-live-company-paid-evidence-pending-2026-05-18` (if live Stripe surface is still pending at code-complete time) | Mirrors the Step 30a.4 / Step 30a.2-pilot carve-out pattern: closing tag cut on code-complete + contract tests + dev-Postgres e2e green, with the live $1,000 paid Company-tier evidence (six-invite cascade on the live wire) carved into a separate drift that closes at the next billing-touch window. Only opened if the live evidence is not yet captured at close time. |
| DRIFTS.md | New entry — `D-teammate-email-overload-removed-2026-05-18` (if any callers still observed) | Tracks any non-website call sites the removal of `teammate_email` may have surfaced. Closes immediately at code-complete time if no observed callers (the website was the only known caller; Step 30a.4 closure stanza noted "removal scheduled at Step 30a.5 close"). |
| Closing tag | git | `step-30a-5-company-self-serve-complete` on the doc-truthing commit (Luciel backend `main`). The website-repo doc-truthing tag is the same name on `Luciel-Website:main`. |

---

## 7. Drifts opened by this step (forward-looking)

1. **`D-domain-count-cap-service-layer-only-2026-05-18`** (parallel to `D-tier-scope-mapping-service-layer-only-2026-05-13`) — *"The per-tier Domain count cap (`DOMAIN_COUNT_CAP_BY_TIER`) is enforced in `AdminService._enforce_self_serve_domain_create`, not via DB CHECK constraint or trigger. Same Pattern E rationale as the tier-scope map: a cross-table count-of-rows guard cannot be a PostgreSQL CHECK; a trigger would carry the rule but adds operational complexity. Future hardening pass may add a deferred-validity trigger."* The single new drift this step opens deliberately.

2. **`D-step-30a-5-live-company-paid-evidence-pending-2026-05-18`** — *opened only if the live $1,000 paid Company-tier evidence has not been captured by the closing commit.* Same Option-1 pattern Step 30a.4 / 30a.2-pilot used. Closes at the next billing-touch window or when the founder's $1,000 buyer-surrogate Checkout completes against the live Stripe surface.

That is the full opened-set. Notably **no** new auth drift, **no** new schema drift beyond the additive `role` column, **no** new audit-chain drift.

## 8. Drifts NOT closed by this step (carried forward, named for symmetry)

- `D-magic-link-auth-cookie-session-2026-05-13` — Step 32a still owns the broader auth surface.
- `D-admin-audit-logs-actor-user-id-fk-missing-2026-05-13` — unchanged; the Domain-creation and department-lead-invite flows write audit rows with `actor_label="cookie:<admin-email>"` and the FK still absent.
- `D-tier-scope-mapping-service-layer-only-2026-05-13` — unchanged; the same gate now covers `requested_scope=domain` for self-serve creation as well as for `LucielInstance` minting.
- `D-rotation-procedure-laptop-dependent-2026-05-12` — unchanged; the prod deploy of this step runs from the founder's laptop using the runbook precedent.
- `D-vantagemind-dns-cloudfront-mismatch-2026-05-13` — unchanged.
- `D-celery-beat-single-replica-coupling-2026-05-14` — unchanged.

---

## 9. Tests

### 9.1 New contract tests (`tests/api/test_step30a_5_company_self_serve_shape.py`)

**~30 tests**, modelled on the Step 30a.4 closure's 28-contract-test pattern.

Coverage:
- `DomainSelfServeCreate` validation: slug regex (5 tests: valid, leading-dash, trailing-dash, uppercase, too-long); display_name length bounds (2 tests).
- `UserInviteCreate.role` Literal validation: teammate-ok, department_lead-ok, invalid-422 (3 tests).
- `POST /admin/domains/self-serve` happy path for Company tier (1 test).
- `POST /admin/domains/self-serve` returns 402 with `error.code='tier_scope_not_allowed'` for Individual tier (1 test).
- `POST /admin/domains/self-serve` returns 402 with `error.code='domain_cap_reached'` for Team tier (second-Domain attempt) (1 test).
- `POST /admin/domains/self-serve` returns 402 for Company tier when 50-Domain cap is reached (1 test).
- `POST /admin/domains/self-serve` returns 409 with `error.code='domain_slug_taken'` on duplicate slug (1 test).
- `POST /admin/domains/self-serve` returns 401 without a cookie (1 test).
- `POST /admin/domains/self-serve` writes one `admin_audit_log` row with `action=ACTION_DOMAIN_CREATED` and the expected `details` field-set (1 test).
- `POST /admin/invites` with `role='department_lead'` and a valid `domain_id` from the caller's tenant — happy path under Company tier (1 test).
- `POST /admin/invites` with `role='department_lead'` returns 402 under Team tier (1 test).
- `POST /admin/invites` with `role='department_lead'` and `domain_id=None` returns 422 (1 test).
- `POST /admin/invites` with `role='department_lead'` and a `domain_id` belonging to a different tenant returns 404 (1 test).
- `/auth/set-password` with `purpose='invite'` and a department-lead `UserInvite` provisions a `ScopeAssignment.role='department_lead'` under the invite's `domain_id` (1 test).
- `GET /admin/domains/self-serve` returns the caller's tenant's Domains with `pending_invites_count` and `active_agents_count` rollups, scope-isolated from sibling tenants (3 tests: own-tenant data present, sibling-tenant data absent, rollups correct).
- The `teammate_email` overload on `POST /admin/luciel-instances` returns 410 GONE (1 test — the removal that this step lands).
- `UserInvite` model serializes `role` correctly in `UserInviteRead` (1 test).
- `DomainConfig` rollup-count helper returns expected counts when Domain has 0 / N invites and 0 / N agents (3 tests).
- `InviteService.create_invite` does NOT auto-mint a domain-scope `LucielInstance` (regression guard on the §4.1 "auto-mint deliberately not done" decision) (1 test).

### 9.2 New live e2e harness (`tests/e2e/step_30a_5_live_e2e.py`)

Single scenario, **the §13.1 T1 walk end-to-end**:
1. A buyer-surrogate completes Stripe Checkout for the Company tier ($1,000 CAD intro, monthly cadence) against the live Stripe account.
2. The `checkout.session.completed` webhook mints the tenant, pre-mints the `company-luciel` tenant-scope Luciel, the `default` Domain, and the buyer's Agent; sends the welcome-set-password email.
3. The buyer redeems the welcome email, sets a password, lands cookied on `/dashboard`.
4. Test calls `POST /admin/domains/self-serve` twice (Sales + Marketing) → two new `DomainConfig` rows + two `admin_audit_log` rows.
5. Test calls `POST /admin/invites` four times (one department lead for Sales, one for Marketing, with `role='department_lead'` and the right `domain_id`; then under each lead's cookied session, two `role='teammate'` invites). **Six invite emails sent total, all marked observable in CloudWatch via the `[welcome-set-password-email]` log marker.**
6. Test redeems all six invites in sequence: two leads land cookied with `role='department_lead'` and the right Domain; four agents land cookied with `role='teammate'` under their respective lead's Domain.
7. Test asserts the final state: the Company admin's `GET /admin/domains/self-serve` returns two Domains with `active_agents_count=2` each (each lead's 2 agents) and `pending_invites_count=0`. The Step 31 `/api/v1/dashboard/tenant` returns the tenant rollup with 2 Domains, 6 active users, 4 active agents.

Env-gated on `STEP_30A_5_LIVE_E2E_ENABLED=1` + a Stripe test-mode key plus the live SES identity. Defaults off in CI.

### 9.3 Existing tests untouched

The 28 Step 30a.4 invite contract tests pass unchanged — the `role` field on `UserInviteCreate` defaults to `'teammate'`, every existing test path implicitly carries that default. The 46 Step 30a billing contract tests, the Step 30a.1 tier-scope contract tests, the Step 30a.3 auth tests, all unchanged. The single pre-existing failure tracked at `D-step-30a-billing-shape-test-moderation-config-failure-2026-05-13` is unchanged.

---

## 10. Execution sequence (when implementation starts)

1. **Backend PR** on `aryanonline/Luciel` branch `step-30a-5-company-self-serve`:
   - **C1:** Alembic migration `step30a_5_user_invite_role_and_audit_actions.py` + `UserInvite.role` column + the two new audit action constants in `AdminAuditLog.ACTION_WHITELIST`.
   - **C2:** `DomainSelfServeCreate` schema + `_enforce_self_serve_domain_create` + `DOMAIN_COUNT_CAP_BY_TIER` constant + service-layer `create_domain_self_serve` method on `AdminService`.
   - **C3:** Two new cookie-gated routes (`POST /admin/domains/self-serve`, `GET /admin/domains/self-serve`) in `app/api/v1/admin.py` alongside the Step 30a.4 invite routes; `_resolve_invite_actor` re-used unchanged.
   - **C4:** `InviteService.create_invite` `role='department_lead'` branch + `redeem_invite` honours `invite.role` to stamp `ScopeAssignment.role='department_lead'`.
   - **C5:** Removal of `teammate_email` overload on `POST /admin/luciel-instances` — branch deleted, 410 GONE returned for any payload that still carries `teammate_email`, deprecation log line removed.
   - **C6:** ~30 contract tests + live e2e harness.
2. **Website PR** on `aryanonline/Luciel-Website` branch `step-30a-5-company-self-serve`:
   - **W1:** `src/lib/admin.ts` — three new client functions per §5.2.
   - **W2:** `src/pages/Dashboard.tsx` — new `CompanyTab` component + tab-visibility gating on `subscription.tier === 'company'` + `TeamTab` inline-comment update.
   - **W3:** `src/pages/Pricing.tsx` — Company CTA refresh to "Start 90-day pilot for $1,000" + monthly/annual toggle threads through.
   - **W4:** Vitest tests for `CompanyTab` (form-validation, list-render, error-toast on 402/409, invite-department-lead flow) + the existing test suite stays green.
3. **Stripe configuration** (manual, founder runs) — no changes. The Company-tier intro Price ID was activated at the very-end Stripe-Prices sweep that closed `D-intro-fee-scaling-to-per-tier-2026-05-16` per the Step 30a.4 closure stanza. Step 30a.5 needs no new SSM puts and no new Stripe Prices.
4. **Prod deploy** (founder runs from laptop, runbook `docs/runbooks/step-30a-5-prod-deploy.md` authored fresh in this arc — mirrors the Step 30a.4 runbook shape):
   - Pre-flight: confirm Alembic head = `b4d8a2e7c1f3` (Step 30a.4 head).
   - Build + push image; register new task definitions; run migration; roll services.
   - Live smoke (the T1 walk) against prod with a fresh `aryans.www+30a5-smoke-…@gmail.com` buyer surrogate.
5. **Doc-truthing commit** — §6 matrix executed atomically across `aryanonline/Luciel` (CANONICAL_RECAP §12, ARCHITECTURE §3.2.13 amendments, DRIFTS §3 → §5 moves and new opens) and `aryanonline/Luciel-Website` (no canonical-doc moves; the website-repo doc-truth is the closing-tag stamp on `main`).
6. Closing tag `step-30a-5-company-self-serve-complete` on the doc-truthing commit in both repos.

---

## 11. Open questions reserved for after first read-back

A small set, deliberately surfaced for partner judgment before implementation begins:

1. **Domain cap for Company tier — 25 or 50? — RESOLVED 2026-05-18: 50.** Partner judgment locked the cap at 50, symmetric with `TIER_INSTANCE_CAPS[TIER_COMPANY]=50`. Rationale: one cap to remember, no artificial sales-touch threshold, and a Company customer who legitimately needs 50 Domains is exactly the kind of customer we want to serve without friction. The §14 instance cap remains the operational ceiling that forces the conversation; the Domain cap is a defensive bound, not a commercial gate.

2. **Domain-name slug constraints — soft or hard? — RESOLVED 2026-05-18: hard regex.** Partner entrusted judgment to me. Locking `^[a-z0-9][a-z0-9-]*[a-z0-9]$` at the Pydantic layer with a parallel `display_name` field (free text, max 64 chars) for human-readable presentation. Rationale: the slug appears in audit logs, URLs, and any future Domain-scoped subdomain we issue; loosening the constraint later is additive (always accepted), tightening it later is breaking (existing rows fail validation). The `display_name` field absorbs the user's natural language without polluting the slug.

3. **Pre-mint a domain-scope Luciel on Domain creation? — RESOLVED 2026-05-18: no.** Partner entrusted judgment to me. The Company-tier instance cap is 50 (§14); auto-minting against that cap creates phantom rows the customer didn't ask for, and the invited department lead may want a differently-configured Luciel anyway. Domain creation is organizational; Luciel creation stays explicit. If "empty Domain" friction shows up in support tickets we revisit, but the cost of being wrong here is one extra customer click — the cost of pre-minting wrong is a phantom row against a hard cap.

4. **Domain-cap-reached 402 messaging — RESOLVED 2026-05-18: disabled form + 402 backstop.** Partner entrusted judgment to me. At Team tier the "Create Domain" form renders disabled with an inline upgrade CTA pointing at the Company tier on Pricing.tsx; the user never gets to click and fail. At Company tier the form is enabled until the 50-Domain cap is hit, at which point the 402 toast surfaces *"Domain cap reached (50). Contact us to expand."* The 402 response stays as the API-level guard for direct callers (curl, future API clients) so the UI is a courtesy, not the only enforcement.

5. **`/dashboard` tab visibility — RESOLVED 2026-05-18: tier AND role.** Partner entrusted judgment to me. Locking the visibility contract:

   ```
   CompanyTab.visible := subscription.tier == 'company'
                         AND scope_assignment.role IN ('tenant_admin', 'owner')
   TeamTab.visible    := subscription.tier IN ('team', 'company')
                         AND scope_assignment.role IN ('tenant_admin', 'owner', 'department_lead')
   ```

   **Why this is the only safe choice.** A department lead's cookied session under a Company tenant has `subscription.tier == 'company'` (the tier is a tenant-wide property, not a per-user one). If CompanyTab gates on tier alone, every department lead under a Company tenant sees every Domain in the tenant the moment they sign in — a scope-boundary leak that violates §4.7's three-layer enforcement (cookie → scope_assignment → tenant_id filter). The fix is to gate the *UI surface* on the same `scope_assignment.role` that already gates the *data access*, so the surface and the data agree at the cookie layer.

   **Why I rejected the alternatives.**
   - *Gate on tier alone + hide Domains from leads at the API.* Splits the contract: the tab shows up, the data doesn't load, and the lead sees an empty state that looks like a bug.
   - *Introduce a new `is_tenant_admin` boolean on `users`.* Reinvents `scope_assignment.role`, creates two sources of truth, and forces a separate migration we don't need.
   - *Show the tab to leads but scope its data to their own Domain.* Conflates "company-wide org building" (CompanyTab's purpose) with "my team's roster" (TeamTab's purpose) — the leads already have TeamTab for their own Domain.

   **Implementation surface.** `Dashboard.tsx` reads `scope_assignment.role` from the existing `/api/v1/me` payload (already returned per Step 30a.4); no new endpoint. The CompanyTab component itself also enforces the check on mount as a defense-in-depth backstop in case a future change ever renders it conditionally without the gate. Tests in §10 are updated below to make the role check a first-class regression guard.

   **Test additions (added to §10).** Two new test cases:
   - `GET /dashboard` rendered for a department_lead under a Company tenant must NOT include CompanyTab in the response payload (1 test, frontend snapshot).
   - CompanyTab's own data endpoint (the Domain list) returns 403 if the caller's `scope_assignment.role NOT IN ('tenant_admin', 'owner')`, regardless of tier (1 test, backend).

All five open questions are now resolved. Implementation proceeds against this design as the source of truth; any deviation from it becomes a drift to log in DRIFTS §3.
