# Arc 4 Deliverable #2 — Tier matrix detail (v2, Free/Pro/Enterprise + Enterprise hybrid billing)

**Status:** DESIGN — not yet implemented in code. Authored 2026-05-22; **rewritten in full 2026-05-22-late** to reflect the Arc 4 tier-shape revision from four tiers (Solo / Team / Company / Enterprise) to three tiers (**Free / Pro / Enterprise**) plus a new Enterprise hybrid-billing axis. Companion to Deliverable #3 (`A-tenancy-collapse-arc-record.md`, execution arc, also revised 2026-05-22-late).

**Audience:** Engineer-facing. The buyer-facing surface is CANONICAL_RECAP §14 "Entitlement matrix v2 (Free / Pro / Enterprise)". When the two disagree, this file documents the *intended* shape and `app/policy/entitlements.py` is the runtime source of truth; any three-way disagreement (CANONICAL §14 / this file / `entitlements.py`) opens a drift in DRIFTS §3.

**Audit-chain note (v1 supersession):** The v1 of this file (4-tier × 22-dim) is preserved in git history; the head of `main` at commit `8c3e0b7` is the canonical v1 source for the four-tier shape. v2 (this file) replaces v1 as the live spec. Both are part of the audit chain.

**Cross-refs:**
- CANONICAL_RECAP.md §11.7 — public-positioning copy (Free / Pro / Enterprise tier framing)
- CANONICAL_RECAP.md §14 — buyer-facing entitlement matrix v2 (six-axis regroup, 3-tier columns) and the v1 annotation block preserving Solo/Team/Company/Enterprise for audit
- ARCHITECTURE.md §3.2 tier-shape note (2026-05-22-late) — system-view commit of the three-tier shape
- ARCHITECTURE.md §3.2.14 — metering hook + `billing_model` enum + `admin_tier_overrides` + `metering_emissions` table
- ARCHITECTURE.md §4.1 — three-axis isolation contract (Admin↔Admin / Instance↔Instance / Lead↔Lead) with 2026-05-22-late annotation
- ARCHITECTURE.md §4.7 — three-layer scope enforcement with 2026-05-22-late annotation (two new gates: Free CAPTCHA + Enterprise metering)
- DRIFTS.md §3 `D-tenancy-collapse-admin-instance-lead-2026-05-22` — umbrella drift (with 2026-05-22-late annotation block extending resolution path)
- DRIFTS.md §3 `D-enterprise-metering-not-implemented-2026-05-22` — runtime metering gap
- DRIFTS.md §3 `D-free-tier-captcha-missing-2026-05-22` — Free-tier abuse-control gap
- `app/policy/entitlements.py` — current 18-dimension × 3-tier module (Step 30a.6 shape under old Individual/Team/Company labels); this file specifies the post-Arc-4 rewrite

---

## §1 — Why this file exists (v2)

CANONICAL §14 commits the three-tier (Free / Pro / Enterprise) capacity-gated doctrine and regroups the entitlement matrix into six axes plus a new Enterprise hybrid-billing axis. What §14 deliberately does *not* carry:

1. **Numeric values for the 18 entitlement dimensions per tier** beyond the per-tier summary cell — no semantics for what "composition depth = 2" actually means at a call-site, no default model-tier label per Instance per tier, no override mechanics on the `admin_tier_overrides` row.
2. **Enterprise-tier numeric values** for the entitlement dimensions — §14 lists them as "Unlimited via overrides" or "Custom"; the negotiation envelope still needs to be bounded somewhere on paper so a sales-call can quote concrete defaults before the override row is written.
3. **Free-tier hard limits and the CAPTCHA + 1-per-email/IP composition** — §14 commits Free as "$0 evaluator tier" but does not specify the 18-dimension numeric floor or the abuse-control gate composition.
4. **The new Enterprise hybrid-billing axis** — `billing_model` enum on `subscriptions`, `included_usage_per_period`, `overage_rate_cents`, `committed_use_discount_bps`, `period_start`, `period_end` columns on `admin_tier_overrides`, and the `metering_emissions` cursor table.
5. **Upgrade / downgrade behavior at the tier boundary** — what happens to an Admin's existing Instances, composition grants, and audit retention when they switch tiers mid-cycle.
6. **The `app/policy/entitlements.py` post-Arc-4 shape** — the dataclass layout, the new `TIER_FREE` / `TIER_PRO` / `TIER_ENTERPRISE` constants, the per-axis override-consultation logic, and the rename of every legacy `TIER_INDIVIDUAL` / `TIER_TEAM` / `TIER_COMPANY` callsite.

