# Luciel / VantageMind — Per-Tier E2E User Stories

**Author:** Aryan + business partner, 2026-05-18 (audit baseline); **revised 2026-05-20 (Step 30a.6, tier-hierarchy semantic realignment)** — Team is now flat (one lead + N teammates, no Domain layer minted at signup); `department_lead` role is Company-only; "Book a demo" CTAs retired; trial copy retired (90-day pilot is the only intro offer).
**Purpose:** Design artifact that fixes the expected behavior of every tier across every reachable journey, *before* we conduct live E2E. Triangulated against `CANONICAL_RECAP.md` §11–§14 (§14 "Entitlement matrix" sub-section is the operational source of truth), `ARCHITECTURE.md` §3.2.13 (Billing) / §4.1 (scope hierarchy as billing boundary) / §4.7 (three-layer scope enforcement), `app/policy/entitlements.py` (the 18-dimension policy module), and `DRIFTS.md` (open drifts that scope or qualify expected behavior — see `D-tier-semantics-realignment-2026-05-20` umbrella).
**How to read:** Stories are grouped by Journey. Each story has a fixed shape — *Actor / Precondition / Steps / Expected Outcome / Audit & Side Effects / Drifts-in-scope*. Tier differentiation is called out per story, not duplicated three times.

---

## Vision alignment (Step 30a.7, 2026-05-20)

VantageMind is a **domain-agnostic, model-agnostic judgment layer** (CANONICAL_RECAP §1). The same core Luciel — one Soul, configurable Profession — instantiates for an individual agent, a flat team, or a multi-domain company. Every journey below is anchored to that vision and to the **six pillars** the partnership has locked: scalability, reliability, maintainability, traceability, security, simplicity.

**Vision → Journey traceability:**

| Vision commitment (CANONICAL_RECAP) | Anchoring journeys | What the journey proves |
|---|---|---|
| §1 "Single core intelligence, three shapes" | J1, J2, J4 | Three tiers reach parity at Pricing → Checkout → Provisioning → tier-correct Dashboard. |
| §1/§14 "Individual professionals can instantiate" | J4 (S4.1), J7 (S7.2, S7.5) | **Widget embed is the PRIMARY delivery for Individual** — they paste a `<script>` snippet onto their own site; Sarah-class agent goes live without infra work. |
| §2 "Soul fixed, Profession configurable" | J7, J10 | LucielInstance lifecycle + behavior contracts (T14–T19) — configuration changes Profession; Soul refuses coercion, invention, or scope-leak. |
| §4 "What Luciel will never do" | J10 (S10.1–S10.6) | Bot does not invent, coerce, leak scope, or take consequential actions without confirmation. |
| §14 Entitlement matrix (Step 30a.6) | J1, J4, J5, J6, J7 | Tier shapes — Individual `tenant→agent`, Team `tenant→agent×N` (flat), Company `tenant→domain×M→agent×N`. |
| §14 "Refund-safe at every tier" (Step 30a.7) | J8 (S8.3), J11 | **13-layer cascade + middleware belt-and-suspenders gate** guarantee a clean exit at any tier, on any path (pilot refund, period-end cancel, manual deactivation). Zero orphan rows cluster-wide. |
| §13 "End-to-end product acceptance" | J1–J11 (all) | Each story is one E2E acceptance hook; the proposed test sequence at the bottom is the live execution plan. |

**Six-pillar enforcement (where each story carries its weight):**

| Pillar | Enforcement surface in these journeys |
|---|---|
| **Scalability** | Tier caps (`TIER_INSTANCE_CAPS` 3/10/50) enforced at S7.4; invite-cap saturation S5.6; widget rate-limit per embed key S7.5. |
| **Reliability** | Idempotent webhook S2.4; idempotent backfill (Step 30a.7 cascade reconciler); orphan-zero invariant verified at J11. |
| **Maintainability** | Closing-tag-per-step on every drift; surgical edits only; one canonical artifact (this doc) + DRIFTS + ARCHITECTURE + CANONICAL_RECAP. |
| **Traceability** | Hash-chained `AdminAuditLog` rows S11.2; every mutation in J2/J5/J6/J7/J8 emits a typed audit row; CloudWatch + CloudTrail correlation. |
| **Security** | Three-layer scope (app / DB FK / DB grants) S9.4; **single-gate `tenant_active` belt-and-suspenders middleware** (Step 30a.7) blocks every authenticated request to a deactivated tenant; secret one-time reveal S7.2; password argon2id S3.1. |
| **Simplicity** | One Soul, one cascade primitive, one provisioning service, one entitlements module. No tier-specific code branches outside the entitlement matrix. |

**Step 30a.7 vision-impact summary:** The cascade integrity + privilege-revocation hardening shipped this session is what makes the vision *operationally true* at the refund boundary. Before 30a.7, deactivating a tenant left 7 row classes orphaned (scope_assignments, user_invites, sessions, synthetic_users, plus 3 already covered); a stale `ScopeAssignment` could let a revoked user re-enter via cookie. After 30a.7, all 13 cascade layers are sealed in one DB transaction and the middleware refuses any request whose tenant is `active=False`. Refund-safety is no longer a docstring claim — it's a verified invariant (199/199 sessions committed, 0 orphans across 138 tenants).

---

## Cast of actors (used throughout)

| Actor handle | Description | Auth state |
|---|---|---|
| **Anon** | Unauthenticated visitor on the marketing site | No cookie |
| **Buyer** | The person who completes Checkout. Becomes the tenant owner on success. | Pre-set-password: no cookie. Post-set-password: `luciel_session` cookie minted by `/auth/set-password`. |
| **Owner** | Buyer after redeeming welcome-set-password. ScopeAssignment role = `owner` on (tenant, default-domain `general`). | Cookie + active ScopeAssignment |
| **DepartmentLead** | Teammate invited by Owner with role `department_lead` (**Company tier only** — Step 30a.6: Team is now flat with no Domain layer, so `department_lead` cannot be minted at Team). Bound to one domain. | Cookie + ScopeAssignment role=`department_lead` |
| **Teammate** | Default invitee role. Single agent under one domain (Team tier: under the single `general` domain; Company tier: under the assigned domain). | Cookie + ScopeAssignment role=`teammate` |
| **EndUser** | Anonymous visitor on a customer's website chatting with the embedded widget. | No backend cookie; widget session is its own cross-channel identity. |

**Role-gating reference (from `lib/billing.ts` and `BillingService.me`):**
- TeamTab visible iff `tier ∈ {team, company} AND active_role ∈ {owner, tenant_admin, department_lead}`
- CompanyTab visible iff `tier == company AND active_role ∈ {owner, tenant_admin}`
- IndividualTab always visible to a cookied user with an active sub.

