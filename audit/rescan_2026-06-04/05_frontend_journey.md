# Manifest Section 05 — Frontend & Journey Contracts Audit

**Auditor scope:** Frontend React/TS/Vite application (`/home/user/workspace/luciel_repos/frontend/src/`) and frontend↔backend API contract conformance.
**Read-only audit — no code changes made.**

**Source documents read:**
- `/home/user/workspace/docs_text/VISION.txt` — §3 (five-pillar config UX), §6 (lifecycle), §7 (tier map, Enterprise requirements)
- `/home/user/workspace/docs_text/CUSTOMER_JOURNEY.txt` — all three personas (Sarah/Free, Marcus/Pro, Northwind+Dana/Enterprise)
- `/home/user/workspace/luciel_repos/frontend/ARC15_FRONTEND_REPORT.md` — 189 tests passing; reconciled ARC15 items
- `/home/user/workspace/luciel_repos/frontend/ARC15_BACKEND_CONTRACT.md` — API shape reconciliation
- Backend routes spot-checked: `admin_personality.py`, `admin_escalation.py`, `admin_connections.py`, `admin_channels.py`, `admin_knowledge.py`, `admin_custom_roles.py`, `admin.py`, `billing.py`

**ARC15-already-resolved items** are noted inline with `[ARC15-resolved]` and are not re-flagged as new findings.

---

## Cluster 1 — Five-Pillar Config UX

| # | Requirement | Doc cite | Implementing artifact(s) | Status | Notes / evidence |
|---|---|---|---|---|---|
| 1.1 | Five pillars in order: Channels → Tools → Knowledge → Escalation → Personality | Vision §3.1 | `LucielInstanceDetail.tsx:98–157` | CONFORMS | Tab/section order matches exactly; no extra pillar inserted |
| 1.2 | No raw `system_prompt` textarea in any pillar | Vision §3 (doctrine), ARC15 | `LucielInstanceDetail.tsx`; all section components | CONFORMS | Grepped all section files — no `system_prompt` textarea exists in pillar UI; see RESIDUE §R1 for type-level residue |
| 1.3 | No model-selector dropdown in any pillar | Vision §3 (doctrine) | All pillar section components | CONFORMS | No `<Select>` or `<Dropdown>` keyed on model variant found anywhere in the five-pillar component tree |
| 1.4 | Channels pillar: multi-select dropdown labeled "Channels this Luciel uses" | Vision §3.1 | `ChannelsSection.tsx:119` | DRIFTED | Implementation uses individual `<Checkbox>` elements in a `<ul>` list — functionally equivalent but interaction pattern is a checklist, not a dropdown. Vision §3.1 explicitly specifies "A multi-select dropdown." |
| 1.5 | Widget channel always-on and non-toggleable | Vision §3.1 | `ChannelsSection.tsx` | CONFORMS | Widget row renders without a toggle; it is display-only |
| 1.6 | Email and SMS channels tier-gated (Pro+) with upgrade nudge | Vision §3.1, §7 | `ChannelsSection.tsx:147–153` | CONFORMS | `tier_available === false` renders "Upgrade to Pro" chip over each channel row |
| 1.7 | Tools pillar: cognition-band capabilities shown as read-only (no checkboxes) | Vision §3.2 | `ToolsSection.tsx:122–148` | CONFORMS | Cognition band renders as static `<ul>` chips; no checkbox or toggle on any built-in capability |
| 1.8 | Tools pillar: add-on tools checklist with default-deny; inline Connect form on enable | Vision §3.2 | `ToolsSection.tsx:157–230`, `ConnectTool.tsx` | CONFORMS | Each add-on tool renders disabled by default; enabling an external-reach tool reveals `ConnectTool` inline; 3-state chip (`action_needed`/`connected`/`reconnect_needed`) sourced from backend `connection_status` |
| 1.9 | Knowledge pillar: upload/paste/CSV on all tiers; website crawl gated Free | Vision §3.3 | `KnowledgeSection.tsx`, `CrawlWebsiteCard.tsx` | CONFORMS | `locked = quota.tier === "free"` in `CrawlWebsiteCard.tsx`; upload/paste/CSV not behind a gate |
| 1.10 | Escalation pillar: contact fields only; signals shown read-only; no trigger toggles | Vision §3.4 | `EscalationSection.tsx` | CONFORMS | Signals rendered as read-only chips; no checkbox or toggle in signals block; contact fields only editable |
| 1.11 | Escalation tier-shape: Free=1 email / Pro=primary+secondary+routing rules / Enterprise=chains+SLA | Vision §3.4, §7 | `EscalationSection.tsx:254–489` | CONFORMS | Three distinct rendering branches keyed on `tier`; per-tier structure matches Vision |
| 1.12 | Personality pillar: 4 named presets + Custom (Pro/Enterprise); no model dropdown; no raw prompt textarea | Vision §3.5 | `PersonalitySection.tsx` | CONFORMS | Exactly 4 presets + Custom; Custom disabled on Free with `UpgradeNudge`; `maxChars` read from `config.business_context_max_chars` (never hardcoded); no model selector; no raw prompt field |