This file holds those six detail layers. Each section below maps 1:1 to an axis (or sub-axis) from CANONICAL §14, then enumerates the engineer-facing detail.

---

## §2 — Axis 1: Instance count

### §2.1 — Numeric values

| Tier | `instance_count_cap` | Override surface |
|---|---|---|
| **Free** | 1 | None — gate is hard-coded in `entitlements.py` |
| **Pro** | 3 | None — gate is hard-coded in `entitlements.py` |
| **Enterprise** | `NULL` (= unlimited) | `admin_tier_overrides.instance_cap_override` — Integer, NULL means unlimited; a numeric value tightens to that value (negotiated cap for cost-conscious Enterprise deals) |

### §2.2 — Enforcement surface

- **Where:** `AdminService.create_luciel_instance(admin_id, ...)` (renamed from `create_instance_for_tenant`); also `TierProvisioningService.premint_for_tier(admin_id, tier)` at signup.
- **Failure mode:** 402 with `error.code='instance_cap_exceeded'` and a body shape `{tier, current_count, cap}` so the UI can render an upgrade CTA.
- **Race:** The check is `SELECT count(*) FROM luciel_instances WHERE admin_id=? AND active=true` immediately before the `INSERT`; a `UNIQUE` constraint on `(admin_id, name)` provides the safety net against a concurrent double-insert.

### §2.3 — Upgrade / downgrade behavior

- **Free → Pro:** All existing Free Instances (max 1) carry forward; the cap raises to 3 immediately. No data migration.
- **Pro → Enterprise:** All existing Pro Instances (max 3) carry forward; the cap raises to unlimited unless `admin_tier_overrides.instance_cap_override` is set. No data migration.
- **Enterprise → Pro (downgrade):** If the Admin currently has more than 3 active Instances, the downgrade is **rejected** at the billing-portal layer with `error.code='downgrade_blocked_instance_count'`; the customer is shown the count and asked to deactivate down to 3 first. Soft-deleted Instances above the new cap are unaffected (they stay soft-deleted).
- **Pro → Free (downgrade):** Same shape; reject above 1 active Instance.

---

## §3 — Axis 2: Leads per month

### §3.1 — Numeric values

| Tier | `leads_per_month_cap` | Override surface |
|---|---|---|
| **Free** | 10 | None — hard-coded |
| **Pro** | 2,000 | None — hard-coded |
| **Enterprise** | `NULL` (= unlimited included floor + overage) | `admin_tier_overrides.included_usage_per_period` (Integer, the included floor); overage above the floor is billed via the metering emitter (§17 below) |

### §3.2 — Enforcement surface

- **Where:** `app/services/lead_service.py` `create_lead(...)` — the same call-site that mints a `Lead` row. Counter source: `SELECT count(*) FROM leads WHERE admin_id=? AND created_at >= date_trunc('month', now())`.
- **Failure mode (Free, Pro):** 402 with `error.code='leads_cap_exceeded'`.
- **Failure mode (Enterprise):** Never returns 402 — the lead is always created. The metering emitter (Arc 6) reports the overage to Stripe; the customer-facing visibility is the next invoice, not a 402.
- **Counter reset:** Calendar-month boundary in the Admin's billing timezone (default UTC; future per-Admin override on `admins.billing_timezone`).

### §3.3 — Upgrade / downgrade behavior

- **Mid-month upgrade Free→Pro:** The counter is not reset; the Pro cap (2,000) absorbs whatever count Free already accumulated. Pro customers won't notice; Free customers get effectively-immediate uncap.
- **Mid-month downgrade Pro→Free:** If current month count > 10, the next `create_lead` call returns 402 immediately. The customer is warned at the billing-portal downgrade confirmation step.
- **Enterprise:** Mid-cycle moves trigger a pro-rated `metering_emissions` close-out so the invoice is correct.