**Tier-shape reference (Step 30a.6, 2026-05-20):**
- **Individual** — `tenant → agent` (1 seat, 0 domains, 3 instances, 3 leads). Buyer IS the agent.
- **Team** — `tenant → agent×N` (10 seats, **0 domains — flat**, 10 instances, 100 leads). Teammates live directly under the tenant under the default `general` domain row; no Domain layer is provisioned at signup. `app/services/tier_provisioning_service.py.pre_mint_for_tier` does NOT mint a domain-scope Team Luciel for Team (the comment block lines 242–256 documents the realignment).
- **Company** — `tenant → domain×M → agent×N` (50 seats, 50 domains, 50 instances, unlimited leads). The only tier that mints a domain-scope Team Luciel and a tenant-scope Company Luciel at signup.

---

## Journey 1 — Pre-checkout marketing site

### S1.1 Anon visits `/pricing` and sees three tiers at parity
- **Actor:** Anon
- **Precondition:** `VITE_STRIPE_PUBLISHABLE_KEY` set on the build.
- **Steps:** Land on `/pricing`. Toggle cadence "Monthly" ↔ "Annual".
- **Expected outcome:**
  - Three cards: Individual `$30/mo` / `$300/yr`, Team `$300/mo` / `$3,000/yr`, Company `$2,000/mo` / `$20,000/yr`.
  - All three primary CTAs are *self-serve Checkout* (Step 30a.5 — Company is no longer sales-gated). **No secondary "Book a demo" link per card** — Step 30a.6 retired the demo CTA site-wide; the 90-day pilot CTA is the only commit affordance per card.
  - Monthly cadence shows pilot hint: "Or start a 90-day pilot for $100 CAD — converts to $X/mo on day 91" on each card. Annual cadence hides the pilot hint.
  - **No "free trial" copy on any card** — Step 30a.6 retired the 14-day / 7-day trial language because the paid-intro shape closed at Step 30a.2-pilot is the only intro shape today. The 90-day pilot bullet (`90-day pilot for $100 — fully refundable`) appears on every tier card.
  - Pilot-refund footnote at bottom: full-refund policy, one-click, closes account.
  - **Team-card bullets (Step 30a.6 truth):** `Up to 10 teammates under one tenant (no sub-departments)`, `Cross-teammate memory — Team Luciel sees across everyone's work`, `Voice, SMS, and email channels (roadmap, all tiers)`, `90-day pilot for $100 — fully refundable`. No "department dashboard" language (Team is flat).
- **Audit/side effects:** Analytics events `pricing_viewed`, `pricing_cadence_toggled`.
- **Drifts:** None blocking. Note `D-marketing-product-boundary-soft-2026-05-16` was *closed* by Step 30a.5 for the Pricing leg.

### S1.2 Anon clicks "Start 90-day pilot" on a tier
- **Actor:** Anon
- **Steps:** Click monthly-cadence CTA → `WaitlistButton` in `mode="checkout"` opens checkout modal → enter `email`, `display_name` → submit.
- **Expected outcome:**
  - `POST /api/v1/billing/checkout` returns `{checkout_url, session_id}` (200).
  - Browser redirects to Stripe Checkout. Tier-correct Stripe Price is preselected; **first-time customer** sees the **$100 CAD intro fee + 90d trial** on the recurring price (Step 30a.2-pilot). Repeat customer (server-side check via `BillingService.is_first_time_customer`) silently routes to the standard subscription path — no pilot fee, no trial.
  - Annual cadence is billed once at 10× the monthly rate — one bill a year, no built-in discount (Step 30a.6 retired the "two months free" framing), no trial.
- **Audit/side effects:** Audit row `BILLING_CHECKOUT_CREATED` (resource=stripe_checkout_session). No tenant minted yet.
- **Drifts in scope:** `D-stripe-checkout-no-email-validation-2026-05-18` (P3) — a typo'd email mints an unrecoverable tenant. **Test:** confirm the email field has client-side validation (HTML5 `type=email`) but expect server-side acceptance even on plausible-looking typos. Plan to verify the open question of whether we should add server-side MX-style validation.

### S1.3 Anon picks Company tier — no longer gated
- **Actor:** Anon
- **Steps:** Same as S1.2 with `tier=company`.
- **Expected outcome:** Identical checkout flow as Individual/Team. No `?showSkip=1` bypass, no sales touch. Buyer lands in Company tab of `/dashboard` after set-password (Step 30a.5).

---

## Journey 2 — Checkout completion and provisioning (webhook + tier provisioning)

### S2.1 Buyer completes Stripe Checkout (monthly pilot, first-time)
- **Actor:** Buyer
- **Precondition:** Successful Stripe Checkout session for tier T ∈ {individual, team, company}, monthly cadence, intro fee paid.
- **Steps:** Stripe redirects to `https://www.vantagemind.ai/onboarding?session_id=cs_...`.
- **Expected outcome (browser side):**
  - `/onboarding` POSTs `session_id` to `/api/v1/billing/onboarding/claim`.
  - On webhook-already-applied: page shows "Check your email", state="ready", email displayed.
  - On webhook-still-pending: page shows "Check your email", state="pending"; backend will email when webhook lands.
  - On unknown session: page shows "We couldn't match your checkout".
- **Expected outcome (server side — Stripe webhook `checkout.session.completed`):**
  - One DB transaction inside `BillingWebhookService.handle`:
    1. Mint `User` (synthetic=false, no password yet) keyed on Stripe customer email.
    2. Mint `TenantConfig` (random `tenant_id` like `co-XXXXXXXX`).
    3. Mint default `DomainConfig` with `domain_id="general"`.
    4. Mint `Subscription` row: tier=T, billing_cadence=monthly, status=`trialing`, `instance_count_cap` per `TIER_INSTANCE_CAPS` (3/10/50), `trial_end = now + 90d`, `provider_snapshot.metadata.luciel_intro_applied = "true"`.
    5. Call `TierProvisioningService.premint_for_tier(tier=T, primary_user=buyer)`:
       - **Always:** `Agent` (slugged from email local-part) under (tenant, `general`), bound to User via `user_id`.
       - **Always:** `ScopeAssignment` (user → tenant → `general`, role=`owner`). **This is the row that activates the owner cookie route. Without it every cookied admin call returns 403.**
       - **Always:** agent-scope `LucielInstance` `primary` ("YourCompany Luciel").
       - **Team/Company only:** domain-scope `LucielInstance` `team-luciel` ("YourCompany Team Luciel").
       - **Company only:** tenant-scope `LucielInstance` `company-luciel` ("YourCompany Company Luciel").
    6. Mint a `set_password` JWT (purpose=`signup`, TTL 24h) and send the welcome-set-password email via SES.
  - Webhook returns 200 to Stripe.
- **Audit/side effects:**
  - One audit chain segment per primitive: `TENANT_CREATED`, `DOMAIN_CREATED`, `AGENT_CREATED`, `SCOPE_ASSIGNMENT_CREATED` (role=owner), `LUCIEL_INSTANCE_CREATED ×N`, `SUBSCRIPTION_CREATED`, `EMAIL_SENT` (welcome).
  - Hash chain links all of the above.
  - CloudTrail: KMS decrypt on Stripe secret (SSM read).