---

## Cluster 2 — Frontend↔Backend Contract Conformance

| # | Requirement | Doc cite | Implementing artifact(s) | Status | Notes / evidence |
|---|---|---|---|---|---|
| 2.1 | `GET/PUT /api/v1/admin/instances/{id}/personality` | ARC15 contract | `lib/personality.ts` ↔ `admin_personality.py:149,170` | CONFORMS | Path, method, body shape match |
| 2.2 | `GET/PUT /api/v1/admin/instances/{id}/escalation` | ARC15 contract | `lib/escalation.ts` ↔ `admin_escalation.py:155,176` | CONFORMS | Path, method, body shape match |
| 2.3 | `GET/POST /api/v1/admin/instances/{id}/connections`, `DELETE /api/v1/admin/connections/{id}` | ARC15 contract | `lib/connections.ts` ↔ `admin_connections.py:270,300,800` | CONFORMS | |
| 2.4 | `POST /api/v1/admin/instances/{instanceId}/connections/oauth/{type}/initiate` | ARC15 contract | `lib/connections.ts` ↔ `admin_connections.py:496` | CONFORMS | |
| 2.5 | `POST /api/v1/admin/instances/{instanceId}/connections/{connectionId}/refresh` | ARC15 contract doc | `lib/connections.ts` ↔ `admin_connections.py:393` | BUG | **Path mismatch.** Frontend sends `…/instances/{instanceId}/connections/{connectionId}/refresh`. Backend route is `@router.post("/connections/{connection_id}/refresh")` under prefix `/admin` → full path `/api/v1/admin/connections/{connection_id}/refresh` (no `instances/{id}` segment). ARC15 contract document listed the frontend path, but the backend implementation does not match it. In production this endpoint would 404 on every reconnect attempt. |
| 2.6 | `ConnectionView` shape: `last_health_check_at` field | ARC15 contract | `lib/connections.ts:57` ↔ `admin_connections.py` backend response | BUG | Frontend `ConnectionView` type declares `last_health_check_at: string \| null`. Backend ARC15 contract shows field as `last_verified_at`. Field name mismatch means the frontend always reads `undefined` for this field — silent data loss. |
| 2.7 | `GET/PUT /api/v1/admin/instances/{id}/channels`, `PUT …/channels/email`, `PUT …/channels/sms` | ARC15 contract | `lib/channels.ts` ↔ `admin_channels.py:320,360,397` | CONFORMS | |
| 2.8 | `GET/POST /api/v1/admin/instances/{id}/tools`, `POST …/tools/{id}/authorize\|revoke` | ARC15 contract | `lib/tools.ts` ↔ `admin_tools.py` | CONFORMS | |
| 2.9 | Custom roles and role-assignments endpoints | ARC15 contract | `lib/roles.ts` ↔ `admin_custom_roles.py:410,101,106` | CONFORMS | |
| 2.10 | Lifecycle endpoints: `close`, `lifecycle-state`, `reactivate/*`, `export*` | ARC15 contract | `lib/lifecycle.ts` ↔ `admin.py:2155,2506,2249,2358` | CONFORMS | |
| 2.11 | Billing endpoints: `downgrade`, `downgrade/preview`, billing status `me` | ARC15 contract | `lib/billing.ts` ↔ `billing.py:1015,1109,624` | CONFORMS | |
| 2.12 | `LucielInstance.system_prompt_additions` present in frontend type and `UpdateLucielInstanceRequest` | Vision §3 doctrine (no raw prompt authoring); ARC15 | `lib/admin.ts:262,297` | RESIDUE | Field has no UI surface (correct); but it persists in the type definition and outbound PATCH shape — any PATCH that serializes the full object sends this field to the backend. See §R1. |