---

## §4 — Axis 3: Model tier per Instance

### §4.1 — Numeric values

| Tier | Default model | Burst availability | Custom fine-tunes |
|---|---|---|---|
| **Free** | `base` only (the cheapest model in the foundation-model abstraction layer §4.8) | None | No |
| **Pro** | `mid` (one tier above base) | `top` available for individual turns marked `urgent=true` by the operator; rate-limited to a configurable budget per Admin per month | No |
| **Enterprise** | All available models | Unrestricted | Yes — per-Admin custom fine-tunes via `admin_tier_overrides.fine_tune_model_id` (nullable VARCHAR(64) FK to a future `fine_tunes` table; v1 schema lands at Arc 5 Revision A as a placeholder, runtime wiring is Arc 6+) |

### §4.2 — Enforcement surface

- **Where:** `app/runtime/context_assembler.py` model-selection logic, called from `chat_service.py` per turn.
- **Failure mode:** A Pro customer requesting `urgent=true` past their monthly burst budget gets the `mid` model with a `model_downgrade_reason='burst_budget_exhausted'` flag on the response envelope. No 402; the turn completes.

### §4.3 — Override surface

`admin_tier_overrides.model_tier_default` (nullable VARCHAR(32)) lets an Enterprise contract specify a non-default model as the floor; e.g. an Enterprise customer who negotiated `top` as their default rather than `mid`.

---

## §5 — Axis 4: Composition (Instance-to-Instance grants within an Admin)

### §5.1 — Numeric values

| Tier | `composition_enabled` | `max_composition_depth` | `knowledge_share_grants_enabled` |
|---|---|---|---|
| **Free** | False | 0 | False |
| **Pro** | True | 2 | False (composition only; knowledge-share is Enterprise-only) |
| **Enterprise** | True | `NULL` (= unlimited within reason) | True |

### §5.2 — Definitions

- **Composition depth** = the number of hops in an inter-Instance call chain. Depth=2 means Instance A can call Instance B which can call Instance C, and the audit row carries the full chain. Depth>2 calls are rejected at `app/services/composition_service.py`.
- **Knowledge-share grant** = an explicit `knowledge_share_grants(grantor_instance_id, grantee_instance_id, knowledge_namespace, granted_at, granted_by_user_id, revoked_at)` row that permits the grantee Instance to read from the grantor's knowledge namespace. Without this row, every Instance is knowledge-isolated even within an Admin scope.

### §5.3 — Enforcement surface

- **Where:** `app/services/composition_service.py` `resolve_composition_chain(...)`; called from the foundation-model tool-call dispatch layer in `chat_service.py`.
- **Failure modes:** `composition_disabled_for_tier` (Free), `max_depth_exceeded` (Pro at >2 hops), `composition_grant_missing` (any tier without an explicit `instance_composition_grants` row).
- **Override surface:** `admin_tier_overrides.composition_depth_override` (Integer, NULL means unlimited; a numeric value tightens) for Enterprise.

---

## §6 — Axis 5: API access (programmatic + widget)

### §6.1 — Numeric values

| Tier | `api_enabled` | Rate limit | Embed-key minting |
|---|---|---|---|
| **Free** | False | N/A | False (no widget embed, no public API access; widget chat for Free Instances only works via the marketing-site preview surface) |
| **Pro** | True | 60 requests/minute per Admin (configurable per-key inside the budget) | True (up to 3 embed keys per Admin, one per Instance) |
| **Enterprise** | True | Custom per `admin_tier_overrides.api_rate_limit_rpm` (Integer, default 1000) | True (unlimited embed keys; key minting still requires `admin_owner` role) |

### §6.2 — Enforcement surface

- **Where:** `app/middleware/rate_limit.py` (existing module from Step 30a); reads `subscriptions.tier` (now `admins.tier` post-Arc-5) to select the limit.
- **Failure mode:** 429 with `Retry-After` header. Free requests are 403 with `error.code='api_disabled_for_tier'`.