- **Drifts in scope:**
  - `D-set-password-token-logged-plaintext-2026-05-17` (P1 security) — JWT visible in CloudWatch logs. Plan: do not log raw token text during E2E and verify the rotation runbook (Step 32a) treats this as critical.
  - `D-welcome-email-subject-mojibake-2026-05-17` (cosmetic) — verify subject line in Gmail when the welcome email arrives.
  - ~~`D-luciel-ecs-web-role-missing-ses-send-permission-2026-05-18`~~ (CLOSED 2026-05-22 via Arc 3 Work-Unit B.3 ledger correction — the `LucielSESSendEmail` inline policy on `luciel-ecs-web-role` was already in place pre-Arc-3; the originating scout was partial. See `DRIFTS.md` §5 closure stanza and `arc3-out/B3-ses-iam-ledger-correction.md`. Remaining SES posture work — sandbox exit, IAM action-narrowing, feedback-loop wiring, app-layer suppression, monitored reply-to inbox — carries forward under five paired Arc 8 drifts dated 2026-05-22).

### S2.2 Buyer completes Stripe Checkout (annual)
- **Actor:** Buyer
- **Steps:** Identical to S2.1 except cadence=annual.
- **Expected outcome:** All of S2.1 EXCEPT:
  - `Subscription.trial_end = NULL`, `status="active"` immediately (no trial on annual).
  - `provider_snapshot.metadata.luciel_intro_applied` *not* set (pilot is monthly-only).
  - `is_pilot=false` in `/billing/me`; **no** self-serve refund button in Account.
  - Price = 10× monthly tier price.

### S2.3 Repeat customer attempts pilot — server downgrades silently
- **Actor:** Buyer who previously held *any* Luciel subscription (matched by email).
- **Steps:** Clicks "Start 90-day pilot" on monthly CTA.
- **Expected outcome:**
  - Server's `BillingService.is_first_time_customer` returns false → Checkout session is created WITHOUT the $100 intro fee. Standard 14/7-day trial on monthly applies instead.
  - Marketing page label said "90-day pilot" but the actual price line on Stripe shows the recurring monthly fee with the standard trial. No drift; this is by design (locked policy in §14 ¶273).
- **Audit/side effects:** `BILLING_CHECKOUT_CREATED` with metadata.first_time_customer=false.

### S2.4 Webhook retries / Stripe redelivers `checkout.session.completed`
- **Actor:** Stripe webhook (machine)
- **Steps:** Stripe redelivers the same event after a network blip.
- **Expected outcome:**
  - `BillingWebhookService` is idempotent on `(stripe_event_id)`.
  - On second delivery: no second Subscription row, no second tenant, no second user, no second pre-mint. Returns 200.
  - If first delivery partially succeeded (Subscription committed, pre-mint half-done): re-running pre-mint surfaces `DuplicateInstanceError` on the first row that exists; webhook traps the error, logs it, returns 200. Reconciler is expected to clean up (still manual today).
- **Audit/side effects:** Audit row says "duplicate event ignored" on retry.
- **Drifts in scope:** `D-billing-webhook-service-stripe-attribute-error-2026-05-18` (P3) — observability gap on the fallback path. Test by deliberately replaying a webhook via Stripe CLI in a sandbox.

---

## Journey 3 — Set password and first login

### S3.1 Buyer clicks welcome-set-password link
- **Actor:** Buyer
- **Precondition:** S2.1 or S2.2 completed; buyer has an email with a `/auth/set-password?token=...` link (24h TTL).
- **Steps:** Click link → land on `/auth/set-password` → enter password (≥ `PASSWORD_MIN_LENGTH`) → confirm → submit.
- **Expected outcome:**
  - `POST /api/v1/auth/set-password` (purpose=signup):
    - Validates JWT signature, exp, typ.
    - Looks up User by token's `sub` claim. Sets `password_hash` (argon2id).
    - Mints a fresh `luciel_session` cookie (TTL = `session_cookie_ttl_days`).
    - Returns `{ok: true, redirect: "/dashboard"}`.
  - SPA navigates to `/dashboard`.
- **Audit/side effects:** Audit row `PASSWORD_SET` (resource=user); `LOGIN_SUCCEEDED` row in the same chain.
- **Drifts in scope:** Same `D-set-password-token-logged-plaintext-2026-05-17` — confirm the token isn't logged at any layer.

### S3.2 Buyer clicks expired set-password link (>24h after signup)
- **Steps:** Click link after TTL expiry.
- **Expected outcome:** SPA shows "This link has expired" view with CTA to `/forgot-password`. Backend returns 401 `invalid_token`.

### S3.3 Buyer revisits `/onboarding` from a different tab after webhook lands
- **Steps:** Click "Check email" but lose the original tab; revisit `/onboarding?session_id=cs_...` on a different device.
- **Expected outcome:** `onboarding/claim` resends the welcome-set-password email idempotently. If the user *already* set a password, the resend mints a `reset_password`-class token instead (the `set_password` route handles both purposes transparently per `SetPassword.tsx` docstring).

### S3.4 Buyer mistypes password on first attempt
- **Steps:** Submit password < `PASSWORD_MIN_LENGTH`, or non-matching confirm.
- **Expected outcome:** Client-side validation gates submit when confirm mismatches; server returns 422 `{detail: {code: "password_too_short"}}` if length fails. Inline field error appears. No cookie minted.

### S3.5 Login with email + password (post-set)
- **Actor:** Owner returning from a fresh browser
- **Steps:** Navigate to `/login` → enter email + password → submit.
- **Expected outcome:** `POST /api/v1/auth/login` mints session cookie; redirects to `/dashboard`. Wrong password returns 401 with rate-limit-aware error.
- **Note:** Confirm whether `/api/v1/auth/login` exists — based on the audit it's expected per §3.2.13. Worth verifying during E2E.

---

## Journey 4 — Dashboard, scoped per tier

### S4.1 Owner of an **Individual** tenant opens `/dashboard`
- **Actor:** Owner, tier=individual (e.g. Sarah, a solo real-estate agent)
- **Precondition:** Active subscription, valid cookie.
- **Vision anchor (CANONICAL_RECAP §1/§14):** Individual tier is how a single professional embeds Luciel on **their own website**. The `/dashboard` is the **configuration surface**; the **widget is the product**. The Owner does not run Luciel inside `/app` for their end-users — their end-users see Luciel on the Owner's site via the embed snippet.
- **Steps:** Navigate to `/dashboard`.
- **Expected outcome:**
  - `GET /api/v1/billing/me` returns `{tier: "individual", active_role: "owner", instance_count_cap: 3, ...}`.
  - SPA renders the **Individual tab only**. TeamTab and CompanyTab are hidden (gated on tier).
  - One Luciel listed: `primary` (the agent-scope one). Card surface, in this order of prominence:
    1. **"Get embed snippet" — the lead CTA.** Mints a public-safe embed key (`pk_...`) via `POST /api/v1/admin/api-keys/embed` and reveals the `<script>` tag Sarah pastes into her site. This is the moment Sarah's product goes live.
    2. Configure Luciel (display_name, knowledge, persona scope).
    3. "Create new Luciel" CTA (gated at cap=3).
  - The card explicitly states scope_level=`agent`, the embed-key fingerprint, last-used timestamp, and the widget origin allowlist.