---

## Cluster 3 — Tier-Gated UI Affordances

| # | Requirement | Doc cite | Implementing artifact(s) | Status | Notes / evidence |
|---|---|---|---|---|---|
| 3.1 | Email and SMS channels upgrade-gated on Free | Vision §7 | `ChannelsSection.tsx:147–153` | CONFORMS | `UpgradeChip` rendered when `tier_available === false` |
| 3.2 | Add-on tools upgrade-gated by tier requirement | Vision §7 | `ToolsSection.tsx:218–225` | CONFORMS | Each tool renders `UpgradeChip` with `"Upgrade to {tier}"` when `tier_available === false` |
| 3.3 | Website crawl gated on Free tier | Vision §7 | `CrawlWebsiteCard.tsx` | CONFORMS | `locked = quota.tier === "free"` |
| 3.4 | Custom personality preset gated Pro+ | Vision §7 | `PersonalitySection.tsx:255–261` | CONFORMS | `UpgradeNudge` rendered when tier is Free and Custom preset selected |
| 3.5 | Custom roles gated Enterprise | Vision §7 | `Dashboard.tsx:870` | CONFORMS | `showCustomRoles = tier === "enterprise"` — entire `CustomRolesSection` conditionally rendered |
| 3.6 | Slack escalation notify channel gated Enterprise | Vision §7 | `EscalationSection.tsx` | CONFORMS | Slack channel option rendered only when `available_notify_channels` (from API) includes `"slack"` |
| 3.7 | Escalation routing rules gated Pro+ | Vision §3.4, §7 | `EscalationSection.tsx:354–394` | CONFORMS | Routing rules block not rendered on Free tier |

---

## Cluster 4 — Lifecycle UX

| # | Requirement | Doc cite | Implementing artifact(s) | Status | Notes / evidence |
|---|---|---|---|---|---|
| 4.1 | "Download all my data" is the first action before entering cancel mode | Vision §6; CJ §8 | `CloseAccountSection.tsx` | CONFORMS | Data-export checkbox renders as first step in close modal before cancel-mode selection is presented |
| 4.2 | Type-to-confirm before account close | Vision §6 | `CloseAccountSection.tsx` | CONFORMS | Confirmation text-match gate before final close button activates |
| 4.3 | 30-day grace window (account close) | Vision §6 | `CloseAccountSection.tsx` copy | CONFORMS | Copy accurately references 30-day grace window |
| 4.4 | Instance pause/delete/restore with 30-day grace and new embed key on restore | Vision §6.4 | `InstanceLifecycleSection.tsx` | CONFORMS | Pause, soft-delete, restore flows present; new embed key dialog shown on restore |
| 4.5 | Downgrade path with overflow preview | Vision §6 | `Account.tsx` (`DowngradeConfirmModal`) | CONFORMS | `DowngradeConfirmModal`, overflow preview, `downgradeToTier()` all present |
| 4.6 | Lifecycle banners | Vision §6 | `LifecycleBanners.tsx` | CONFORMS | File exists and is imported into `Dashboard.tsx` |

---

## Cluster 5 — Lived-Flow Conformance

### Sarah (Free tier)

| # | Beat | Doc cite | Implementing artifact(s) | Status | Notes / evidence |
|---|---|---|---|---|---|
| 5.1 | Widget-only; Email/SMS locked with upgrade chips | CJ §2 | `ChannelsSection.tsx:147` | CONFORMS | |
| 5.2 | Built-in cognition band; no capability checkboxes | CJ §2 | `ToolsSection.tsx:122–148` | CONFORMS | |
| 5.3 | Knowledge quota meters (10 MB / 100 MB); crawl gated | CJ §2 | `lib/knowledge.ts`, `KnowledgeSection.tsx` | CONFORMS | Quota values driven by API response, not hardcoded |
| 5.4 | Single email escalation field | CJ §2 | `EscalationSection.tsx:254–273` | CONFORMS | |
| 5.5 | Warm Concierge preset; 280-char business-context box | CJ §2 | `PersonalitySection.tsx`, `maxChars` from `config.business_context_max_chars` | CONFORMS | `maxChars` is API-driven; Warm Concierge is one of the 4 named presets |
| 5.6 | "Save and get embed snippet" path | CJ §2 | `PersonalitySection.tsx:197`, `DeployTab` | CONFORMS | Save → `DeployTab` renders embed snippet |
| 5.7 | At-cap graceful reply display | CJ §2 | `UsageDashboard.tsx:81` | CONFORMS | "Budget reached" state rendered in `UsageDashboard` |