---

## §7 — Axis 6: Roles and seats

### §7.1 — Numeric values

| Tier | Available roles | Seat cap | `delegated_admin_enabled` |
|---|---|---|---|
| **Free** | `admin_owner` only (1 user = the signup email) | 1 | False |
| **Pro** | `admin_owner`, `instance_lead`, `member` | 5 (the Admin and up to 4 invited teammates) | False |
| **Enterprise** | `admin_owner`, `instance_lead`, `member`, `delegated_admin` | `NULL` (= unlimited, but bounded by `admin_tier_overrides.seat_cap_override` if set) | True (gated on `admin_tier_overrides.delegated_admin_enabled`) |

### §7.2 — Role definitions

- **`admin_owner`** — full read/write/admin on the Admin scope; the signup user; one per Admin.
- **`instance_lead`** — read/write on the Instances they're assigned to via `scope_assignments`; read-only on Admin-rollup dashboard panes.
- **`member`** — read-only on the Instances they're assigned to.
- **`delegated_admin`** (Enterprise-only) — full admin on a subset of Instances delegated by `admin_owner`; supports an SSO-mapped sub-admin pattern for large Enterprise customers.

### §7.3 — Enforcement surface

- **Where:** `app/middleware/auth.py` resolves the user's role on every request; `app/services/admin_service.py` calls the role-check helper on every mutating call.
- **Override surface:** `admin_tier_overrides.seat_cap_override` (Integer, NULL means unlimited) and `admin_tier_overrides.delegated_admin_enabled` (Boolean).

---

## §8 — Axis 7: Dashboard views

### §8.1 — Numeric values

| Tier | Single-instance view | Instance-group view | Admin rollup | Metering / overage surface |
|---|---|---|---|---|
| **Free** | ✅ | ❌ | ❌ | ❌ |
| **Pro** | ✅ | ✅ | ✅ | ❌ |
| **Enterprise** | ✅ | ✅ | ✅ | ✅ |

### §8.2 — Enforcement surface

The `/app` shell in the marketing site reads `admins.tier` and selects one of three layouts (`FreeShell`, `ProShell`, `EnterpriseShell`). Backend dashboard endpoints (`/api/v1/dashboard/{admin,instance-group,instance}`) are unchanged in shape but return 403 with `error.code='dashboard_disabled_for_tier'` when the caller's tier does not include that view.

---

## §9 — Axis 8: Audit retention

### §9.1 — Numeric values

| Tier | `audit_retention_days` | Override surface |
|---|---|---|
| **Free** | 30 | None |
| **Pro** | 365 | None |
| **Enterprise** | `NULL` (= unlimited) | `admin_tier_overrides.audit_retention_days_override` (Integer, NULL means unlimited; a numeric value caps; most Enterprise contracts negotiate 7-year retention for FINTRAC compliance, so the override is rarely NULL in practice) |

### §9.2 — Enforcement surface

The retention purge worker (`app/workers/retention_worker.py`, shipped at Step 30a.2) reads each Admin's tier + override and purges `admin_audit_log` rows older than the resolved retention.

---

## §10 — Axis 9: SSO

### §10.1 — Numeric values

| Tier | SSO | Provider support |
|---|---|---|
| **Free** | ❌ | N/A |
| **Pro** | ❌ | N/A |
| **Enterprise** | ✅ | SAML 2.0 v1; OIDC v2 (Arc 7+) |

### §10.2 — Enforcement surface

`app/services/sso_service.py` (lands at Arc 7+); Enterprise contracts include the SP metadata exchange. Pro customers asking for SSO are sales-funnel-routed to Enterprise.

---

## §11 — Axis 10: Custom widget branding

### §11.1 — Numeric values

| Tier | `widget_branding_custom` | Co-brand "Powered by Luciel" |
|---|---|---|
| **Free** | False | Always shown |
| **Pro** | False (color + logo customization only) | Always shown |
| **Enterprise** | True | Removable via `admin_tier_overrides.cobrand_hidden=true` |