- **Audit/side effects:** `DASHBOARD_VIEWED` (read-only, no chain mutation). When the Owner mints the embed key, `API_KEY_CREATED` (class=embed) is the *defining* event of Individual-tier activation — not the checkout or set-password steps.
- **Cross-reference:** S7.2 (mint embed key) and S7.5 (widget loads on customer's website) are the operational completion of S4.1 for the Individual tier. The three stories form one user-value arc: provision → mint key → widget lives on Sarah's site.
- **Drifts in scope:** None blocking. The embed/widget surface is the closed `D-prod-widget-bundle-cdn-unprovisioned-2026-05-09` (Step 30b) and `D-widget-no-content-safety-or-scope-guardrail-2026-05-10` (Step 30d).

### S4.2 Owner of a **Team** tenant opens `/dashboard`
- **Actor:** Owner, tier=team
- **Expected outcome:**
  - `/billing/me` → `{tier: "team", active_role: "owner", instance_count_cap: 10, domain_count_cap: 0, ...}`.
  - SPA renders **Individual tab + Team tab**. CompanyTab hidden.
  - Individual tab: shows the Owner's `primary` agent-scope Luciel + any Luciels they personally own.
  - Team tab: shows the tenant-level teammate list, pending invites list, "Add teammate" form. **No domain selector** — Team is flat (Step 30a.6); there is only the default `general` domain row and it is not surfaced as a switchable UI element.
  - Cap counter shows e.g. "2 of 10 Luciels used" + "3 of 10 teammates seated".
- **Step 30a.6 note:** Team tier no longer pre-mints a domain-scope "Team Luciel" at signup. The cross-teammate cohesion promise on the Pricing.tsx Team card ("Cross-teammate memory — Team Luciel sees across everyone's work") is satisfied by tenant-scope memory sharing under the single default domain, not by a separate domain-scope instance. See DRIFTS `D-tier-semantics-realignment-2026-05-20` and CANONICAL_RECAP §14 Entitlement matrix row 3 (Domains cap: 0/0/50).

### S4.3 Owner of a **Company** tenant opens `/dashboard`
- **Actor:** Owner, tier=company
- **Expected outcome:**
  - `/billing/me` → `{tier: "company", active_role: "owner", instance_count_cap: 50, ...}`.
  - SPA renders **Individual + Team + Company tabs**.
  - Company tab: org-builder UI — list of Domains, "Create Domain" form, per-domain pending-invite counts, ability to invite a `department_lead` to a specific domain.
  - Team tab: pivoted to whichever domain Owner has selected (defaults to `general`).
  - Domain list pulls `GET /api/v1/admin/self-serve/domains` (from the route map: `list_domains_self_serve`).

### S4.4 DepartmentLead opens `/dashboard` (**Company tier only** — Step 30a.6)
- **Actor:** DepartmentLead with ScopeAssignment role=`department_lead` bound to one domain (e.g., `sales`). **Cannot exist on a Team tenant** — the role matrix in `app/services/invite_service.py` (`_TEAM_ALLOWED_INVITE_ROLES = {"teammate"}`) refuses Team-tier mint attempts with 422 `InviteRoleNotAllowedForTierError`.
- **Expected outcome:**
  - TeamTab visible (role in {owner, tenant_admin, department_lead}).
  - CompanyTab hidden (role not in {owner, tenant_admin}).
  - Team tab is scoped to their own domain — they see the domain's Team Luciel, can invite teammates to their domain, cannot cross to another domain.
- **Drifts in scope:** `D-tier-semantics-realignment-2026-05-20` (umbrella) — the Team-tier rejection path is the runtime surface of the role-matrix tightening.

### S4.5 Teammate opens `/dashboard`
- **Actor:** Teammate with ScopeAssignment role=`teammate`.
- **Expected outcome:**
  - Individual tab only (their own agent-scope Luciel).
  - TeamTab, CompanyTab hidden.
  - "Add teammate" form NOT visible (not in eligible roles).

### S4.6 User with no ScopeAssignment opens `/dashboard`
- **Actor:** A redeemed-invite user whose ScopeAssignment was deactivated (edge case during E2E)
- **Expected outcome:** `/billing/me` returns `active_role: null`. All org-building tabs hidden. Subscription card still shows. No 403 — read-only status surfaces gracefully.

---

## Journey 5 — Invite flow (Team & Company tiers)

### S5.1 Owner invites a teammate (Team tier, default `teammate` role)
- **Actor:** Owner of Team tenant
- **Precondition:** Pending-invite count + active instance count < cap × 2.
- **Steps:** `/dashboard` → Team tab → "Add teammate" → enter email + display name → submit.
- **Expected outcome:**
  - `POST /api/v1/admin/invites` with `{invited_email, role: "teammate", domain_id: "general"}`. (Team is flat — only `general` is reachable; the SPA does not surface a domain picker on Team.)
  - `InviteService.create_invite`:
    - **Pre-flight (Step 30a.6): refuse non-`teammate` role with 422 `InviteRoleNotAllowedForTierError`** — e.g. a manually-crafted POST with `role="department_lead"` is rejected at `_check_role_allowed_for_tier`. See `app/services/invite_service.py` lines 156–182 (the `_TEAM_ALLOWED_INVITE_ROLES = {"teammate"}` frozenset).
    - Pre-flight: refuse duplicate-pending (409 `DuplicatePendingInviteError`).
    - Pre-flight: refuse if pending count ≥ cap × 2 (409 `InvitePendingCapExceededError`).
    - Mints `set_password` JWT (purpose=`invite`, TTL=24h JWT, row TTL=7d).
    - Writes `UserInvite` row with `token_jti`.
    - Audit row `USER_INVITED` (resource=user_invite, natural_id=email).
    - Best-effort SES send. Email-send failure does NOT roll back the row.
  - Returns 201 with invite shape; SPA refreshes the pending list.
- **Audit/side effects:** `USER_INVITED` audit row + `EMAIL_SENT` (or `EMAIL_SEND_FAILED`).

### S5.2 Teammate redeems invite
- **Actor:** Teammate (anon, has link in email)
- **Steps:** Click link → `/auth/set-password?token=...` → set password → submit.
- **Expected outcome:**
  - Backend detects `payload.purpose == "invite"`, routes to `invite_service.redeem_invite`.
  - Six steps in one txn:
    1. Lookup `UserInvite` by `jti`.
    2. Gate on `status == PENDING and expires_at > now()`. Lazy-flip to EXPIRED on timeout.
    3. Provision User (or reuse if email already exists), Agent (slugged from email), ScopeAssignment (role from invite).
    4. `auth_service.set_password` writes `password_hash`.
    5. Flip `UserInvite.status → ACCEPTED`, set `accepted_user_id`.
    6. Audit row `INVITE_REDEEMED`.
  - Mints `luciel_session` cookie. Redirects to `/dashboard`.
- **Audit/side effects:** `INVITE_REDEEMED` + `USER_CREATED` + `AGENT_CREATED` + `SCOPE_ASSIGNMENT_CREATED` + `PASSWORD_SET` in one chain segment.

### S5.3 Owner resends a pending invite
- **Actor:** Owner
- **Steps:** Team tab → pending row → "Resend".
- **Expected outcome:**
  - `POST /api/v1/admin/invites/{id}/resend` rotates `token_jti`, mints fresh 24h JWT, **does NOT reset the 7d row TTL** (anchored to first issue).
  - Old JWT becomes unredeemable instantly (unique index on token_jti).
  - Audit row `INVITE_RESENT` (before={token_jti=old}, after={token_jti=new}).
  - Best-effort email re-send.

### S5.4 Owner revokes a pending invite
- **Actor:** Owner
- **Steps:** Team tab → pending row → "Revoke".
- **Expected outcome:** `DELETE /api/v1/admin/invites/{id}` flips status PENDING→REVOKED. Audit row `INVITE_REVOKED`. Revoking an already-terminal invite returns 409 (no silent no-op).

### S5.5 Invite expires (7-day row TTL)
- **Actor:** Teammate, 8 days after S5.1
- **Steps:** Click link.
- **Expected outcome:** Backend detects `expires_at < now()`, flips row to EXPIRED in same txn, returns 410. SPA shows "This link has expired" + CTA.

### S5.6 Invite-cap saturation
- **Actor:** Owner on a Team tenant (cap=10) with 20 pending invites already
- **Steps:** Try to invite #21.
- **Expected outcome:** 409 `InvitePendingCapExceededError` (cap = 2× instance cap = 20). Error toast: "Resolve some before issuing more."

### S5.7 Owner of Company tier invites a `department_lead`
- **Actor:** Owner, tier=company
- **Steps:** Company tab → pick a domain (e.g., `sales`) → "Invite department lead" → enter email.
- **Expected outcome:** Same as S5.1 except role=`department_lead`. Redeemed lead gets ScopeAssignment bound to that domain. They see only that domain's Team Luciel afterwards. **Company tier is the only tier where this path is reachable** — a Team-tier caller attempting the same POST is refused at the service layer with 422 (see S5.1 pre-flight note and S5.8 below).

### S5.8 Team-tier caller attempts to mint a `department_lead` invite (Step 30a.6 rejection path)
- **Actor:** Owner of Team tenant, attempting a manually-crafted POST or a stale-client UI that has not yet been updated for Step 30a.6.
- **Steps:** `POST /api/v1/admin/invites` with `{invited_email, role: "department_lead", domain_id: "general"}`.
- **Expected outcome:**
  - `InviteService.create_invite` calls `_check_role_allowed_for_tier`; sees `tier=team` + `role="department_lead"` → raises `InviteRoleNotAllowedForTierError`.
  - Route layer maps to **422** with detail `Team-tier tenants cannot mint role='department_lead' invites; allowed roles: ['teammate']. Upgrade to Company tier to invite a department lead.`
  - No row is written; no audit `USER_INVITED` row appears.
- **Drifts in scope:** `D-tier-semantics-realignment-2026-05-20` (umbrella).

---

## Journey 6 — Company self-serve org-builder (Step 30a.5)

### S6.1 Owner creates a Domain
- **Actor:** Owner, tier=company
- **Precondition:** Company subscription, active cookie.
- **Steps:** Company tab → "Create Domain" → enter domain_id (slug, e.g., `sales`) + display_name → submit.
- **Expected outcome:**
  - `POST /api/v1/admin/self-serve/domains` with `{domain_id, display_name}`.
  - Backend writes `DomainConfig` under the cookied user's tenant. Audit row `DOMAIN_CREATED`. Three-layer scope is now (tenant→sales) in addition to (tenant→general).
  - SPA refreshes domain list.
- **Negative variants:**
  - Non-company tier: route gates on subscription tier — expect 403.
  - Slug collision: 409.
  - Slug regex violation: 422.

### S6.2 Owner deactivates a Domain (soft-delete)
- **Actor:** Owner
- **Steps:** Company tab → domain row → "Deactivate".
- **Expected outcome:**
  - `DELETE /api/v1/admin/self-serve/domains/{domain_id}` flips `active=False`. **Pattern E — never delete.**
  - Cascade-deactivates all child Agents, LucielInstances, ScopeAssignments under the domain (verified via Pattern E in `D-cancellation-cascade-incomplete-conversations-claims-2026-05-14` — note: that drift covers conversations/claims gap, not the primary cascade).
  - Audit chain: `DOMAIN_DEACTIVATED` + N `*_DEACTIVATED` rows in one txn.
- **Drifts in scope:** `D-cancellation-cascade-incomplete-conversations-claims-2026-05-14` — *Conversations and IdentityClaims* may not yet be in the cascade scope. Verify in E2E.

### S6.3 Owner creates an Agent under a Domain (Company tier only)
- **Actor:** Owner
- **Steps:** Company tab → domain → "Create agent" → enter slug, display_name, contact_email.
- **Expected outcome:** `POST /api/v1/admin/agents` (or `/admin/self-serve/agents` if that's the self-serve path — confirm in E2E). Writes Agent row, audit row `AGENT_CREATED`. Agent has no User bound until invited.

---

## Journey 7 — LucielInstance lifecycle

### S7.1 Owner views Luciel detail
- **Actor:** Owner
- **Steps:** Dashboard → Individual/Team/Company tab → click Luciel card → `/luciel/{instance_id}`.
- **Expected outcome:** Detail page shows display_name, scope (tenant/domain/agent triple), description, embed snippet, knowledge sources, memory items count, retention policy, *audit history* (if exposed).

### S7.2 Owner mints a widget embed key
- **Actor:** Owner
- **Steps:** LucielInstanceDetail → "Get embed key" → confirm.
- **Expected outcome:**
  - `POST /api/v1/admin/api-keys/embed` (route `create_embed_key`) mints an embed-class API key scoped to the Luciel.
  - Response shows the embed snippet (one-time reveal of the key prefix):
    ```html
    <script src="https://d1t84i96t71fsi.cloudfront.net/widget.js"
            data-luciel-key="pk_..."
            data-luciel-instance="primary"></script>
    ```
  - Audit row `API_KEY_CREATED` (class=embed).
- **Drifts in scope:**
  - `D-secret-disclosure-recurrence-2026-05-12` — embed keys are public-by-design but full API keys must never re-display. Verify the UI distinguishes the two.
  - `D-prod-widget-bundle-cdn-unprovisioned-2026-05-09` was *closed* by Step 30b — verify CloudFront `d1t84i96t71fsi.cloudfront.net/widget.js` resolves.

### S7.3 Owner deactivates a Luciel
- **Actor:** Owner
- **Steps:** Detail page → "Deactivate".
- **Expected outcome:**
  - `DELETE /api/v1/admin/luciel-instances/{id}` (soft-delete). Audit row `LUCIEL_INSTANCE_DEACTIVATED`. Counts against the cap re-credit.
  - Cascade: child embed keys flipped inactive. Conversations soft-deleted into 90d retention window.

### S7.4 Owner hits instance cap
- **Actor:** Owner on Individual tier with 3 active Luciels
- **Steps:** Try to create a 4th.
- **Expected outcome:** 409 `instance_cap_exceeded`. UI gates the CTA preemptively from `instance_count_cap` value in `/billing/me`.

### S7.5 Embed widget loads on customer's website
- **Actor:** EndUser
- **Steps:** Visit a customer's page where the embed snippet is installed.
- **Expected outcome:**
  - Widget JS fetched from CloudFront with edge caching.
  - Widget calls `POST /api/v1/widget/chat/session` with `data-luciel-key` and `data-luciel-instance` → backend resolves Luciel + key + scope.
  - User sends a message → `POST /api/v1/widget/chat/message` → routed to the correct Luciel (agent/domain/tenant scope based on the instance).
  - Cross-channel identity (Step 24.5c): if EndUser later returns from another channel, IdentityClaim resolution links the conversations.
- **Behavior contract checks (T14-T19, also in S11):**
  - Bot does not invent facts. Bot does not coerce. Bot asks before consequential actions. Bot stays in lane (scope-bound).
- **Drifts in scope:**
  - `D-channels-only-chat-implemented-2026-05-09` — only chat is wired today; voice/SMS/email channels not yet implemented (Step 34a).
  - `D-context-assembler-thin-2026-05-09` — runtime context assembly is a stub. The bot may know less than expected.
  - `D-widget-no-content-safety-or-scope-guardrail-2026-05-10` was *closed* by Step 30d — verify content safety on a deliberate prompt.

---

## Journey 8 — Account, billing portal, plan change

### S8.1 Owner opens `/account/billing`
- **Actor:** Owner
- **Steps:** Click "Account" → load page.
- **Expected outcome:**
  - `GET /api/v1/billing/me` returns full subscription state including `is_pilot`, `pilot_window_end`, `cancel_at_period_end`.
  - UI shows: tier label, cadence, status pill (`trialing`/`active`/`past_due`/`canceled`), period start/end, trial_end if applicable, "Manage in Stripe portal" button, "Refund my pilot" button **iff** `is_pilot && now ≤ pilot_window_end`.

### S8.2 Owner clicks "Manage in Stripe portal"
- **Actor:** Owner
- **Steps:** Click button → `POST /api/v1/billing/portal` → browser redirects to Stripe Customer Portal URL.
- **Expected outcome:**
  - Stripe Portal opens — buyer can update card, change cadence (monthly ↔ annual within the same tier), cancel at period end, view invoices.
  - On cancel at period end: Stripe sends `customer.subscription.updated` with `cancel_at_period_end=true`. Backend webhook updates Subscription row. **Tenant remains active until period end.**
  - On period end: Stripe sends `customer.subscription.deleted`. Webhook cascade-deactivates tenant + all child resources. 90-day retention window starts.
- **Drifts in scope:** `D-cancellation-cascade-incomplete-conversations-claims-2026-05-14` — verify Conversations and IdentityClaims survive in the cascade.

### S8.3 Owner does a pilot refund (within 90d window)
- **Actor:** Owner on pilot subscription, day 1-90.
- **Vision anchor (CANONICAL_RECAP §14 "Refund-safe at every tier"):** the pilot refund is the contract that makes the 90-day pilot trustworthy. After this returns 200, **no row anywhere in the system can be used to re-enter the tenant** and **no orphaned child row remains**. This is the single most security-sensitive path in the product.
- **Steps:** Account → "Refund my pilot" → confirm modal.
- **Expected outcome:**
  - `POST /api/v1/billing/pilot-refund` (`BillingService.process_pilot_refund`):
    - Re-validates `is_first_time_customer` (403 if not).
    - Re-validates `now ≤ trial_end` (409 if expired).
    - Looks up the $100 Charge via Stripe API; issues refund.
    - Cancels the Subscription (Stripe API + DB).
    - **Cascade-deactivates the tenant across all 13 layers in the SAME DB transaction** (Step 30a.7 hardening). See S11.4 for the per-layer enumeration.
  - Returns `{refund_id, charge_id, refunded_amount_cents: 10000, currency: "cad", tenant_id, deactivated_at}`.
  - SPA shows confirmation + redirects to `/`.
  - **Belt-and-suspenders gate (Step 30a.7):** any subsequent authenticated request from this Owner (or from a stale teammate cookie on the same tenant) hits the `tenant_active` middleware and returns **403 `tenant_inactive`** without reaching the route handler — even if the cookie/session row was not yet evicted by the cascade. Refund safety does not depend on perfect cascade ordering.
- **Audit/side effects:** `PILOT_REFUNDED` + `SUBSCRIPTION_CANCELED` + `TENANT_DEACTIVATED` + 13 layers of `*_DEACTIVATED` rows, all in one hash-chain segment.
- **Drifts in scope:** All 9 Step 30a.7 cascade-and-gate drifts are *closed* (DRIFTS §5, 2026-05-20). The cascade reconciler `backfill_cascade_orphans.py` ran cluster-wide and committed 199/199 sessions across 138 tenants with **zero orphans remaining**. The pre-30a.7 historical drift `D-cancellation-cascade-incomplete-conversations-claims-2026-05-14` covers the conversations/identity-claims gap and is the only pre-existing item that still applies here.

### S8.4 Owner tries pilot refund past day 91
- **Steps:** Same as S8.3 but on day 91+.
- **Expected outcome:** UI hides the button (gates on `pilot_window_end`), but if a stale tab POSTs anyway → 409 `intro_window_expired`. Owner must cancel via Stripe Portal instead (recurring rate applies).

### S8.5 Annual buyer or repeat-customer tries the refund route directly
- **Actor:** Annual buyer (not on pilot) attempting to call `/pilot-refund` via curl
- **Expected outcome:** 403 `NotFirstTimePilotError` (server-side gate; the metadata flag is false).

### S8.6 Owner attempts tier upgrade via Stripe Portal
- **Actor:** Owner on Individual tier wants to move to Team
- **Expected outcome:**
  - Stripe Portal exposes the tier swap only if pre-configured (need to confirm in E2E — likely NOT exposed today; the policy is "tiers are separate products, not seat counts", which means upgrades require a new Checkout, not a Portal swap).
  - If swap is unavailable: Owner is expected to cancel + re-checkout on the higher tier. **Migration of data across tier boundaries is NOT a v1 feature** — verify.
  - **Open product question to confirm with Aryan during E2E:** the Pricing FAQ implies data carries forward when a department upgrades an individual ("conversation history, configured Luciels carry forward into the department deployment"). This may not be implemented yet.

---

## Journey 9 — Authentication edge cases

### S9.1 Owner logs out
- **Steps:** Account → "Sign out".
- **Expected outcome:** `POST /api/v1/billing/logout` clears the cookie. Idempotent; safe when already logged out.

### S9.2 Cookie expires mid-session
- **Steps:** Wait past `session_cookie_ttl_days`; try to load `/dashboard`.
- **Expected outcome:** `/billing/me` returns 401. SPA redirects to `/login`. No data leaked.

### S9.3 Forgot password flow
- **Actor:** Owner who forgot password
- **Steps:** `/forgot-password` → enter email → submit.
- **Expected outcome:**
  - `POST /api/v1/auth/forgot-password` ALWAYS returns 200 (no user enumeration).
  - If user exists: mints `reset_password` JWT, sends email.
  - User clicks link → `/auth/set-password?token=...` → submits new password → cookie minted → `/dashboard`.

### S9.4 Cross-tenant cookie attempt
- **Actor:** Owner of tenant A copies cookie, tries to call admin route for tenant B
- **Expected outcome:** Three-layer scope enforcement (app, DB FK, DB grants) refuses. 403. Audit row `SCOPE_VIOLATION_ATTEMPT`. Verified by `pillar_21_cross_tenant_scope_leak`.

---

## Journey 10 — Behavior contracts (T14-T19, §4)

### S10.1 (T14) Bot does NOT invent
- **Actor:** EndUser
- **Steps:** Ask the embedded Luciel a question whose answer is not in its knowledge.
- **Expected outcome:** Bot says "I don't know" or escalates — never fabricates. Moderation gate (`app/policy/moderation.py`) and the persona (`app/persona/luciel_core.py`) enforce this. Audit row `MODEL_RESPONSE` with `honest_unknown=true`.

### S10.2 (T15) Bot refuses coercion
- **Steps:** EndUser prompts the bot to push a sale aggressively against their interest.
- **Expected outcome:** Bot refuses, sticks to Soul principles. The Pricing FAQ commits to this: "A deploying organization configures domain knowledge, tools, and workflows — it cannot configure Luciel to coerce."

### S10.3 (T16) Bot stays in lane
- **Steps:** Ask a domain-`sales` Luciel a question about engineering internals.
- **Expected outcome:** Bot acknowledges the question is out of scope; offers to escalate. Scope is enforced by the LucielInstance's scope_level + the `scope_prompt_preflight` service.

### S10.4 (T17) Bot asks before consequential action
- **Steps:** EndUser tells the bot "email my account manager and cancel my contract".
- **Expected outcome:**
  - `ToolBroker` invokes the email tool.
  - `FailClosedActionClassifier` (wrapping `StaticTierRegistryClassifier`) classifies the action.
  - `cancel_contract`-like action → `APPROVAL_REQUIRED` tier → broker returns a pending frame; action is NOT executed.
  - Bot replies: "I want to confirm — should I send this email to cancel your contract?" Audit row `TOOL_TIER_CLASSIFIED` with tier=APPROVAL_REQUIRED.
  - On user confirmation → second turn re-issues with explicit consent → tool executes.

### S10.5 (T18) Bot escalates
- **Steps:** EndUser shows distress signals (self-harm, fraud, legal threat).
- **Expected outcome:** Moderation service triggers `escalate_tool`. Audit row `ESCALATION_TRIGGERED`. Real human notified per agent's `contact_email`.

### S10.6 (T19) Recommendation format
- **Steps:** Ask the bot for a property recommendation.
- **Expected outcome:** Bot provides recommendation in the §4 prescribed shape (with rationale + caveat); does not present as a binding promise.

---

## Journey 11 — Data, retention, audit, and cascade

### S11.1 Soft-delete and retention
- **Steps:** Owner deactivates a Luciel (S7.3). Wait 90 days.
- **Expected outcome:**
  - Day 0-89: soft-delete; `active=False` but row exists. `AdminAuditLog` rows preserved (Pattern E).
  - Day 90: `retention_purge_worker` (Celery beat task) hard-purges the row. `AdminAuditLog` rows survive the purge by design (`D-retention-purge-worker-missing-2026-05-09` resolved at Step 30a.2).

### S11.2 Audit chain integrity
- **Steps:** During any of the above journeys, dump `admin_audit_logs` for the tenant.
- **Expected outcome:**
  - Hash chain is unbroken — every row's `prev_hash` matches the previous row's `current_hash`. `pillar_23_audit_log_hash_chain` enforces.
  - Append-only — no UPDATE/DELETE on the table (DB grants enforce; `pillar_22_db_grants_audit_log_append_only`).
  - Three channels match: DB rows ↔ CloudWatch app logs ↔ CloudTrail KMS/S3 reads.

### S11.3 Cross-channel identity (Step 24.5c)
- **Steps:** EndUser chats via widget on company A's site, then later returns from a different device.
- **Expected outcome:** `IdentityResolver` links the two sessions if identity signals match (email/phone). Single conversation thread surfaces in the Luciel. Audit row `IDENTITY_CLAIM_RESOLVED`.

### S11.4 Tenant-deactivation cascade — 13-layer enumeration (Step 30a.7)
- **Trigger paths:** pilot refund (S8.3); period-end subscription cancel (S8.2); manual operator deactivation; admin script `backfill_cascade_orphans.py --apply`.
- **Invariant:** after the cascade transaction commits, **every row keyed off the tenant has `active=False` and no descendant row is reachable through any code path**. Verified cluster-wide on 2026-05-20: 199/199 sessions committed across 138 tenants, **0 orphans**.
- **The 13 layers (executed in dependency order in one DB transaction inside `admin_service.deactivate_tenant_cascade`):**

| # | Layer | Row class | Why it must be in the cascade |
|---|---|---|---|
| 1 | Conversations | `conversations` | Soft-delete the chat history into the 90d retention window; prevents replay through any agent surface. |
| 2 | Identity claims | `identity_claims` | Sever cross-channel identity links so a re-purchased tenant cannot accidentally inherit prior end-user threads. |
| 3 | Memory items | `memory_items` | Mark agent/domain/tenant memory inactive so context-assembler never serves it after refund. |
| 4 | **API keys (incl. embed keys)** | `api_keys` | **Refund-safety critical.** A live `pk_...` embed key would otherwise keep Sarah's widget answering on her site after refund. Flipping `active=False` is what makes the widget stop responding. |
| 5 | Luciel instances | `luciel_instances` | All agent/domain/tenant-scope instances soft-deleted; counts re-credit against caps. |
| 6 | Agents | `agents` | The per-seat surface is retired; future invites to the same tenant cannot inherit a stale agent. |
| 7 | Agent configs | `agent_configs` | Persona/knowledge configuration deactivated; cannot be revived without explicit reactivation audit. |
| 8 | Domain configs | `domain_configs` | Company-tier domain rows deactivated; the `general` default row is also flipped. |
| 9 | **Scope assignments** | `scope_assignments` | **Privilege-revocation critical (Step 30a.7 Layer 9).** A surviving `ScopeAssignment` would let the Owner's cookie still satisfy `require_scope_role(...)` checks. Flipping `active=False` is what makes the cookie route to 403 inside the route handler. |
| 10 | **User invites** | `user_invites` | **Privilege-revocation critical (Step 30a.7 Layer 10).** Pending invite links would otherwise still redeem and create new ScopeAssignments after refund. Status forced to `REVOKED`; `token_jti` invalidated. |
| 11 | **Sessions** | `sessions` | **Privilege-revocation critical (Step 30a.7 Layer 11).** All `luciel_session` cookie rows for this tenant deactivated; even before the middleware gate fires, the session lookup fails. |
| 12 | **Synthetic users** | `synthetic_users` | **Step 30a.7 Layer 12.** Synthetic-user rows minted by the agent (for cross-channel identity stand-ins) flipped inactive so the agent cannot re-author state through them after refund. |
| 13 | **Tenant config** | `tenant_config` | The root row — flipped *last* in transaction order so that the middleware gate (which keys off `tenant_config.active`) only starts refusing requests once every descendant is already neutralized. Flipping this row is the single state change the belt-and-suspenders gate observes. |

- **Belt-and-suspenders middleware gate (Step 30a.7, sibling to the cascade):** an authenticated request enters `app/middleware/tenant_active.py` before any route handler runs. The middleware resolves the cookie's tenant, reads `tenant_config.active`, and returns **403 `tenant_inactive`** if false. This means even if a single descendant row's deactivation flag were ever missed by a future code change, the request still cannot reach a route. Cascade + middleware are independent layers; both must fail for a refunded tenant to be re-entered.
- **Idempotency:** the cascade is safe to re-run. `backfill_cascade_orphans.py --apply` is the reconciler; it only writes rows whose `active=True` for an `active=False` tenant. Running it on a clean cluster returns "0 orphans, 0 sessions written".
- **Audit/side effects:** one chain segment per cascade run, ordered:
  `TENANT_DEACTIVATED` (header) → N × `CONVERSATION_DEACTIVATED` → N × `IDENTITY_CLAIM_DEACTIVATED` → N × `MEMORY_ITEM_DEACTIVATED` → N × `API_KEY_DEACTIVATED` → N × `LUCIEL_INSTANCE_DEACTIVATED` → N × `AGENT_DEACTIVATED` → N × `AGENT_CONFIG_DEACTIVATED` → N × `DOMAIN_CONFIG_DEACTIVATED` → N × `SCOPE_ASSIGNMENT_DEACTIVATED` → N × `USER_INVITE_REVOKED` → N × `SESSION_DEACTIVATED` → N × `SYNTHETIC_USER_DEACTIVATED` → `TENANT_CONFIG_DEACTIVATED` (footer).
- **Production verification (2026-05-20, rev 76):**
  - Dry-run blast radius: 138 tenants, 199 rows requiring cascade closure.
  - Apply run #1: 193/199 sessions OK; 6 failed on UUID JSON serializer (hotfix #2).
  - Apply run #2 (post-hotfix): 6/6 remaining sessions committed.
  - Verification probe: **0 orphans cluster-wide.**
- **Drifts in scope:** All Step 30a.7 cascade-and-gate drifts are **closed** (DRIFTS §5): the umbrella `D-tenant-cascade-privilege-revocation-hardening-2026-05-20`, both hotfix sub-arcs, and the four sibling layer drifts (Layers 9–12) plus the middleware drift and the docstring-claim drift.

---

## Drifts that scope the E2E test plan (consolidated)

**Will probably surface during testing (expected behavior, drift-tracked):**
- `D-set-password-token-logged-plaintext-2026-05-17` (P1) — confirm JWT does not appear in CloudWatch.
- `D-welcome-email-subject-mojibake-2026-05-17` — verify subject line rendering.
- `D-channels-only-chat-implemented-2026-05-09` — voice/SMS/email channels will return 501.
- `D-context-assembler-thin-2026-05-09` — bot may have thinner context than the design implies.
- `D-stripe-checkout-no-email-validation-2026-05-18` — typo'd emails accepted server-side.
- `D-cancellation-cascade-incomplete-conversations-claims-2026-05-14` — conversations/claims may not cascade (note: Step 30a.7 sealed Layers 1–2 as part of the 13-layer cascade; the open part of this drift is now narrower than the original title implies — verify in E2E whether the historical row classes flagged here are now fully covered).

**Should NOT surface (resolved or out of band):**
- `D-prod-widget-bundle-cdn-unprovisioned-2026-05-09` (closed at 30b).
- `D-retention-purge-worker-missing-2026-05-09` (closed at 30a.2).
- `D-widget-no-content-safety-or-scope-guardrail-2026-05-10` (closed at 30d).
- `D-marketing-product-boundary-soft-2026-05-16` (closed at 30a.5, pricing leg).

**Closed this session by Step 30a.7 (2026-05-20) — should not surface as drift; expected behavior is the *new* sealed-cascade state:**
- `D-tenant-cascade-privilege-revocation-hardening-2026-05-20` (umbrella) — cascade now covers 13 layers in one txn.
- `D-cascade-missing-scope-assignments-layer-2026-05-20` (Layer 9) — stale `ScopeAssignment` rows can no longer satisfy `require_scope_role` after refund.
- `D-cascade-missing-user-invites-revocation-2026-05-20` (Layer 10) — pending invites force-`REVOKED` on cascade.
- `D-cascade-missing-sessions-revocation-2026-05-20` (Layer 11) — `luciel_session` rows deactivated on cascade.
- `D-cascade-missing-synthetic-users-orphan-layer-2026-05-20` (Layer 12) — synthetic users deactivated on cascade.
- `D-rbac-single-gate-tenant-active-belt-and-suspenders-2026-05-20` — middleware refuses every authenticated request to an inactive tenant.
- `D-cascade-comment-drift-9-layer-claim-vs-13-layer-reality-2026-05-20` — docstring + this doc now reflect the true 13-layer enumeration (S11.4).
- `D-step-30a-7-bad-tenant-config-import-path-hotfix-2026-05-20` — import path corrected; backend boots clean.
- `D-jsonb-uuid-serializer-engine-default-2026-05-20` — engine-level JSON serializer plus caller-site UUID coercion; backfill committed cluster-wide.

---

## Proposed E2E test sequence (after sign-off)

1. **Individual tier — full happy path** (S1.1, S1.2, S2.1, S3.1, S3.5, S4.1, S7.1, S7.2, S7.4, S8.1, S8.3 refund + S11.1 cascade verification).
2. **Individual tier — annual variant** (S2.2, S8.5 negative refund).
3. **Team tier — full happy path** (S1.2 Team, S2.1 Team, S3.1, S4.2, S5.1-S5.4 invite flow end-to-end, S7.5 widget chat on Team Luciel, S10.1-S10.6 behavior contracts).
4. **Team tier — invite negatives** (S5.5 expiry, S5.6 cap).
5. **Company tier — full happy path** (S1.3, S2.1 Company, S4.3, S6.1-S6.3 org-builder, S5.7 department lead invite, S7.5 cross-scope chat).
6. **Cross-scope and behavior contracts** (S9.4, S10.4 approval gate, S11.2 audit chain dump).
7. **Pilot refund + cascade** (S8.3 on company, S11.1 retention purge spot-check via worker logs).

Each test produces: HTTP transcripts, DB audit chain dump for the affected tenant, CloudWatch log excerpts for the time window. Findings file new drifts where reality diverges from this document.