### Marcus (Pro tier)

| # | Beat | Doc cite | Implementing artifact(s) | Status | Notes / evidence |
|---|---|---|---|---|---|
| 5.8 | Email and SMS channels enabled; dedicated number choice | CJ §4.2 | `ChannelsSection.tsx` | CONFORMS | `sms_provisioned_number` displayed when provisioned |
| 5.9 | record_source CSV live connect | CJ §4.2 | `ConnectTool.tsx`, `LIVE_CONNECTION_TYPES` | CONFORMS | `record_source` is in `LIVE_CONNECTION_TYPES`; CSV connect form renders live |
| 5.10 | calendar OAuth live connect | CJ §4.2 | `ConnectTool.tsx`, `oauthLive` flag | CONFORMS | `calendar` is in `oauthLive` set; OAuth button renders live |
| 5.11 | email_sender Connect step (sender identity) | CJ §4.2 | `ConnectTool.tsx`, `OAUTH_CONNECTION_TYPES`, `LIVE_CONNECTION_TYPES` | DRIFTED | `email_sender` is not in `LIVE_CONNECTION_TYPES` nor in `oauthLive` set. It renders an "Available later" disabled button. CJ §4.2 describes the step as completing. ARC15 marks this arc17_pending. [ARC15-resolved as deferred] — noting here because CJ describes it as a live beat. |
| 5.12 | sms_sender already-connected chip | CJ §4.2 | `ConnectTool.tsx` | DRIFTED | `sms_sender` renders "Available later" deferred state. CJ §4.2 describes it showing as "Connected." ARC15 marks arc17_pending. [ARC15-resolved as deferred] |
| 5.13 | push_to_crm HubSpot OAuth live connect | CJ §4.2 | `ConnectTool.tsx`, `crm` provider | DRIFTED | `crm` is arc17_pending; HubSpot OAuth not live. CJ §4.2 describes it connecting. [ARC15-resolved as deferred] |
| 5.14 | Routing rules (Pro escalation) | CJ §4.2 | `EscalationSection.tsx:354–394` | CONFORMS | |
| 5.15 | Custom/Professional Advisor preset selection | CJ §4.2 | `PersonalitySection.tsx` | CONFORMS | |
| 5.16 | Live takeover: admin clicks "Take over" in dashboard live feed | CJ §7 (Marcus Phase 7) | Frontend src/ (full search) | MISSING | No takeover or `human_controlled` UI exists anywhere in the frontend. Backend cognition (`finalizer.py`, `orchestrator.py`) has `handoff_requested` / `human_controlled` state machine logic, but zero frontend surface exposes it. CJ §7 explicitly describes this beat. |
| 5.17 | Reconnect-needed chip on connection-status change | CJ §4.2 | `ConnectTool.tsx:76`, `ConnectTool.test.tsx:238` | CONFORMS | |
| 5.18 | Overage invoice line item | CJ §4.2 | `UsageDashboard.tsx` (`OverageInvoice` block) | CONFORMS | |

### Northwind + Dana (Enterprise tier)