---

## §12 — Axis 11: Webhook outbound

### §12.1 — Numeric values

| Tier | Webhook outbound | Concurrent deliveries |
|---|---|---|
| **Free** | False | N/A |
| **Pro** | True (1 endpoint per Admin) | 5 concurrent |
| **Enterprise** | True (unlimited endpoints) | `admin_tier_overrides.webhook_concurrency_override` (default 50) |

---

## §13 — Axis 12: Cross-Instance memory federation

### §13.1 — Numeric values

| Tier | Federation enabled | Notes |
|---|---|---|
| **Free** | False | One Instance = no federation surface |
| **Pro** | False (composition is the surface for cross-Instance work; memory stays per-Instance) | A Pro customer who needs federated memory across Instances is an Enterprise customer |
| **Enterprise** | True | Requires `knowledge_share_grants` row per (grantor, grantee, namespace) |

---

## §14 — Axis 13: SLA

### §14.1 — Numeric values

| Tier | Uptime SLA | Response SLA |
|---|---|---|
| **Free** | None (best-effort) | None |
| **Pro** | 99.5% (no credit, just published target) | 24h email response |
| **Enterprise** | 99.9% with service credits per `admin_tier_overrides.sla_credit_schedule` | Custom per contract; default 4h business-hours |

---

## §15 — Axis 14: Data residency

### §15.1 — Numeric values

| Tier | Region |
|---|---|
| **Free** | `ca-central-1` only |
| **Pro** | `ca-central-1` only |
| **Enterprise** | `ca-central-1` default; alternate region via `admin_tier_overrides.data_residency_region` (lands at Arc 9+ when a second region is provisioned) |

---

## §16 — Axis 15: Export

### §16.1 — Numeric values

| Tier | CSV export | Full audit-chain export |
|---|---|---|
| **Free** | False | False |
| **Pro** | True (CSV of own data) | False (the audit chain is a regulator-facing surface; Pro customers see the dashboard view but not the raw chain) |
| **Enterprise** | True | True |

---

## §17 — NEW Axis: Enterprise hybrid billing model

This axis is net-new in v2 of this matrix. It applies to Enterprise tier only; Free and Pro have a single implied `billing_model` (Free=`free` implicit, Pro=`flat`).

### §17.1 — `billing_model` enum on `subscriptions`

| Value | Meaning | Applies to |
|---|---|---|
| `flat` | Recurring Price only; no metered emission | Pro (default); Enterprise contracts that negotiated a flat rate |
| `hybrid` | Recurring platform-fee Price + metered usage Price on the same subscription | Enterprise (default) |
| `consumption` | Metered usage Price only (no platform fee) | Reserved for v2 future; no SKU ships with `consumption` at Arc 6 |

### §17.2 — `admin_tier_overrides` hybrid-billing columns

