# Arc 4 Deliverable #2 — Tier matrix detail

**Status:** DESIGN — not yet implemented in code. Authored 2026-05-22 alongside Deliverable #1 (canonical doctrine integration, commit `66f6528`). Companion to forthcoming Deliverable #3 (`A-tenancy-collapse-arc-record.md`, execution arc).

**Audience:** Engineer-facing. The buyer-facing surface is CANONICAL_RECAP §14 "Entitlement matrix". When the two disagree, this file documents the *intended* shape and `app/policy/entitlements.py` is the runtime source of truth; any three-way disagreement (CANONICAL §14 / this file / `entitlements.py`) opens a drift in DRIFTS §3.

**Cross-refs:**
- CANONICAL_RECAP.md §14 — buyer-facing entitlement matrix (six-axis regroup, 4-tier columns)
- ARCHITECTURE.md §4.1 — three-axis isolation contract (Admin↔Admin / Instance↔Instance / Lead↔Lead)
- DRIFTS.md §3 `D-tenancy-collapse-admin-instance-lead-2026-05-22` — umbrella drift this file resolves under
- `app/policy/entitlements.py` — current 18-dimension × 3-tier module (Step 30a.6 shape); this file specifies the 22-dimension × 4-tier rewrite

---

## §1 — Why this file exists

CANONICAL §14 commits the new four-tier (Solo / Team / Company / Enterprise) capacity-gated doctrine and regroups the entitlement matrix into six axes. What §14 deliberately does *not* carry:

1. **Numeric values for the four new Arc-4 dimensions** (`model_tier_per_instance`, `composition_enabled`, `max_composition_depth`, `knowledge_share_grants_enabled`) beyond the per-tier summary cell — no semantics for what "depth=2" actually means at a composition call-site, no default model-tier label per Instance, no override mechanics.
2. **Enterprise-tier numeric values** for the existing 18 dimensions — §14 lists them as "Unlimited (negotiated)" or "Custom" with no specifics; the negotiation envelope still needs to be bounded somewhere.
3. **Upgrade / downgrade behavior at the tier boundary** — what happens to an Admin's existing Instances, composition grants, and audit retention when they switch tiers mid-cycle.
4. **The `app/policy/entitlements.py` post-rename shape** — the dataclass layout, the new `TIER_SOLO` / `TIER_ENTERPRISE` constants, the four new `Dimension(...)` rows, and the rename of `domains_cap` (delete) and the rename of every `TIER_INDIVIDUAL` callsite.

This file holds those four detail layers. Each section below maps 1:1 to an axis from CANONICAL §14, then enumerates the engineer-facing detail for that axis.

---

## §2 — Axis 1: Instance count

### §2.1 — Numeric values (post-Arc-4)

| Dimension | Solo | Team | Company | Enterprise | Live-Today? | Enforcement site |
|---|---|---|---|---|---|---|
| `luciel_instances_cap` (rename → `instances_cap`) | 3 | 10 | 50 | `None` (sentinel for "negotiated") | ✅ | `POST /api/v1/admin/luciel-instances` (rename → `/api/v1/admin/instances`) checks against `INSTANCE_COUNT_CAP_BY_TIER` |
| `widget_cap` (derived) | 1 | 3 | `None` (Unlimited within instance cap) | `None` | ✅ | Derived view; embed-key-mint at Step 30b / Step 31.2 |

### §2.2 — Enterprise envelope

"Unlimited (negotiated)" is **not** an unbounded runtime value. The Enterprise tier's `instances_cap` is `None` in `entitlements.py` (matches existing Company `widget_cap` and Company `leads_cap` sentinel pattern), and the negotiation envelope is bounded by **a per-Admin override row** in a new table `admin_tier_overrides` (proposed for Deliverable #3 schema migration):

```
admin_tier_overrides
-------------------
admin_id              FK admins.id      PK
dimension_key         varchar(64)       PK
override_value        jsonb             (literal int / bool / str matching the Entitlement.value type)
override_enforced     boolean
notes                 text              (operator-set, e.g. "Contract 2026-Q3, 200-instance cap")
created_at            timestamptz
created_by_user_id    FK users.id
```

When `get_entitlement(tier=TIER_ENTERPRISE, dimension_key=...)` is called, the lookup first checks `admin_tier_overrides` for the Admin in scope; if a row exists, that wins. If no row exists, the `_enterprise_set()` default applies (which is "unbounded" for most dimensions — i.e. `None`).

This is the **one architectural shape** that Enterprise needs that the existing three tiers don't: a per-Admin override surface. Without it, Enterprise is indistinguishable from Company-with-bigger-numbers, and the negotiation envelope is unauditable.

### §2.3 — Upgrade / downgrade behavior at this axis