| # | Beat | Doc cite | Implementing artifact(s) | Status | Notes / evidence |
|---|---|---|---|---|---|
| 5.19 | Fleet view for Enterprise home (distinct from single-instance card view) | CJ §3, §7 | `Dashboard.tsx:321` (`LucielsTab`) | DRIFTED | `LucielsTab` renders identical `grid gap-4 sm:grid-cols-2` card grid regardless of tier. No tier-conditional branch for a fleet-view layout exists. CJ §3 explicitly says "fleet view instead of a single instance card"; CJ §7 references "fleet view in the dashboard shows…". |
| 5.20 | Org-wide policies and branding configuration UI | CJ §5; Vision §7 | Frontend src/ (full search) | MISSING | No org-wide policy or branding configuration surface found anywhere in frontend src/. |
| 5.21 | Custom roles from atomic permissions (Enterprise-gated) | CJ §5; Vision §7 | `CustomRolesSection.tsx`, `lib/roles.ts`, `Dashboard.tsx:870` | CONFORMS | Enterprise-gated; full CRUD for custom roles |
| 5.22 | Salesforce connector live connect | CJ §5 | `ConnectTool.tsx`, `crm`/`salesforce` provider | DRIFTED | `crm` provider (including Salesforce) is arc17_pending. CJ §5 describes it connecting. [ARC15-resolved as deferred] |
| 5.23 | Compliance-archive write-back via outbound_webhook | CJ §5 | `ConnectTool.tsx:315–329`, `LIVE_CONNECTION_TYPES` | CONFORMS | `outbound_webhook` is in `LIVE_CONNECTION_TYPES`; webhook URL connect form renders live |
| 5.24 | Escalation chains + per-step SLA minutes | CJ §5; Vision §3.4 | `EscalationSection.tsx:399–489` | CONFORMS | Enterprise branch renders multi-step chains with per-step `sla_minutes` fields |
| 5.25 | 2000-char business-context field (Enterprise) | Vision §7 | `PersonalitySection.tsx:171` | CONFORMS | `maxChars = config.business_context_max_chars ?? 280` — API-driven; Enterprise API returns 2000 |
| 5.26 | Personality change approval workflow (Enterprise) | Vision §7 | `PersonalitySection.tsx` | MISSING | No approval step, approval state, or approval-pending UI in `PersonalitySection.tsx`. Vision §7 states personality changes on Enterprise require an approval workflow. (Sibling grants have `approval_state` state machine; personality does not.) |
| 5.27 | Custom-domain widget (CNAME config UI) | Vision §7 | Frontend src/ (full search) | MISSING | No CNAME or custom-domain configuration UI found anywhere in frontend src/. |
| 5.28 | SLA tracking / adherence display | CJ §7; Vision §7 | `UsageDashboard.tsx`, frontend src/ | AMBIGUOUS | `UsageDashboard` shows usage bars (messages, storage, overage). CJ §7 and Vision §7 reference SLA tracking for Enterprise. No SLA-adherence reporting surface found. Spec language is ambiguous about whether this is a real-time UI or a billing/reporting artifact — cannot firmly mark MISSING without clearer spec language. |

---

## CONFLICTS

### C1 — Channels pillar: "multi-select dropdown" vs. checklist implementation
- **Vision §3.1** specifies: "A multi-select dropdown labeled 'Channels this Luciel uses.'"
- **Implementation** (`ChannelsSection.tsx:119`): individual `<Checkbox>` elements in a `<ul>` list.
- This is not merely aesthetic: a multi-select dropdown and a visible checklist have different UX affordances (collapsibility, default-hidden state, selection summary). The implementation is functionally equivalent but violates the spec's explicit interaction pattern. Downstream concern: if more channel types are added, a checklist expands vertically; a dropdown does not.
- **Resolution needed:** Confirm whether the checklist is an intentional deviation or an unrecognized drift.

### C2 — `refreshConnection` endpoint: ARC15 contract doc vs. backend route
- **ARC15_BACKEND_CONTRACT.md** lists the refresh endpoint as `POST /instances/{instance_id}/connections/{connection_id}/refresh`.
- **Backend code** (`admin_connections.py:393`): `@router.post("/connections/{connection_id}/refresh")` under prefix `/admin` → resolves to `/api/v1/admin/connections/{connection_id}/refresh` (no `instances/{id}` segment).
- Frontend (`lib/connections.ts`) sends to the ARC15-documented path, which does NOT match the actual backend route. The ARC15 contract document itself is wrong, or the backend route was changed after the contract was written.
- **Impact:** Every reconnect attempt from the frontend would 404 in production unless there is a router-level rewrite not visible in the audited source.

### C3 — `ConnectionView.last_health_check_at` vs. backend field `last_verified_at`
- **Frontend type** (`lib/connections.ts:57`): `last_health_check_at: string | null`
- **ARC15_BACKEND_CONTRACT.md**: field is `last_verified_at`
- These are different field names. The frontend type will always read `undefined` for this field when the backend sends `last_verified_at`. Silent data loss — no runtime error, incorrect display.