| Column | Type | Meaning | Default for Enterprise hybrid |
|---|---|---|---|
| `billing_model` | VARCHAR(16) | Mirror of the subscription-row value; lives here so a regulator reading the override table alone can see the negotiated shape | `'hybrid'` |
| `included_usage_per_period` | Integer | The included floor of metered units (leads or tokens, per the configured unit) | Negotiated; typical floors: 10,000 leads/month or 10M tokens/month |
| `overage_rate_cents` | Integer | Per-unit overage rate in cents (Stripe's wire format) | Negotiated; typical rates: 5¢/lead or $0.05/1K tokens |
| `committed_use_discount_bps` | Integer | Basis points off the floor for committed-use Admins (e.g. 1500 = 15% off the platform fee in exchange for a 12-month commit) | 0 (no discount) for month-to-month; 1500 typical for annual commits |
| `period_start` | Date | Contract window start | Signing date |
| `period_end` | Date | Contract window end; renewal triggers re-negotiation, not silent rollover | Signing date + 12 months |
| `metered_unit` | VARCHAR(16) | Either `'leads'` or `'tokens'` | `'leads'` (real-estate vertical default) |

### §17.3 — Metering emitter (Celery beat)

Per ARCHITECTURE §3.2.14, an hourly Celery-beat worker reads the running total of the metered unit and emits the delta to Stripe with an idempotency key. Failure modes are documented at DRIFTS §3 `D-enterprise-metering-not-implemented-2026-05-22`.

### §17.4 — Pricing-page positioning

CANONICAL_RECAP §11.7 commits the "starting at `$[ENTERPRISE_FLOOR]/year`" framing with a `[Talk to sales](mailto:enterprise@vantagemind.ai)` CTA. The `$[ENTERPRISE_FLOOR]` placeholder is filled at Arc 6 when the marketing-side copy is wired (placeholder is intentional in this design doc so the value is not committed in a code review separate from the marketing-copy review).

### §17.5 — Self-serve carve-out

Enterprise is **deliberately not self-serve at v1**. There is no Checkout flow that mints an Enterprise subscription; every Enterprise row is operator-minted via a `scripts/mint-enterprise-admin.ps1` runbook that takes the signed contract terms and writes the `admin_tier_overrides` row + the Stripe subscription. Self-serve Enterprise is a future shape if a market signal demands it.

---

## §18 — `app/policy/entitlements.py` post-Arc-4 shape

### §18.1 — Constants

```python
TIER_FREE = "free"
TIER_PRO = "pro"
TIER_ENTERPRISE = "enterprise"
ALL_TIERS = (TIER_FREE, TIER_PRO, TIER_ENTERPRISE)

BILLING_MODEL_FLAT = "flat"
BILLING_MODEL_HYBRID = "hybrid"
BILLING_MODEL_CONSUMPTION = "consumption"
ALL_BILLING_MODELS = (BILLING_MODEL_FLAT, BILLING_MODEL_HYBRID, BILLING_MODEL_CONSUMPTION)
```

### §18.2 — Per-tier static map (load-bearing for Free + Pro; Enterprise consults overrides)

```python
TIER_ENTITLEMENTS = {
    TIER_FREE: TierEntitlement(
        instance_count_cap=1,
        leads_per_month_cap=10,
        model_tier_default="base",
        composition_enabled=False,
        max_composition_depth=0,
        knowledge_share_grants_enabled=False,
        api_enabled=False,
        api_rate_limit_rpm=0,
        embed_key_count_cap=0,
        seat_cap=1,
        delegated_admin_enabled=False,
        dashboard_views=frozenset({"single_instance"}),
        audit_retention_days=30,
        sso_enabled=False,
        widget_branding_custom=False,
        webhook_outbound_enabled=False,
        cross_instance_memory_federation=False,
        uptime_sla_pct=None,
        data_residency_region="ca-central-1",
        export_csv_enabled=False,
        export_audit_chain_enabled=False,
    ),
    TIER_PRO: TierEntitlement(
        instance_count_cap=3,
        leads_per_month_cap=2000,
        model_tier_default="mid",
        composition_enabled=True,
        max_composition_depth=2,
        knowledge_share_grants_enabled=False,
        api_enabled=True,
        api_rate_limit_rpm=60,
        embed_key_count_cap=3,
        seat_cap=5,
        delegated_admin_enabled=False,
        dashboard_views=frozenset({"single_instance", "instance_group", "admin_rollup"}),
        audit_retention_days=365,
        sso_enabled=False,
        widget_branding_custom=False,
        webhook_outbound_enabled=True,
        cross_instance_memory_federation=False,
        uptime_sla_pct=99.5,
        data_residency_region="ca-central-1",
        export_csv_enabled=True,
        export_audit_chain_enabled=False,
    ),
    TIER_ENTERPRISE: TierEntitlement(
        # Defaults; every field is overrideable via admin_tier_overrides
        instance_count_cap=None,  # unlimited
        leads_per_month_cap=None,  # unlimited included floor; overage metered
        model_tier_default="top",
        composition_enabled=True,
        max_composition_depth=None,  # unlimited
        knowledge_share_grants_enabled=True,
        api_enabled=True,
        api_rate_limit_rpm=1000,
        embed_key_count_cap=None,  # unlimited
        seat_cap=None,  # unlimited
        delegated_admin_enabled=True,
        dashboard_views=frozenset({"single_instance", "instance_group", "admin_rollup", "metering_overage"}),
        audit_retention_days=None,  # unlimited (typically negotiated to 7y)
        sso_enabled=True,
        widget_branding_custom=True,
        webhook_outbound_enabled=True,
        cross_instance_memory_federation=True,
        uptime_sla_pct=99.9,
        data_residency_region="ca-central-1",
        export_csv_enabled=True,
        export_audit_chain_enabled=True,
    ),
}
```

### §18.3 — `resolve_entitlement(admin, axis)` algorithm

1. Read `admin.tier`.
2. Look up the static map: `value = TIER_ENTITLEMENTS[admin.tier].<axis>`.
3. If `admin.tier == TIER_ENTERPRISE` and an `admin_tier_overrides` row exists for this Admin and the override column for this axis is not NULL, return the override value.
4. Otherwise return the static value.

**Fail-closed posture:** A missing entitlement entry inherits the most-restrictive value (no access). A missing override row for an Enterprise Admin reads the static map (which is the unlimited shape for Enterprise — so an Enterprise Admin with no override row gets the full feature set, which is the intended posture for a freshly-signed-and-not-yet-customized Enterprise customer).

---

## §19 — Migration & rename scope

The legacy `entitlements.py` (Step 30a.6 shape under `TIER_INDIVIDUAL` / `TIER_TEAM` / `TIER_COMPANY` labels) is **fully replaced** at Arc 5 Revision B (cutover). The rename map:

| Legacy | v2 |
|---|---|
| `TIER_INDIVIDUAL` | `TIER_PRO` (Solo customers backfill to Pro at the in-migration `UPDATE`; their cap raises from 3 to … 3 — no semantic change for them at the Instance axis; their `leads_per_month_cap` raises from the implicit unlimited-with-rate-limit shape to the explicit 2000 cap, which is a **tightening** that affects no current customer because no current Solo customer exceeds 2000 leads/month per the existing usage data) |
| `TIER_TEAM` | Eliminated. Existing Team customers (none on the live wire at the time of Arc 4) would backfill to `TIER_PRO` if any existed. |
| `TIER_COMPANY` | `TIER_ENTERPRISE` (Company customers backfill to Enterprise with a default `admin_tier_overrides` row mirroring their Company tier limits as override values, so their effective limits do not change) |
| `DOMAIN_COUNT_CAP_BY_TIER` | Eliminated entirely (Domain layer removed at Arc 4) |
| `INSTANCE_COUNT_CAP_BY_TIER[TIER_INDIVIDUAL]=3` | `TIER_ENTITLEMENTS[TIER_PRO].instance_count_cap=3` (same value, new key) |
| `INSTANCE_COUNT_CAP_BY_TIER[TIER_TEAM]=10` | Eliminated |
| `INSTANCE_COUNT_CAP_BY_TIER[TIER_COMPANY]=50` | `TIER_ENTITLEMENTS[TIER_ENTERPRISE].instance_count_cap=None` (unlimited) with `admin_tier_overrides.instance_cap_override=50` for the backfilled Company customers |

The rename is purely a label move for the live customers (Solo→Pro, Company→Enterprise with override-preserved-limits); the only **net-new** tier is Free, and the only **net-removed** tier is Team (which had no live customers).

---

## §20 — Audit chain integrity

The Arc 4 tier rename emits the following audit rows:

- One `TIER_RENAME_APPLIED` row per existing customer at the cutover transaction (Arc 5 Revision B), with field set `{old_tier, new_tier, admin_tier_overrides_minted: bool}`.
- One `BILLING_MODEL_ENUM_ADDED` row at Arc 5 Revision A close, tied to the schema change itself.
- Two `METERING_INFRASTRUCTURE_ADDED` rows (one for the `admin_tier_overrides` extended columns, one for the `metering_emissions` table) at Arc 5 Revision A close.

All four action types are added to `app/models/admin_audit_log.py` in Arc 5 Revision A. The hash chain advances normally; the integrity check (`scripts/verify-audit-chain.ps1`) confirms continuity across the rename boundary.