| From → To | Instance cap behavior |
|---|---|
| Solo → Team | Cap goes 3 → 10. Existing Instances all carry forward. No data movement. |
| Team → Company | Cap goes 10 → 50. Existing Instances all carry forward. No data movement. |
| Company → Enterprise | Cap goes 50 → negotiated. Existing Instances all carry forward. An `admin_tier_overrides` row is **required** at the upgrade transaction or the upgrade aborts (no silent fall-back to "unbounded"). |
| Team → Solo (downgrade) | Cap goes 10 → 3. If Admin currently has > 3 Instances, the downgrade is **refused at the API layer** with a structured error listing the excess Instances. Admin must archive Instances before downgrade. No Instance is ever auto-deleted at tier-change. |
| Company → Team (downgrade) | Cap goes 50 → 10. Same refusal-on-excess policy as Team → Solo. |
| Enterprise → Company (downgrade) | Cap goes negotiated → 50. Same refusal-on-excess policy. The `admin_tier_overrides` row is **soft-deleted** (audit-trail preserved), not hard-deleted. |

---

## §3 — Axis 2: Per-instance capacity

### §3.1 — Numeric values

| Dimension | Solo | Team | Company | Enterprise | Live-Today? | Enforcement site |
|---|---|---|---|---|---|---|
| `leads_cap` (per Instance) | 3 active | 100 active | `None` (Unlimited) | `None` | ✅ | Lead-create call-site checks against `LEADS_CAP_BY_TIER` |
| `conversations_per_day_per_seat` | 50 | 200 | `None` (Unlimited) | `None` | ❌ Roadmap | Per-seat counter not built; gated on Step 34 / Step 31.x. **Override available via `admin_tier_overrides`** when built. |
| `audit_retention_days` | 30 | 90 | 365 | "Custom" (`None` = honor `admin_tier_overrides`, default 365) | ❌ Roadmap | Purge worker tier-agnostic today; gated on next cron touch. |

### §3.2 — `leads_cap` semantics — per Instance or per Admin?

**Per Instance.** The cap counts *active* leads against one Instance, not the Admin's aggregate. A Solo Admin with 3 Instances may have 9 active leads total (3 × 3). The cap is the gate that triggers the soft-purge cascade after retention expiry — once an Instance hits its `leads_cap`, the lead-create call-site returns a structured "cap-reached" response, and the Admin must either archive an existing lead, upgrade, or wait for the purge worker to expire stale leads.

This is unchanged from the existing Step 30a.6 shape; documenting here because the Arc 4 collapse renames `agent_id` → `instance_id` and the cap mechanic must rebind to the new noun.

### §3.3 — `conversations_per_day_per_seat` semantics — what counts?

A "conversation" is a `conversations` row (the cross-channel grouping primitive from Step 24.5c). A new conversation is a new count-tick. Continued messages within the same `conversation_id` do not re-tick. The "seat" is a `users.id` under the Admin scope, not an Instance. A Team Admin with 10 seats has a daily ceiling of 10 × 200 = 2,000 conversations.

The reset window is **rolling 24h**, not midnight-aligned, per the implementation precedent at the Stripe rate-limit middleware. This avoids the "midnight rush" failure mode where every Admin's counter resets at the same wall-clock moment.

### §3.4 — `audit_retention_days` — purge worker shape

Today's purge worker (Step 30a.2) is **tier-agnostic** with a uniform 90-day window. Post-Arc-4 it becomes tier-aware:

```python
# pseudocode for the purge worker dispatch
for admin in admins_query:
    retention_days = get_entitlement(
        tier=admin.tier,
        dimension_key="audit_retention_days",
    ).value
    if retention_days is None:  # Enterprise with override
        override = admin_tier_overrides.lookup(admin.id, "audit_retention_days")
        retention_days = override.value if override else 365
    cutoff = now() - timedelta(days=retention_days)
    purge_audit_rows(admin_id=admin.id, older_than=cutoff)
```