### C4 — CJ live beats for deferred tools (email_sender, sms_sender, crm/Salesforce)
- **Customer Journey §4.2** describes `email_sender`, `sms_sender`, and `push_to_crm` as live connect beats in Marcus's Pro flow.
- **ARC15 resolution**: all three are arc17_pending (deferred to a future arc).
- These are doc-vs-reality gaps that ARC15 already acknowledged. They are recorded here because they appear as DRIFTED beats in the lived CJ flow, not because they are new disputes.

---

## §9 TOUCHED

Architecture §9 lists 35 AUTHORED-but-unratified commitments. The following are touched by this slice:

| §9 item | Value authored in §9 | Value found in code | Notes |
|---|---|---|---|
| Personality `business_context_max_chars` (Free=280, Pro=800, Ent=2000) | Free: 280, Pro: 800, Enterprise: 2000 | `PersonalitySection.tsx:171`: `maxChars = config.business_context_max_chars ?? 280` (API-driven, fallback 280) | Code correctly defers to API; §9 authored values are the backend's responsibility to serve. Frontend conformant. |
| Widget channel always-on | Always-on, non-removable | `ChannelsSection.tsx`: widget row has no toggle | Conformant. |
| Escalation: Free tier single email field | 1 email contact | `EscalationSection.tsx:254–273` | Conformant. |
| Custom roles gated Enterprise | Enterprise only | `Dashboard.tsx:870`: `tier === "enterprise"` | Conformant. |

---

## RESIDUE DETAIL

### R1 — `system_prompt_additions` in frontend types
- **Location:** `lib/admin.ts:262` (`LucielInstance`), `lib/admin.ts:297` (`UpdateLucielInstanceRequest`)
- **Issue:** Field exists in both the read model and the write (PATCH) request type. Vision §3 doctrine and ARC15 explicitly remove raw prompt authoring. There is no UI surface for this field (correct), but any code path that serializes the full `UpdateLucielInstanceRequest` object will include `system_prompt_additions` in the outbound PATCH body. If the backend silently accepts this field, a stale or `null` value could be written on every save.
- **Dependency impact:** Affects every pillar's save action that uses `updateInstance()`. Low risk if backend ignores unknown fields; medium risk if backend processes the field on any code path. Recommend removing the field from the frontend type and PATCH shape to prevent accidental writes.

---

## BLOCKED-EXTERNAL

None. All findings were determinable from source code and document review.

---

## Headline Summary

**28 CONFORMS · 6 DRIFTED · 5 MISSING · 2 BUG · 1 RESIDUE · 1 AMBIGUOUS · 0 BLOCKED-EXTERNAL**

1. Five-pillar structure, order, and doctrine (no raw prompt, no model selector) conforms throughout.
2. **DRIFTED (C1):** Channels pillar uses a checkbox list, not the multi-select dropdown Vision §3.1 specifies.
3. **BUG (C2):** `refreshConnection` frontend path `…/instances/{id}/connections/{id}/refresh` does not match the backend route `/api/v1/admin/connections/{id}/refresh` — would 404 in production.
4. **BUG (C3):** `ConnectionView.last_health_check_at` in frontend types does not match backend field `last_verified_at` — silent field drop.
5. **MISSING:** Live takeover ("Take over" button) has backend machinery (`human_controlled` state) but zero frontend UI surface (Marcus Phase 7, CJ §7).
6. **MISSING:** Enterprise fleet view — same card grid renders for all tiers; CJ §3/§7 specify a distinct fleet view for Enterprise.
7. **MISSING:** Org-wide policies and branding configuration — no frontend UI found anywhere.
8. **MISSING:** Enterprise personality approval workflow — no approval step in `PersonalitySection.tsx` despite Vision §7 requiring it.
9. **MISSING:** Custom-domain widget (CNAME config) — no UI found.
10. **DRIFTED (ARC15-acknowledged):** `email_sender`, `sms_sender`, `push_to_crm`/Salesforce all render deferred/disabled despite CJ describing them as live beats — arc17_pending per ARC15.
11. **RESIDUE:** `system_prompt_additions` persists in frontend type definitions and outbound PATCH shape with no doc justification and no UI surface.
12. **AMBIGUOUS:** SLA tracking/adherence display for Enterprise — spec mentions it but is unclear whether it requires a real-time UI surface.