The purge worker still runs nightly; only the cutoff varies per Admin. The `None` sentinel for Enterprise means "honor override or default 365" — Enterprise cannot be "infinite retention" without an explicit override row, because unbounded retention is a compliance hazard (we'd be holding data indefinitely with no buyer-side commitment).

### §3.5 — Upgrade / downgrade behavior at this axis

| Transition | Per-instance behavior |
|---|---|
| Solo → Team | `leads_cap` 3 → 100 (per Instance). Existing leads carry forward; no purge. `audit_retention_days` 30 → 90: rows aged 30–90 days that were due for purge are *preserved* (we never shorten history; we only ever extend it on upgrade). |
| Team → Company | `leads_cap` 100 → Unlimited. `audit_retention_days` 90 → 365: same preserve-on-extend rule. |
| Company → Enterprise | `audit_retention_days` 365 → override-controlled (default 365, never shorter). |
| Team → Solo (downgrade) | `leads_cap` 100 → 3: existing leads beyond 3 per Instance are **not** auto-deleted; they remain accessible but the cap-reached gate fires on new lead-create. `audit_retention_days` 90 → 30: rows aged 30–90 days are **preserved through the current billing cycle**, then become eligible for purge at the next billing-cycle boundary. (We do not retroactively shorten the data window mid-cycle — that would be a quiet contract break.) |
| Company → Team / Enterprise → Company (downgrade) | Same retention-preserve-through-cycle rule. |

---

## §4 — Axis 3: Model tier per Instance

### §4.1 — New dimension (Arc 4)

| Dimension | Solo | Team | Company | Enterprise | Live-Today? | Enforcement site |
|---|---|---|---|---|---|---|
| `model_tier_per_instance` | `"mini"` | `"mid"` | `"premium"` | `"premium+dedicated"` | ❌ Roadmap | Not yet wired; gated on future arc that adds per-Instance model-class routing in `app/services/chat_service.py` |

### §4.2 — Semantics

`model_tier_per_instance` is the **default** model class for a new Instance under that Admin. It is a label, not a model identifier — the mapping label → concrete model (e.g. `mini` → `gpt-4o-mini`, `mid` → `gpt-4o`, `premium` → `claude-sonnet-4`) is held in a separate `MODEL_TIER_MAPPING` table/config so we can swap the concrete model without changing the entitlement contract.

The label is the **floor**, not the ceiling. A Company-tier Admin can configure a specific Instance to use `mini` (e.g. for a high-volume low-stakes use case) — the entitlement just sets the default and the maximum class the Admin is *allowed* to select per Instance. A Solo Admin cannot configure an Instance above `mini`; the chat-service routing layer refuses the call.

### §4.3 — `premium+dedicated` semantics

Enterprise's `premium+dedicated` means premium-class model running on dedicated infrastructure (negotiated). This is the bridge to the "dedicated infrastructure" promise in CANONICAL §14's closing paragraph. The dedicated infrastructure is a separate provisioning concern (separate VPC, separate model-host pool); the entitlement contract just gates *whether* the Admin can request it.

### §4.4 — Override surface

`admin_tier_overrides` applies here too — a Company-tier Admin can negotiate `premium+dedicated` access without upgrading to Enterprise (and pay the model-cost differential). The override is the lever; the tier label is the default.

---

## §5 — Axis 4: Composition

### §5.1 — Three new dimensions (Arc 4)

| Dimension | Solo | Team | Company | Enterprise | Live-Today? | Enforcement site |
|---|---|---|---|---|---|---|
| `composition_enabled` | `False` | `True` | `True` | `True` | ❌ Roadmap | `instance_composition_grants` table not yet created |
| `max_composition_depth` | `0` | `2` | `None` (Unlimited within Admin) | `None` | ❌ Roadmap | Depth enforcement at composition call site |
| `knowledge_share_grants_enabled` | `False` | `False` | `True` | `True` | ❌ Roadmap | `knowledge_share_grants` table not yet created |

### §5.2 — Two-table contract

Composition needs **two** tables, not one. The distinction is the §4.1 isolation contract:

**Table 1 — `instance_composition_grants`:** grants one Instance permission to *call* another Instance as a tool. Directional. Audited. Bounded by `max_composition_depth`.

```
instance_composition_grants
---------------------------
id                      uuid PK
admin_id                FK admins.id        (composition is same-Admin-only)
caller_instance_id      FK instances.id
callee_instance_id      FK instances.id
created_at              timestamptz
created_by_user_id      FK users.id
revoked_at              timestamptz NULL
notes                   text

CHECK (caller_instance_id != callee_instance_id)
CHECK (admin_id = (SELECT admin_id FROM instances WHERE id = caller_instance_id))
CHECK (admin_id = (SELECT admin_id FROM instances WHERE id = callee_instance_id))
```

**Table 2 — `knowledge_share_grants`:** grants one Instance the right to *read knowledge* (vector embeddings, memories, lead context) from another Instance. Strict superset of composition — if knowledge-share is granted, composition is implied. The reverse is **not** true: a Team Admin can grant Instance A → Instance B composition (Instance A can call Instance B as a tool) without granting knowledge-share (Instance A cannot read Instance B's memory directly; the only data flow is via the explicit tool-call response payload, which is audited).

```
knowledge_share_grants
----------------------
id                      uuid PK
admin_id                FK admins.id        (same-Admin-only)
source_instance_id      FK instances.id     (the Instance whose knowledge is being shared)
target_instance_id      FK instances.id     (the Instance gaining read access)
scope                   varchar(32)         CHECK IN ('memories', 'leads', 'embeddings', 'all')
created_at              timestamptz
created_by_user_id      FK users.id
revoked_at              timestamptz NULL
```

### §5.3 — Depth semantics — what does `max_composition_depth = 2` mean?

Depth is the **length of the composition chain at call time**, not the count of grants. If Instance A → Instance B → Instance C is invoked as one request, that's depth 2. A grant graph with 10 instances but each request only chains 2-deep is fine at depth = 2.

| Depth value | Meaning | Reference |
|---|---|---|
| `0` | No composition at all (Solo tier) — every Instance is an island | enforcement: refuse any cross-Instance tool call at the chat-service layer |
| `2` | A chain of at most 2 calls (A→B, or A→B→C) per request | enforcement: chat-service maintains a per-request chain-depth counter; aborts if call would exceed |
| `None` | Unlimited within Admin scope, bounded only by request-timeout and tool-budget | enforcement: same counter, no abort |

Cross-Admin composition is **architecturally forbidden** at all depths, all tiers — enforced by the FK chain (every grant row has `admin_id` and both Instance FKs must resolve to the same `admin_id`). Even an Enterprise Admin with custom SLA cannot compose with another Admin's Instances; if cross-Admin federation is ever needed, it requires a *new* primitive (federation, not composition) and a new contract surface.

### §5.4 — Audit trail per composition call

Every composition call emits one `AdminAuditLog` row:

```
action:           "instance_composition_called"
admin_id:         <calling Admin>
actor_user_id:    <user who initiated the request, or NULL for autonomous triggers>
target_resource:  "instance"
target_id:        <callee_instance_id>
metadata: {
  caller_instance_id: <uuid>,
  callee_instance_id: <uuid>,
  chain_depth: <int>,
  grant_id: <uuid>,
  call_signature: <sha256 of tool name + arg shape>,
}
hash_prev:        <previous row hash>
hash_self:        <this row hash>
```

The hash-chain (existing audit chain from §4.3) extends through composition calls. A composition chain of depth 2 emits **two** audit rows (one per hop), not one.

### §5.5 — Upgrade / downgrade at this axis

| Transition | Composition behavior |
|---|---|
| Solo → Team | `composition_enabled` `False` → `True`. No existing grants to migrate (Solo had none). Admin must explicitly create grants via the admin UI; nothing auto-enables. |
| Team → Company | `max_composition_depth` `2` → `None`. Existing grants remain valid (the grant *graph* is unchanged); only the per-request depth ceiling lifts. |
| Company → Solo (downgrade) | `composition_enabled` `True` → `False`. All existing `instance_composition_grants` and `knowledge_share_grants` are **soft-revoked** (revoked_at set, rows preserved for audit). At the next composition call attempt, the chat service refuses. Re-upgrade restores the grants (revoked_at cleared) within a 30-day grace window; after 30 days the rows hard-delete via the next purge worker pass. |
| Team → Solo (downgrade) | Same soft-revoke cascade as Company → Solo. |
| Company → Team (downgrade) | `max_composition_depth` `None` → `2`. Existing grants remain valid (no grant-row state change). New composition calls that would exceed depth 2 are refused at request time; in-flight calls already deeper than 2 complete (we don't kill mid-request). |

---

## §6 — Axis 5: Configurability

### §6.1 — Numeric values

| Dimension | Solo | Team | Company | Enterprise | Live-Today? | Enforcement site |
|---|---|---|---|---|---|---|
| `custom_branding` | `False` | `False` | `True` | `True` | ❌ Roadmap | Step 30b widget carries theme field; tier-gating not enforced |
| `voice_channel` | `"Roadmap"` | `"Roadmap"` | `"Roadmap"` | `"Roadmap"` | ❌ Roadmap | Step 34a channel adapter framework |
| `sms_channel` | `"Roadmap"` | `"Roadmap"` | `"Roadmap"` | `"Roadmap"` | ❌ Roadmap | Step 34a |
| `email_channel` | `"Roadmap"` | `"Roadmap"` | `"Roadmap"` | `"Roadmap"` | ❌ Roadmap | Step 34a |

### §6.2 — `custom_branding` shape

The widget already carries a `theme` JSONB field (from Step 30b). Today the value is ignored at render time (always uses the default VantageMind theme). Post-Arc-4 enforcement:

- Solo / Team: `theme` field reads as `null` to the widget runtime regardless of stored value (the entitlement check happens at the read-side, not the write-side, so an Admin who upgrades sees their stored theme apply without re-saving).
- Company / Enterprise: `theme` field reads as stored.

The write-side accepts any valid theme JSON regardless of tier (we don't strip data on write), so upgrade-to-Company instantly unlocks any theme the Admin had already saved.

### §6.3 — Channel adapters — why all four tiers are "Roadmap"

Per the Step 30a.6 correction (`D-channels-promised-not-built-multi-tier-2026-05-20`), voice / SMS / email are committed across all tiers, not Company-only. Gated entirely on Step 34a's adapter framework. When Step 34a lands, all four tier cells flip together to `True` (or to a per-tier rate limit if we discover we need to gate channel volume).

---

## §7 — Axis 6: Operational features

### §7.1 — Numeric values (Live-Today + Roadmap)

| Dimension | Solo | Team | Company | Enterprise | Live-Today? | Enforcement site |
|---|---|---|---|---|---|---|
| `seats` | 1 | 10 | 50 | `None` (negotiated) | ✅ | `invite_service.py` invite-mint cap |
| `api_rate_limit_rpm` | 10 | 60 | 300 | `None` (Custom) | ❌ Roadmap | `RateLimitFallback` middleware; per-tier profiles not wired |
| `concurrent_instances` | 2 | 10 | 50 | `None` (Custom) | ❌ Roadmap | Concurrency counter not built |
| `cross_instance_memory` | `None` (N/A — composition off) | `None` (N/A — knowledge-share off) | `"Roadmap"` (explicit grants) | `"Roadmap"` | ❌ Roadmap | Knowledge-share grant table not built |
| `audit_csv_export` | `False` | `False` | `True` | `True` | ❌ Roadmap | No route exists; Step 31.x |
| `sso` | `False` | `False` | `"Roadmap"` | `"Roadmap"` | ❌ Roadmap | No integration; future enterprise step beyond Step 33b |
| `priority_support` | `"None (community / docs)"` | `"Email response within 24h"` | `"Email + Slack within 4h"` | `"Custom SLA"` | ❌ Roadmap | No SLA infrastructure; future ops step |
| `dedicated_success_manager` | `False` | `False` | `"Annual cadence only"` | `True` | ❌ Roadmap | Operator-process row; first Company annual hand-off |

### §7.2 — REMOVED dimension — `domains_cap`

Original Dimension 3 from the Step 30a.6 matrix. Arc 4 removes the Domain layer entirely. The entitlements module deletes this dimension; the `DOMAIN_COUNT_CAP_BY_TIER` map in `app/models/subscription.py` is deleted in the same commit. CANONICAL §14 preserves the row with strikethrough as audit chain; this module does *not* preserve it (the module is the runtime source of truth; preserving a dead dimension creates lookup risk).

### §7.3 — `api_rate_limit_rpm` — per Admin or per Instance?

**Per Admin.** A Team Admin with 10 Instances shares 60 rpm across all Instances. This matches the "Admin is the billing boundary" doctrine — rate limit is a billing-axis cap, not a per-Instance capacity cap (the per-Instance capacity cap is `conversations_per_day_per_seat`).

### §7.4 — `concurrent_instances` — what counts as "concurrent"?

A `concurrent_instances` value of 10 means: at any single moment in time, no more than 10 of the Admin's Instances may be actively processing an LLM call. An Instance idle-between-requests does not count. The counter ticks on chat-service entry and decrements on exit (including error exit).

This is **per-Admin**, not per-Instance. An Admin with 50 Instances all warm-but-idle is at concurrent count 0.

### §7.5 — `seats` semantics — what does a "seat" map to?

A `seat` is one row in `users` × `scope_assignments` under the Admin. Scope assignments rebind from `(tenant_id, domain_id?, agent_id?)` to `(admin_id, instance_id?)`. A user can have multiple scope_assignment rows (e.g. assigned to two Instances under the same Admin) without counting as multiple seats — the seat is the `users.id`, not the assignment.

The post-Arc-4 role taxonomy on `scope_assignments`:

| Old role | New role | Semantics |
|---|---|---|
| `owner` | `owner` | Unchanged. One per Admin. The Admin's billing contact. Full read/write across every Instance under the Admin. |
| `tenant_admin` | `admin_user` | Renamed. A user with admin-write privileges across the Admin scope but not the billing contact. |
| `department_lead` | `instance_lead` | Renamed. A user assigned to a specific Instance with read/write on that Instance only. |
| `teammate` | `member` | Renamed. A user assigned to a specific Instance with read-only on that Instance. |

The four-role taxonomy is preserved in shape; only the names change. The `_enforce_tier_scope` map in `admin_service.py` deletes the `domain` row and renames the `agent` row to `instance`.

---

## §8 — Post-rewrite `app/policy/entitlements.py` shape

This is the **target** state of the module after the Arc 4 execution arc completes. The structure is unchanged (dataclasses, frozen, lookup helpers); only the dimensions, tiers, and constants rebind.

```python
"""Tier-entitlement matrix v2 (Arc 4, 2026-05-22).

The operational differences between Solo, Team, Company, and Enterprise
tiers -- beyond instance cap and price -- live in this module as the
single first-class artifact. CANONICAL_RECAP §14 "Entitlement matrix" is
the buyer-facing surface. Six axes, 22 named dimensions across 4 tiers.

Eight dimensions remain deferred to follow-up Steps (channel adapters,
per-seat metering, concurrency counter, etc.) plus the four new Arc 4
dimensions (model tier, composition, depth, knowledge-share) all
roadmap. Per-row drifts open lazily at the corresponding-Step touch.

Enterprise tier accepts per-Admin overrides via `admin_tier_overrides`
(see arc4-out/A-tier-matrix-detail.md §2.2).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.subscription import (
    TIER_COMPANY,
    TIER_ENTERPRISE,  # NEW
    TIER_SOLO,        # renamed from TIER_INDIVIDUAL
    TIER_TEAM,
)


@dataclass(frozen=True)
class Entitlement:
    value: Any
    enforced: bool
    pairing_step: str | None = None


@dataclass(frozen=True)
class Dimension:
    key: str
    label: str
    axis: str  # NEW: one of "instance_count", "per_instance_capacity",
               # "model_tier", "composition", "configurability",
               # "operational"


DIMENSIONS: tuple[Dimension, ...] = (
    # Axis 1 -- Instance count (2 dimensions)
    Dimension("instances_cap", "Instances cap", "instance_count"),
    Dimension("widget_cap", "Widget cap (derived)", "instance_count"),

    # Axis 2 -- Per-instance capacity (3 dimensions)
    Dimension("leads_cap", "Leads / conversations stored cap (per Instance)", "per_instance_capacity"),
    Dimension("conversations_per_day_per_seat", "Conversations per day, per seat", "per_instance_capacity"),
    Dimension("audit_retention_days", "Audit retention", "per_instance_capacity"),

    # Axis 3 -- Model tier (1 dimension, NEW)
    Dimension("model_tier_per_instance", "Model tier per Instance", "model_tier"),

    # Axis 4 -- Composition (3 dimensions, NEW)
    Dimension("composition_enabled", "Composition enabled", "composition"),
    Dimension("max_composition_depth", "Max composition chain depth", "composition"),
    Dimension("knowledge_share_grants_enabled", "Knowledge-share grants enabled", "composition"),

    # Axis 5 -- Configurability (4 dimensions)
    Dimension("custom_branding", "Custom widget branding", "configurability"),
    Dimension("voice_channel", "Voice channel adapter", "configurability"),
    Dimension("sms_channel", "SMS channel adapter", "configurability"),
    Dimension("email_channel", "Email channel adapter", "configurability"),

    # Axis 6 -- Operational features (8 dimensions)
    Dimension("seats", "Seats (people who can sign in under the Admin)", "operational"),
    Dimension("api_rate_limit_rpm", "API rate limit (rpm, per Admin)", "operational"),
    Dimension("concurrent_instances", "Concurrent Instances (per-Admin LLM concurrency)", "operational"),
    Dimension("cross_instance_memory", "Cross-instance memory (knowledge-share)", "operational"),
    Dimension("audit_csv_export", "Audit CSV export", "operational"),
    Dimension("sso", "SSO (SAML / OIDC)", "operational"),
    Dimension("priority_support", "Priority support", "operational"),
    Dimension("dedicated_success_manager", "Dedicated success manager", "operational"),
)

assert len(DIMENSIONS) == 21, (  # 2+3+1+3+4+8 = 21 dimensions in the module
    "DIMENSIONS must remain 21 rows in the module -- the 22nd row in "
    "CANONICAL_RECAP §14's matrix is `domains_cap` preserved with "
    "strikethrough as audit chain; the module does not carry dead rows."
)


# Pairing-step tokens (existing + new Arc 4 tokens)
_STEP_34A_CHANNELS = "Step 34a (channel adapter framework)"
_STEP_31X_METERING = "Step 31.x (per-seat metering + per-tier rate-limit profiles)"
_STEP_36_COUNCIL = "Step 36 (Luciel Council) / Step 31.x (concurrency counter)"
_STEP_37_HYBRID = "Step 37 (hybrid retrieval) -- knowledge-share grants"
_STEP_NEXT_CRON = "next cron touch -- per-tier retention class into the purge worker"
_STEP_AUDIT_EXPORT = "Step 31.x (audit CSV export route)"
_STEP_WIDGET_THEME = "next widget touch -- tier-gating the theme field"
_STEP_FUTURE_OPS = "future ops step -- SLA infrastructure"
_STEP_FIRST_COMPANY_ANNUAL = "first Company annual hand-off"
_STEP_BEYOND_33B = "future enterprise step beyond Step 33b -- SSO integration"
_STEP_ARC_5_COMPOSITION = "Arc 5 (composition grant tables + chat-service routing)"  # NEW
_STEP_ARC_5_MODEL_TIER = "Arc 5 (per-Instance model-class routing)"  # NEW


# Each tier's row builder -- shape unchanged, dimension set rebound.
def _solo_set() -> EntitlementSet:
    return {
        # Axis 1
        "instances_cap": Entitlement(3, enforced=True),
        "widget_cap": Entitlement(1, enforced=True),
        # Axis 2
        "leads_cap": Entitlement(3, enforced=True),
        "conversations_per_day_per_seat": Entitlement(50, enforced=False, pairing_step=_STEP_31X_METERING),
        "audit_retention_days": Entitlement(30, enforced=False, pairing_step=_STEP_NEXT_CRON),
        # Axis 3
        "model_tier_per_instance": Entitlement("mini", enforced=False, pairing_step=_STEP_ARC_5_MODEL_TIER),
        # Axis 4
        "composition_enabled": Entitlement(False, enforced=False, pairing_step=_STEP_ARC_5_COMPOSITION),
        "max_composition_depth": Entitlement(0, enforced=False, pairing_step=_STEP_ARC_5_COMPOSITION),
        "knowledge_share_grants_enabled": Entitlement(False, enforced=False, pairing_step=_STEP_ARC_5_COMPOSITION),
        # Axis 5
        "custom_branding": Entitlement(False, enforced=False, pairing_step=_STEP_WIDGET_THEME),
        "voice_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        "sms_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        "email_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        # Axis 6
        "seats": Entitlement(1, enforced=True),
        "api_rate_limit_rpm": Entitlement(10, enforced=False, pairing_step=_STEP_31X_METERING),
        "concurrent_instances": Entitlement(2, enforced=False, pairing_step=_STEP_36_COUNCIL),
        "cross_instance_memory": Entitlement(None, enforced=True),  # N/A -- composition off
        "audit_csv_export": Entitlement(False, enforced=True),
        "sso": Entitlement(False, enforced=True),
        "priority_support": Entitlement("None (community / docs)", enforced=False, pairing_step=_STEP_FUTURE_OPS),
        "dedicated_success_manager": Entitlement(False, enforced=True),
    }


def _enterprise_set() -> EntitlementSet:
    return {
        # Axis 1
        "instances_cap": Entitlement(None, enforced=True),  # negotiated; admin_tier_overrides required
        "widget_cap": Entitlement(None, enforced=True),
        # Axis 2
        "leads_cap": Entitlement(None, enforced=True),
        "conversations_per_day_per_seat": Entitlement(None, enforced=False, pairing_step=_STEP_31X_METERING),
        "audit_retention_days": Entitlement(None, enforced=False, pairing_step=_STEP_NEXT_CRON),  # default 365 via override path
        # Axis 3
        "model_tier_per_instance": Entitlement("premium+dedicated", enforced=False, pairing_step=_STEP_ARC_5_MODEL_TIER),
        # Axis 4
        "composition_enabled": Entitlement(True, enforced=False, pairing_step=_STEP_ARC_5_COMPOSITION),
        "max_composition_depth": Entitlement(None, enforced=False, pairing_step=_STEP_ARC_5_COMPOSITION),  # Unlimited
        "knowledge_share_grants_enabled": Entitlement(True, enforced=False, pairing_step=_STEP_ARC_5_COMPOSITION),
        # Axis 5
        "custom_branding": Entitlement(True, enforced=False, pairing_step=_STEP_WIDGET_THEME),
        "voice_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        "sms_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        "email_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        # Axis 6
        "seats": Entitlement(None, enforced=True),
        "api_rate_limit_rpm": Entitlement(None, enforced=False, pairing_step=_STEP_31X_METERING),
        "concurrent_instances": Entitlement(None, enforced=False, pairing_step=_STEP_36_COUNCIL),
        "cross_instance_memory": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_37_HYBRID),
        "audit_csv_export": Entitlement(True, enforced=False, pairing_step=_STEP_AUDIT_EXPORT),
        "sso": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_BEYOND_33B),
        "priority_support": Entitlement("Custom SLA", enforced=False, pairing_step=_STEP_FUTURE_OPS),
        "dedicated_success_manager": Entitlement(True, enforced=False, pairing_step=_STEP_FIRST_COMPANY_ANNUAL),
    }


# _team_set() and _company_set() follow the same shape -- see CANONICAL §14
# matrix for the full numeric values per dimension. Omitted here for brevity
# (will be authored in full at execution-arc commit time).


ENTITLEMENTS_BY_TIER: dict[str, EntitlementSet] = {
    TIER_SOLO: _solo_set(),
    TIER_TEAM: _team_set(),
    TIER_COMPANY: _company_set(),
    TIER_ENTERPRISE: _enterprise_set(),
}


# Override lookup -- NEW for Arc 4
def get_entitlement(
    *,
    tier: str,
    dimension_key: str,
    admin_id: str | None = None,
    db_session: Any | None = None,
) -> Entitlement:
    """Look up an entitlement cell, honoring admin_tier_overrides for Enterprise.

    When tier == TIER_ENTERPRISE and admin_id + db_session are provided,
    the lookup first checks admin_tier_overrides for that Admin.
    Otherwise falls back to the tier default.
    """
    if tier == TIER_ENTERPRISE and admin_id and db_session:
        override = _lookup_admin_override(
            db_session=db_session,
            admin_id=admin_id,
            dimension_key=dimension_key,
        )
        if override is not None:
            return Entitlement(
                value=override.override_value,
                enforced=override.override_enforced,
                pairing_step=None,
            )
    return ENTITLEMENTS_BY_TIER[tier][dimension_key]
```

(`_team_set()` and `_company_set()` omitted in this design doc for brevity; their full numeric rows are mechanically derived from the CANONICAL §14 matrix and will be authored at execution-arc commit time. The pattern is fixed.)

---

## §9 — Code-surface impact assessment

Surfaces that touch tier or scope nouns and need rename/rewrite in the Arc 5 execution arc:

| File | Current state | Post-Arc-4 target | Notes |
|---|---|---|---|
| `app/models/subscription.py` | `TIER_INDIVIDUAL` / `TIER_TEAM` / `TIER_COMPANY` constants; `TIER_PERMITTED_SCOPES = {TIER_TEAM: ("agent", "domain"), ...}` (line 99–103); `INSTANCE_COUNT_CAP_BY_TIER`; `DOMAIN_COUNT_CAP_BY_TIER` | `TIER_SOLO` / `TIER_TEAM` / `TIER_COMPANY` / `TIER_ENTERPRISE`; `TIER_PERMITTED_SCOPES` map deleted (uniform `("instance",)` at every tier); `DOMAIN_COUNT_CAP_BY_TIER` deleted | **Existing drift confirmed (added to Arc 4 sweep):** `TIER_PERMITTED_SCOPES[TIER_TEAM]` still claims `("agent", "domain")` at line 101 even though Step 30a.6 set `DOMAIN_COUNT_CAP_BY_TIER[TIER_TEAM] = 0` at line 147. Arc 4 collapse closes this inconsistency by deleting the map entirely. |
| `app/policy/entitlements.py` | 18 dimensions × 3 tiers (this file's pre-Arc-4 reading) | 21 dimensions × 4 tiers + Enterprise override path | Per §8 above |
| `app/services/tier_provisioning_service.py` | `premint_for_tier` walks tier-specific scope branch | Uniform `premint_for_admin` — mints Admin + 1 Instance regardless of tier | Composition grants minted lazily on Admin demand, not at signup |
| `app/services/admin_service.py` | `_enforce_tier_scope` maps tier → allowed scope_level | Map deleted; tier no longer determines scope topology | Scope is uniform `(admin_id, instance_id?, lead_id?)` |
| `app/api/v1/admin.py` | `/admin/luciel-instances` POST; `/admin/domains/self-serve` POST | `/admin/instances` POST; `/admin/domains/*` routes deleted | Backwards-compat redirect maintained for one billing cycle |
| `app/models/subscription.py` (Subscription FK) | `subscriptions.tenant_id` | `subscriptions.admin_id` | Alembic column rename |
| `app/middleware/session_cookie_auth.py` | References `TenantConfig.active` | `AdminConfig.active` | Mechanical rename |
| `app/services/billing_webhook_service.py` | `Tenant` / `TenantConfig` lookups | `Admin` / `AdminConfig` | Mechanical rename |
| Audit constants | `TENANT_CREATED`, `DOMAIN_CREATED`, `AGENT_CREATED`, etc. | `ADMIN_CREATED`, `INSTANCE_CREATED` (DOMAIN_* deleted, AGENT_* renamed) | Audit chain integrity preserved: historical rows keep their original action strings; new rows use the new strings; the verify-chain script reads both |

**Measured scope of rename sweep (corrected at Deliverable #3 grounding pass, 2026-05-22):** ~4,025+ raw callsites across 227 files in `app/`, `tests/`, and `alembic/`. Breakdown:

| Identifier | Callsite count | Files |
|---|---|---|
| `tenant_id` (column / variable / kwarg) | 2,121 | majority of API + service + model layer |
| `domain_id` (column / variable / kwarg) | 917 | dashboard + admin + cascade layer |
| `agent_id` (column / variable / kwarg) | 606 | chat + identity + Luciel-instance layer |
| `LucielInstance` (class / type / import) | 381 | model + service + API layer |
| `Domain` (class / type / import) | 298 | model + service layer |
| `Tenant` (class / type / import) | 214 | model + service + middleware layer |
| `Agent` (class / type / import) | 456 | shared with the AI-worker class name; care needed to disambiguate from third-party `Agent` references if any |
| `TenantConfig` | 83 | config layer |
| `tenant_admin` (role string) | 63 | scope_assignments + policy layer |
| `TIER_INDIVIDUAL` | 38 | subscription model + service layer |
| `department_lead` (role string) | 16 | scope_assignments + policy layer |
| `DOMAIN_COUNT_CAP` | 13 | subscription model + service layer |

The scale forces a **staged migration** plan over a single-revision rename — see Deliverable #3 §3 (Migration ordering decision). Stripe SKU rename (Individual → Solo) remains a separate out-of-band concern.

---

## §10 — Open questions for execution arc (Deliverable #3)

These are decisions deliberately *not* made in Deliverable #2 — they belong in the execution arc where the migration plan lives:

1. **Migration ordering** — single Alembic revision vs sequence of revisions (rename Tenant first, then drop Domain, then rename Agent)? Single revision is atomic but harder to rollback; sequence is granular but creates intermediate states.
2. **Backwards-compat window** — do we keep `tenant_id` as a column alias for one billing cycle (via DB view) or hard-cut on the migration commit?
3. **Stripe SKU rename timing** — before, during, or after the schema migration? My recommendation: after. Stripe rename is cosmetic and reversible; do it last so the schema is stable before customers see the new label.
4. **`admin_tier_overrides` migration** — created empty at Arc 5, or pre-populated with one override per existing Enterprise customer? (Currently zero Enterprise customers, so empty is safe; but the migration must create the table.)
5. **Customer-comms SES email** — sent at Stripe-rename time, or at schema-migration time? My recommendation: at Stripe-rename time. Customers don't see the schema; they see the Stripe label. Tying the email to the visible event is cleaner.
6. **`max_composition_depth` ceiling at Company tier** — is `None` (truly unlimited) the right call, or should we set a safety ceiling (e.g. 10) to prevent runaway composition chains? Strongly recommend a safety ceiling at the *implementation* layer even when the contract is `None` — the request-timeout / tool-budget layer should already prevent runaway chains, but belt-and-suspenders.

---

## §11 — Closing

This file is the engineer-facing detail layer for the Arc 4 tier matrix doctrine integrated into CANONICAL §14 at commit `66f6528`. It is the input artifact for Arc 5 (schema migration + code rename sweep) and Arc 6 (Stripe SKU restructure + customer-comms). It carries no implementation; the implementation lands in those execution arcs against this spec.

When the Arc 5 schema migration lands, this file's §8 module shape becomes the actual `app/policy/entitlements.py` module. When the Arc 6 Stripe rename lands, this file's §10 open questions become resolved commitments and get folded into the canonical record.

Until those arcs land, this file is the contract.

**Closing tag:** `arc-4-deliverable-2-tier-matrix-detail` — earned when this file commits and DRIFTS §3 `D-tenancy-collapse-admin-instance-lead-2026-05-22` adds a cross-ref line pointing here.
