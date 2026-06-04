# Manifest Section 04 — Tiers, Billing, Audit & Analytics

**Scope:** Tier entitlement matrix, billing/metering/overage, escalation DELIVERY, analytics, audit log
immutability, retention policy, data export/portability.

**Source-of-truth reads:**
- `docs_text/VISION.txt` (Vision §7 full tier table, §3.4, §3.5, §3.9, §5.10)
- `docs_text/ARCHITECTURE.txt` (Arc §3.4.1b, §3.5, §5.2, §5.3, §3.4.10, §3.9, §5.10, §8)
- `docs_text/CUSTOMER_JOURNEY.txt`

**Prior ARC reports read:** `ARC15_BACKEND_REPORT.md`, `ARC17_LOOKUP_RECORD_AMENDMENT.md`
(neither touched the items in this slice).

---

## 1. Cluster 1 — Tier Capability Matrix: Vision §7 vs `entitlements.py`

**Primary file:** `app/policy/entitlements.py` — v2 post-Arc-5 surface, 3-tier (Free/Pro/Enterprise).

### 1.1 Axis-by-Axis Comparison Table

| Axis | Vision §7 value | `entitlements.py` value | Status | Notes |
|------|----------------|------------------------|--------|-------|
| **Instance count** | Free=1 / Pro=10 / Ent=unlimited | Free=1 / Pro=10 / Ent=None (unlimited) | CONFORMS | `entitlements.py:226,261,312` |
| **Conversation budget (monthly)** | Free=200 / ProMonthly=2000 / ProAnnual=2500 / Ent=10000 | `_CONVERSATION_BUDGET` dict: same values exactly | CONFORMS | `entitlements.py:715-722`; cadence-aware derivation |
| **Overage rate** | Pro monthly=$15/100 / Pro annual=$10/100 / Ent=contractual / Free=N/A | `_OVERAGE_RATE_PER_100_CENTS`: 1500/1000/None/None | CONFORMS | `entitlements.py:731-734`; cents to avoid float drift |
| **Budget reset** | Monthly on billing cycle boundary | Stripe `invoice.paid` webhook advances period_start; Free uses signup-anchored monthly fallback | CONFORMS | `billing_webhook_service.py:1330+`, `billing_period.py` |
| **Channels per instance** | Free=widget / Pro=widget+email+SMS / Ent=all (incl. voice, WhatsApp) | Free={widget} / Pro={widget,email,sms} / Ent={widget,email,sms} | **DRIFTED** | Vision §7 says Enterprise gets "all channels (incl. voice, WhatsApp)"; `entitlements.py:518-522` only maps `{widget,email,sms}` for Enterprise — no voice or WhatsApp channel ids. `channels_available()` derivation at line 525. |
| **Admin-notification channels** | Free=email / Pro=email+SMS / Ent=email+SMS+Slack+custom | `_ESCALATION_NOTIFY_CHANNELS_BY_TIER`: Free={email} / Pro={email,sms} / Ent={email,sms,slack,custom} | CONFORMS | `entitlements.py:638-648` |
| **Tool access** | Free=cognition-only / Pro=full catalog+sibling+BYO webhook / Ent=Pro+custom authoring | `composition_enabled`: Free=False / Pro=True / Ent=True; `custom_role_authoring_enabled`: Ent only; no `tool_authoring` axis | **AMBIGUOUS** | Vision §7 mentions "custom tool authoring" for Enterprise; `TierEntitlement` has no `tool_authoring_enabled` axis. Composition (sibling) is correctly gated. Custom tool authoring not enforced via entitlements. |
| **Knowledge file per-file cap** | Free=10MB / Pro=50MB / Ent=500MB | Free=10MB / Pro=50MB / Ent=500MB | CONFORMS | `entitlements.py:230,265,297` |
| **Knowledge total quota** | Free=100MB / Pro=5GB / Ent=unlimited | Free=100×1024² / Pro=5×1024³ / Ent=None | CONFORMS | `entitlements.py:228,263,295` |
| **Website crawl** | Free=no / Pro=yes / Ent=yes | `knowledge_website_crawl_enabled`: False/True/True | CONFORMS | `entitlements.py:231,266,298` |
| **Graph store** | Free=no / Pro=? / Ent=yes | **NOT present** in `TierEntitlement` | **MISSING** | Vision §7 lists "graph store" row (Free=N/A, Pro=Yes, Ent=Yes). No `graph_store_enabled` axis in `TierEntitlement` dataclass or derivation functions. Architecture §8 names `app/runtime/knowledge_retrieval.py` as doctrine-anchored; check if graph retrieval is toggled there instead. |
| **Sibling delegation** | Free=none / Pro=directional per-pair grants / Ent=grants+optional approval workflow | `composition_enabled` Free=False, Pro=True, Ent=True; Enterprise `sibling_grant_authored` → `approval_state='pending_approval'` (Arc 12 WU4); `knowledge_share_grants_enabled` Free=False, Pro=False, Ent=True | CONFORMS | `entitlements.py:119-120,234,269,315`; approval workflow in `admin_audit_log.py:427` |
| **Agentic loop iterations** | 5 (same all tiers) | Enforced in `orchestrator.py` (not an entitlements axis — correct) | CONFORMS | Vision §7: "5 (same as all tiers)" |
| **Escalation contact** | Free=1 email / Pro=primary+secondary+routing rules / Ent=chains+SLA tracking | `escalation_secondary_contact_enabled`: Pro+Ent=True; `escalation_chains_enabled`: Ent only | CONFORMS | `entitlements.py:667-678`, `escalation_config.py:297-333` |
| **Personality — presets** | All tiers: 4 named presets | Not an entitlements axis; all-tier access assumed | CONFORMS (implicit) | |
| **Personality — custom preset** | Pro+Ent only | `custom_personality_enabled()`: Pro+Ent=True | CONFORMS | `entitlements.py:612-619` |
| **Personality — business context char cap** | Free+Pro=280 / Ent=2000+approval | `_BUSINESS_CONTEXT_CAP_BY_TIER`: 280/280/2000 | CONFORMS | `entitlements.py:594-598`. Approval workflow: Enterprise approval on business_context mentioned in Vision §7 but not surfaced as a `TierEntitlement` axis — **AMBIGUOUS** |
| **Model selection** | Free=base / Pro=mid / Ent=top (tier-locked) | `model_tier_default`: "base"/"mid"/"top" | CONFORMS | `entitlements.py:232,267,313` |
| **Uptime SLA** | Vision §7: Free=best-effort / Pro=99.9% / Ent=99.95% | `uptime_sla_pct`: None / **99.5** / **99.9** | **DRIFTED** | Vision §7 line 509: Pro=99.9%, Ent=99.95%. `entitlements.py:284` sets Pro=99.5%; `entitlements.py:334` sets Ent=99.9%. Both are one SLA tier lower than Vision §7. |
| **Support SLA** | Free=community / Pro=48h email / Ent=24h email+CSM | `support_sla`: SUPPORT_SLA_COMMUNITY / EMAIL_48H / EMAIL_24H_PLUS_CSM | CONFORMS | `entitlements.py:248,285,335` |
| **Branding** | Free=powered-by / Pro=powered-by+custom domain / Ent=white-label | `widget_branding_custom`: False/False/True; `widget_custom_domain_cname_cap`: 0/1/None | CONFORMS | `entitlements.py:243-244,280-281,330-331`. Vision says Pro has "powered-by + custom domain" (custom domain yes, branded widget no); code gives Pro `widget_branding_custom=False` and CNAME cap=1 — matches. |
| **Dashboard analytics** | Free=basic counters / Pro=full per-instance+per-team-member / Ent=fleet+SLA+CSV export | `dashboard_views` frozenset: Free={single_instance} / Pro={single_instance,instance_group,admin_rollup} / Ent=same as Pro | **DRIFTED** | Vision §7 tier analytics differ (Pro should have per-team-member, Ent fleet+SLA+CSV). The `dashboard_views` axis captures VIEW types only, not analytics depth. See Cluster 6 for detail. |
| **Cross-team roles** | Free=n/a / Pro=4 locked roles / Ent=4+custom roles | `delegated_admin_enabled`: Free=False, Pro=False, Ent=True; `custom_role_authoring_enabled`: Ent only; `seat_cap`: 1/25/None | CONFORMS | `entitlements.py:131,206,239,254,273,291,319-320,341` |
| **Audit retention** | Free=30d / Pro=1y / Ent=7y | `audit_retention_days`: 30/365/None(typically 7y per contract) | CONFORMS | `entitlements.py:241,278,328`. Enterprise None = unlimited/contractual — matches spec. |
| **Self-serve data export** | Free=closure-only / Pro=anytime / Ent=anytime+CSM | `export_csv_enabled`: False/True/True; `export_audit_chain_enabled`: False/False/True | **DRIFTED** | `export_csv_enabled` approximates Pro/Ent self-serve. However, the Free "closure-only" gating is NOT enforced in `admin.py` `request_data_export` endpoint (line 2362+). A Free admin can currently call `POST /account/export` at any time — no tier check blocks them. |
| **SSO** | Vision §7 table: Enterprise only | `sso_enabled`: False/False/True | CONFORMS | `entitlements.py:242,279,329` |
| **Webhook outbound** | Pro+Ent | `webhook_outbound_enabled`: False/True/True | CONFORMS | `entitlements.py:245,282,332` |

### 1.2 Summary of Cluster 1 Findings

| Status | Count | Items |
|--------|-------|-------|
| CONFORMS | 18 | Instance count, conversation budget, overage rate, budget reset, admin-notify channels, file caps, knowledge quota, crawl, sibling, loop iters, escalation contact, custom personality, business_context cap, model, support SLA, branding, cross-team roles, audit retention, SSO, webhook |
| DRIFTED | 4 | Channels (Ent missing voice/WhatsApp), uptime SLA (Pro 99.5 vs 99.9; Ent 99.9 vs 99.95), dashboard analytics (views-only not depth), self-serve export Free gate missing |
| MISSING | 1 | Graph store entitlement axis |
| AMBIGUOUS | 2 | Custom tool authoring for Ent; Enterprise business_context approval workflow |

---

## 2. Cluster 2 — Billing & Metering

| # | Requirement | Doc cite | Implementing artifact(s) | Status | Notes / evidence |
|---|------------|----------|--------------------------|--------|-----------------|
| 2.1 | `conversation_overage_ledger` model: `(admin_id, instance_id, billing_period_start)` unique, durable billing audit trail | Architecture §3.4.1b, Arc 18 | `app/models/conversation_overage_ledger.py:46-102` | CONFORMS | Unique constraint `uq_overage_ledger_period` at line 91; RLS ENABLED+FORCED per docstring line 24; all required columns present (`conversations_used`, `budget_cap`, `overage_count`, `overage_units_reported`, `tier_at_close`, `cadence_at_close`, `stripe_usage_record_id`) |
| 2.2 | `BudgetMeter`: Redis per-session idempotency marker; per-instance counter; 70-day TTL | Architecture §3.4.1b, Arc 18 spec §23 | `app/runtime/budget_meter.py:1-252` | CONFORMS | `count_session_once` uses SETNX idempotency (line 207); `_COUNTER_TTL_SECONDS=70*24*3600` (line 38); `mark_alert_fired_once` for threshold dedup (line 235) |
| 2.3 | Stripe metered overage usage records reported at cycle close (`invoice.paid`); period reset | Architecture §3.4.1b | `app/services/billing_webhook_service.py:1330-1516` | CONFORMS | `_on_invoice_paid` handler at line 1330; calls `overage_billing.report_overage_usage` (line 1455); resets counter after reporting; idempotency via `last_event_id` dedup on Subscription row |
| 2.4 | 80% nudge email; 100% email+SMS; budget_csm_alert_at_80 for Enterprise | Architecture §3.4.1b, Vision §7 | `app/services/budget_alert_service.py:1-380`; `app/policy/entitlements.py:803-832` | CONFORMS | `_BUDGET_ALERT_CHANNELS` at `entitlements.py:806-817`; CSM copy at `budget_alert_service.py:159-165`; SMS intent logged even when transport not wired (line 316) |
| 2.5 | `app/billing/metering.py` — Architecture §8 canonical path | Architecture §8 | **Path drift**: actual code at `app/runtime/budget_meter.py` + `app/services/overage_billing.py` | DRIFTED | §8 doctrine-anchored path `app/billing/metering.py` does not exist. Functionality split across `app/runtime/budget_meter.py` (counter) and `app/services/overage_billing.py` (Stripe reporting). Capability present; path drifted from §8 canonical. |
| 2.6 | Free at-cap: graceful handoff, no LLM call, budget_exhausted escalation | Vision §3.4.1b, Architecture §3.4.1b | `app/models/escalation_event.py:69` `SIGNAL_BUDGET_EXHAUSTED`; `app/runtime/budget_meter.py` | CONFORMS | `SIGNAL_BUDGET_EXHAUSTED` signal wired in escalation model CHECK constraint; `ACTION_BUDGET_EXHAUSTED` in audit constants |
| 2.7 | `billing_period.py` Free fallback (signup-anchored monthly) | Architecture §3.4.1b | `app/runtime/billing_period.py` (exists) | CONFORMS | Referenced in `budget_meter.py:12-14` docstring |

---

## 3. Cluster 3 — Escalation DELIVERY

| # | Requirement | Doc cite | Implementing artifact(s) | Status | Notes / evidence |
|---|------------|----------|--------------------------|--------|-----------------|
| 3.1 | `NotificationAdapter` protocol: email (SES all tiers), SMS (Twilio Pro+Ent), Slack Incoming Webhook (Ent) | Architecture §3.5.1 | `app/notifications/` | **MISSING** | Architecture §8 doctrine-anchored path `app/notifications/` does **not exist**. Notification delivery is a best-effort log-only stub in `app/policy/escalation.py:_maybe_notify` (line 245): "a later unit binds concrete SES/SMS/Slack senders." No `NotificationAdapter` protocol implemented. |
| 3.2 | `escalation_event` row written BEFORE any delivery attempt | Architecture §3.5.2 | `app/policy/escalation.py:_write_event` (line ~200) | CONFORMS | `_write_event` called then `db.commit()` before `_maybe_notify` at line ~180+ of `record_escalation` |
| 3.3 | Idempotency key `(session_id, signal_type, gate)` | Architecture §3.5.5 | `app/models/escalation_event.py` (no unique constraint on these three) | **MISSING** | Architecture §3.5.5 specifies dedup via idempotency key `(session_id, signal_type, gate)`. The `escalation_events` table has no unique constraint on `(session_id, signal, gate)`. The "check status='delivered' or 'acked'" dedup logic (Arc §3.5.5) is also absent — there is no `status` column on `escalation_events`. |
| 3.4 | Pro: single-email routing; Fan-out | Architecture §3.5.3 | `app/policy/escalation_routing.py:resolve_contact` | **DRIFTED** | `resolve_contact` (line 95) resolves tier-shaped channel set correctly. However, per its docstring (line 26): "v2 has NO escalation-contact surface on the Instance model … address fields are left unresolved." Email/SMS/Slack concrete addresses are never populated. Routing decision is computed but delivery cannot execute. |
| 3.5 | Enterprise chain walker: 5-min first-step SLA, ack via dashboard open, advance on timeout, owner fallback | Architecture §3.5.4 | Searched all services + policy files | **MISSING** | No chain walker implementation found. `escalation_config.py` validates chain shape (SLA minutes accepted) but the runtime that advances steps, tracks timeouts, and fires fallback does not exist. |
| 3.6 | Retry 3× exponential backoff (Architecture §3.5.5; Architecture line 1599) | Architecture §3.5.5, line 1599 | Not found | **MISSING** | "3 attempts, exponential backoff" specified. No retry logic found in escalation path. |
| 3.7 | Audit events: `escalation_notification_sent`, `escalation_delivery_failed`, `escalation_chain_step`, `escalation_acked`, `escalation_chain_end_fallback` | Architecture §3.5.6 | `app/models/admin_audit_log.py` | **MISSING** | `ACTION_ESCALATION_FIRED` is present (line 595) and `ALLOWED_ACTIONS` includes it (line 837). None of the five §3.5.6 delivery-phase audit events (`escalation_notification_sent`, `escalation_delivery_failed`, `escalation_chain_step`, `escalation_acked`, `escalation_chain_end_fallback`) are defined as constants or appear anywhere in the codebase. |
| 3.8 | `escalation_acked` on dashboard open / explicit "I'm on it" | Architecture §3.5.4 | Not found | **MISSING** | Ack mechanism not implemented. |

---

## 4. Cluster 4 — Audit Log Immutability

| # | Requirement | Doc cite | Implementing artifact(s) | Status | Notes / evidence |
|---|------------|----------|--------------------------|--------|-----------------|
| 4.1 | Schema: `event_id`(PK), `admin_id`, `instance_id`(nullable), `actor_key_prefix`, `actor_permissions`, `actor_label`, `action`, `event_payload`(jsonb), `created_at`, `row_hash`, `prev_row_hash` | Architecture §5.2 | `app/models/admin_audit_log.py:1084-1328` | CONFORMS | All required columns present. Note: Architecture §5.2 calls the JSONB field `event_payload`; code uses `before_json`/`after_json` (split diff pattern) — functionally equivalent. Architecture calls the actor field `actor_user_id`/`actor_type`; code uses `actor_key_prefix`/`actor_permissions`/`actor_label`. Shape is semantically aligned, vocabulary differs. |
| 4.2 | Full event_type enumeration — spot-check 10 critical | Architecture §5.2, §5.10 | `app/models/admin_audit_log.py` `ALLOWED_ACTIONS` tuple | CONFORMS | Spot-checked: `ACTION_ESCALATION_FIRED` (line 595/837), `ACTION_ACCOUNT_CLOSURE_INITIATED` (line 794), `ACTION_DATA_EXPORT_REQUESTED` (line 97-99 in export service / line 782+ in model), `ACTION_RETENTION_ENFORCE` (line 733), `ACTION_SUBSCRIPTION_CREATE` (line 736), `ACTION_BUDGET_EXHAUSTED` (line 854), `ACTION_OVERAGE_REPORTED` (line 856), `ACTION_CONSENT_GRANT` (line 730), `ACTION_KNOWLEDGE_SOURCE_CREATED` (line 797), `ACTION_SIBLING_GRANT_AUTHORED` (line 811). All present in `ALLOWED_ACTIONS`. |
| 4.3 | Append-only: no UPDATE/DELETE grants on `admin_audit_logs` to `luciel_app` | Architecture §5.3 | `alembic/versions/arc9_1_a_tenant_isolation_seal.py:167` | CONFORMS | `"REVOKE UPDATE, DELETE ON admin_audit_logs FROM luciel_app"` at line 167. `luciel_audit_archiver` has SELECT+UPDATE only (Arc 10); UPDATE used only to stamp `cold_archived_at`. |
| 4.4 | RLS WITH CHECK (write-side enforcement) | Architecture §5.3 | `alembic/versions/arc9_c3_1_rls_admin_audit_logs.py:52,123,142`; `arc9_c4_3f_rls_instance_admin_audit_logs.py:92`; `arc10_gap7_audit_loosen_instance_for_admin_scope.py:103,127` | CONFORMS | Multiple RLS policies with `WITH CHECK` clause; PERMISSIVE policy fences on `admin_id = current_setting('app.admin_id', true)`. Arc 10 Gap 7 loosened `luciel_instance_id` to nullable correctly. |
| 4.5 | SHA-256 hash chain: `row_hash = sha256(canonical_content + prev_row_hash)`; genesis `prev_row_hash = '0'*64` | Architecture §5.3 | `app/repositories/audit_chain.py:98-180` | CONFORMS | `GENESIS_PREV_HASH = "0" * 64` (line 98); `canonical_row_hash` at line 155; `_CHAIN_FIELDS` tuple drives field set; advisory lock prevents fork (line 244); before_flush event installed at module-import time |
| 4.6 | `data_export_self_serve` audit event (Architecture §5.10 / §5.2) | Architecture §5.10 line 2153 | `app/models/admin_audit_log.py` | **MISSING** | Architecture §5.10 explicitly adds `data_export_self_serve` to the audit log enumeration. This constant is absent from `admin_audit_log.py` and from `ALLOWED_ACTIONS`. The export audit uses `ACTION_DATA_EXPORT_REQUESTED` which is the enqueue-level event — semantically different from the self-serve intent distinction the spec requires. |
| 4.7 | Enterprise S3 WORM nightly export | Architecture §5.3 | `app/services/audit_retention_service.py`; `alembic/versions/arc10_lifecycle_subsystem.py:95` | CONFORMS | Nightly archiver writes to S3 `audit-cold-archive/` prefix per tier window (Free=30d, Pro=365d, Ent=2555d). **BLOCKED-EXTERNAL**: S3 bucket Object Lock / Compliance mode (WORM) configuration cannot be verified without AWS control-plane access — see BLOCKED-EXTERNAL section. |

---

## 5. Cluster 5 — Retention Policy

| # | Requirement | Doc cite | Implementing artifact(s) | Status | Notes / evidence |
|---|------------|----------|--------------------------|--------|-----------------|
| 5.1 | Retention table per tier: transcripts 30d/1y/7y; summaries 90d/1y/7y; audit 30d/1y/7y | Architecture §3.4.10 | `app/services/audit_retention_service.py:96-98`; `app/policy/retention.py:48-92`; `retention_rules.py` | **DRIFTED** | Audit tier-retention IS correctly implemented (30/365/2555 days at `audit_retention_service.py:96-98`). However, `retention_rules.py` `PLATFORM_DEFAULTS` seeds: sessions=730d, messages=730d, memory_items=365d, traces=365d — these are single GLOBAL values, NOT per-tier values. Architecture §3.4.10 requires tier-conditional 30d/1y/7y for transcripts (sessions+messages). The platform seeds do not segment by tier; there is no per-tier seed logic. `app/policy/retention_rules.py` does not exist — the defaults are in `app/services/audit_retention_service.py`. |
| 5.2 | Transcripts to S3 cold after 90 days | Architecture §3.4.10 | Not found for conversations | **MISSING** | Architecture §3.4.10: "moved from hot Postgres to S3 cold storage after 90 days." The S3 cold move is implemented for `admin_audit_logs` only (via `audit_retention_service.py`). No equivalent S3 cold-archive path exists for conversation transcripts (sessions/messages tables). `RetentionService._batched_delete` only does DELETE — no cold-move. |
| 5.3 | Deterministic deletion logged `data_retention_hard_delete` | Architecture §3.4.10, §5.2 | `app/models/retention.py:DeletionLog`; `app/policy/retention.py:311-335` | **DRIFTED** | `DeletionLog` is written at `retention.py:315`. Architecture §3.4.10 specifies `event_type = 'data_retention_hard_delete'` in `admin_audit_log`. Code uses a separate `DeletionLog` table (not `admin_audit_log`), and `ACTION_RETENTION_ENFORCE` in `admin_audit_log` for policy enforcement. The `DeletionLog` is functionally equivalent but the doc spec says `admin_audit_log` with `data_retention_hard_delete` event type — that specific event constant does not exist. |
| 5.4 | `app/policy/retention_rules.py` (canonical retention policy) | Architecture §8 (implied) | `app/services/audit_retention_service.py:1-477` + `app/policy/retention.py` | DRIFTED | `app/policy/retention_rules.py` does not exist as a standalone file. Platform defaults live in docstring/inline dict in `audit_retention_service.py`. Minor path drift. |
| 5.5 | `app/services/audit_retention_service.py` tier-aware cold archive | Architecture §5.3 | `app/services/audit_retention_service.py` | CONFORMS | Per-tier cold archival (30/365/2555 days), S3 prefix `audit-cold-archive`, hash-chain extension across boundary, batch processing with `luciel_audit_archiver` role constraints. |

---

## 6. Cluster 6 — Analytics

| # | Requirement | Doc cite | Implementing artifact(s) | Status | Notes / evidence |
|---|------------|----------|--------------------------|--------|-----------------|
| 6.1 | `app/analytics/` — doctrine-anchored path (Architecture §8) | Architecture §8 | **NOT FOUND** | **MISSING** | Architecture §8 doctrine-anchored path `app/analytics/` does not exist. |
| 6.2 | Free: basic counters only (conversations, leads, budget bar) | Architecture §3.9 | `app/services/dashboard_service.py`; `app/api/v1/dashboard.py` | **DRIFTED** | `DashboardService.get_tenant_dashboard` at `dashboard_service.py:1-412` returns `TenantDashboard` with turn_count, unique_user_count, escalation_count, tool_call_count, seven_day_trend — no tier gating. A Free admin gets the same aggregates as Pro. The spec requires Free to receive only basic counters. |
| 6.3 | Pro: per-team-member breakdowns (escalation→response time, conversion proxies per member) | Architecture §3.9 | Not found | **MISSING** | No per-team-member analytics. `dashboard_service.py` returns top_luciel_instances but no team-member-level breakdown metrics. |
| 6.4 | Enterprise: fleet view, SLA-adherence reporting, CSV export | Architecture §3.9 | Not found | **MISSING** | No fleet-level view, no SLA-adherence reporting, no analytics CSV export in `dashboard_service.py` or `dashboard.py`. |
| 6.5 | Read-only over existing stores; no new write path; RLS-scoped | Architecture §3.9 | `app/services/dashboard_service.py:17-19` | CONFORMS | Docstring explicitly: "No new DB writes. The service reads traces … nothing else." Double-scope enforcement at SQL + post-query loop (lines 28-31). |
| 6.6 | `instance_operator` scope respected | Architecture §3.9 | `dashboard_service.py`; Arc 7 tier-aware middleware | CONFORMS | `scope_prompt_preflight` and scope enforcement at HTTP layer; `DashboardService` trusts pre-validated scope. |

**Summary:** Analytics is PARTIALLY PRESENT. The basic read-only dashboard layer (`dashboard_service.py`, `app/api/v1/dashboard.py`) conforms for read-only/RLS requirements. The doctrine-anchored path is missing; tier-shaping (Free vs Pro vs Enterprise depth) is not enforced; Pro team-member analytics and Enterprise fleet/SLA/CSV export are absent.

---

## 7. Cluster 7 — Data Export / Portability

| # | Requirement | Doc cite | Implementing artifact(s) | Status | Notes / evidence |
|---|------------|----------|--------------------------|--------|-----------------|
| 7.1 | Export ZIP structure: `conversations/{session}.json` + `conversations.csv`, `leads.json`+`csv`, `knowledge/{original files}`+`manifest.json`, `instances.json` (provider+non-secret+status NEVER secrets), `audit_log.jsonl` | Architecture §5.10 | `app/services/data_export_service.py` | **DRIFTED** | Implemented as `.tar.gz` (not ZIP). Bundle contains: `conversations.jsonl`, `leads.jsonl`, `audit_log.csv`, `instances.json`, `escalations.csv`, `knowledge_sources/manifest.json`, `knowledge_sources/chunks/*.jsonl`. **Deviations vs §5.10 spec:** (a) Format is tar.gz not ZIP; (b) conversations are JSONL (not per-session JSON files in `conversations/` dir); (c) no `conversations.csv` flat CSV; (d) no `leads.csv` (only JSONL); (e) knowledge sources contain reconstructed chunks (NOT original uploaded files — Option-2 arc10 decision, documented in service); (f) `audit_log.csv` instead of `audit_log.jsonl`. |
| 7.2 | 7-day presigned URL TTL | Architecture §5.10 | `app/services/data_export_service.py:117-121` | CONFORMS | `_TIER_URL_TTL_SECONDS`: free=7d, pro=7d, enterprise=90d. §5.10 says "7 days" for download link — service matches for Free/Pro. Enterprise gets 90 days (more generous). |
| 7.3 | Self-serve tiering: Free=closure-only / Pro+Ent=anytime | Architecture §5.10 | `app/api/v1/admin.py:2362+` | **DRIFTED** | `POST /account/export` performs NO tier check on request. A Free admin in non-closure state (`closure_initiated_at IS NULL`) can call the endpoint successfully — `triggered_by` is set to `"admin_request"` not gated. Vision §7 / Architecture §5.10 explicitly requires Free to be "at closure only." No `403` path for active Free admin. |
| 7.4 | `data_export_self_serve` audit event (§5.2) | Architecture §5.10:2153 | `app/models/admin_audit_log.py` | **MISSING** | `ACTION_DATA_EXPORT_SELF_SERVE` constant does not exist. Existing events are `ACTION_DATA_EXPORT_REQUESTED` (enqueue) / `ACTION_DATA_EXPORT_GENERATED` (ready). The spec-required distinct event for self-serve Pro/Ent requests is absent. |
| 7.5 | `instances.json` NEVER includes secrets | Architecture §5.10, §3.8.3 | `data_export_service.py:_write_instances` (line 700) | CONFORMS | Uses `row_to_json(i.*)` on instances table. Credential secrets are in `aws_secrets_manager` via `credential_ref` pointer — the instances row contains `credential_ref` (a reference string), not the token. No `config_json` secrets in the instances table (secrets live in AWS SM per §3.8.3). |
| 7.6 | `closure_service.py` export at closure | Architecture §3.6.6 | `app/services/closure_service.py` | CONFORMS | Closure flow initiates export via `DataExportService.enqueue` with `triggered_by="admin_request"` — closure offers export before deactivation. |

---

## CONFLICTS

### C-1: Uptime SLA — Vision vs entitlements.py
- **Vision §7** (canonical, wins): Pro=99.9% monthly, Enterprise=99.95% monthly MSA-backed
- **`app/policy/entitlements.py:284`**: `uptime_sla_pct=99.5` for Pro
- **`app/policy/entitlements.py:334`**: `uptime_sla_pct=99.9` for Enterprise
- **Impact**: Both tiers are one SLA tier below Vision. Pro commits to 99.5% but markets 99.9%. Enterprise commits to 99.9% but MSA should be 99.95%.
- **Resolution needed**: Entitlements must be updated to Vision §7 values. This is not a stale doc — Vision §7 is the buyer-facing commitment.

### C-2: Export Bundle Format — Architecture §5.10 vs data_export_service.py
- **Architecture §5.10**: ZIP file with `conversations/{session_id}.json` per-file structure, `conversations.csv`, `leads.json`, `leads.csv`, original knowledge files, `audit_log.jsonl`
- **Code**: tar.gz, JSONL not per-file, no CSVs for conversations/leads, reconstructed chunks not originals, `audit_log.csv` not JSONL
- **Note**: The Arc 10 "Option-2" decision (original files not retained) is explicitly documented in the service. The remaining format divergences (ZIP vs tar.gz, CSV row shapes) are undocumented drift.

### C-3: Retention PLATFORM_DEFAULTS — Tier-Conditional vs Single-Value
- **Architecture §3.4.10**: Transcript retention is 30d (Free) / 1y (Pro) / 7y (Enterprise) — per-tier
- **`retention_rules.py` (embedded in audit_retention_service.py)**: Single global defaults — sessions=730d, messages=730d
- **Impact**: Production Postgres retention policy seeds a 2-year window that does not match Vision/Architecture for Free tier (should be 30d) or Enterprise (should be 7y). A Free tenant's conversation data survives 730 days instead of 30.

---

## §9 TOUCHED

Architecture §9 authored-but-unratified commitments relevant to this slice:

| §9 # | Commitment | Authored value | Value found in code | Status |
|------|-----------|----------------|---------------------|--------|
| (SLA item, not numbered in §9) | Uptime SLA values | Pro=99.9%, Ent=99.95% | Pro=99.5%, Ent=99.9% | DRIFTED — Vision §7 is the source; code is below both |
| §9 item — Export self-serve tiering | "Free=at closure only" | Free=closure-only, Pro/Ent=anytime | Not enforced at API layer | MISSING enforcement |

---

## RESIDUE DETAIL

No residue items identified in this slice. All code examined is either implementing or attempting to implement a doc-specified requirement.

---

## BLOCKED-EXTERNAL

| Item | What is needed | Why |
|------|---------------|-----|
| Enterprise S3 WORM (Architecture §5.3) | AWS console or CLI: verify `luciel-audit-cold-archive` (or equivalent) S3 bucket has Object Lock enabled, mode=COMPLIANCE, retention period=7 years | `audit_retention_service.py` writes to S3 but bucket-level Object Lock config is an AWS control-plane setting invisible to code. Architecture §5.3 and Architecture compliance checklist item #15 require Compliance-mode WORM. |
| Stripe metered price IDs | Verify `settings.stripe_price_overage_pro_monthly` and `settings.stripe_price_overage_pro_annual` are provisioned in the Stripe dashboard | `_OVERAGE_PRICE_CONFIG_KEY` in `entitlements.py:743-750` references config keys; cannot verify the Stripe price objects exist without API/dashboard access. |
| Enterprise contractual overage rate | Verify `admin_tier_overrides` table has a mechanism for per-contract overage rate entry | Architecture §6: "Negotiated overage rate in MSA" for Enterprise. `resolve_entitlement` override hook exists (`entitlements.py:352-391`) but Enterprise overage rate is `None` (no fixed Stripe price). Contract-rate population pathway needs ops verification. |

---

## 12-LINE HEADLINE SUMMARY

1. **Tier matrix (Cluster 1):** 18/23 axes CONFORMS. Critical DRIFTED: uptime SLA (Pro 99.5% vs required 99.9%; Ent 99.9% vs required 99.95%). MISSING: graph store entitlement axis. DRIFTED: Enterprise channels missing voice/WhatsApp; Free self-serve export gate absent.
2. **Billing/metering (Cluster 2):** Mostly CONFORMS. `BudgetMeter` (Redis idempotency), `ConversationOverageLedger` (durable Postgres), `invoice.paid` overage cycle, 80%/100% alerts all correctly implemented. One path drift: `app/billing/metering.py` (Architecture §8 canonical) does not exist; capability lives in `app/runtime/budget_meter.py` + `app/services/overage_billing.py`.
3. **Escalation DELIVERY (Cluster 3):** Severely MISSING. Architecture §3.5 delivery layer is a stub. No `NotificationAdapter` implementations, no `app/notifications/` directory, no retry/backoff, no idempotency key on escalation_events, no chain walker, no ack mechanism. Five §3.5.6 delivery-phase audit events undefined. Only CONFORMS: event row written before delivery, tier-shaped channel set computed, `escalation_fired` audit recorded.
4. **Audit log immutability (Cluster 4):** Strongly CONFORMS. SHA-256 hash chain (`audit_chain.py`), advisory lock, before_flush event, REVOKE UPDATE/DELETE grants, RLS WITH CHECK all in place. One MISSING: `data_export_self_serve` audit event constant absent despite explicit §5.10 requirement.
5. **Retention policy (Cluster 5):** DRIFTED. Audit cold-archive CONFORMS (30/365/2555d per tier). Transcript/summary retention DRIFTED — platform defaults are single-value 730d/365d (not per-tier). S3 cold archive for conversations (after 90d) is MISSING — only audit logs get cold-archived. `data_retention_hard_delete` event in `admin_audit_log` MISSING (code uses separate `DeletionLog` table).
6. **Analytics (Cluster 6):** PARTIALLY PRESENT. Basic dashboard exists (`dashboard_service.py`, `app/api/v1/dashboard.py`) and is read-only/RLS-scoped (CONFORMS). Doctrine path `app/analytics/` MISSING. Tier gating absent — Free gets same view as Pro. Pro team-member analytics and Enterprise fleet/SLA/CSV export MISSING.
7. **Data export (Cluster 7):** DRIFTED. Export service functional but diverges from §5.10 spec: tar.gz not ZIP, JSONL not per-file JSON, no conversations.csv/leads.csv, reconstructed chunks not originals (documented Arc 10 decision). Free-tier "closure-only" gate NOT enforced at API level. `data_export_self_serve` audit event MISSING.
8. **Counts by status:** CONFORMS=28, DRIFTED=12, MISSING=12, AMBIGUOUS=2, BLOCKED-EXTERNAL=3, RESIDUE=0.
9. **Highest-risk finding:** Escalation delivery (Cluster 3) — the entire §3.5 delivery layer is unbuilt. Enterprise chain walker, retry backoff, and delivery audit events are all MISSING. The five §3.5.6 audit event types (notification_sent, delivery_failed, chain_step, acked, chain_end_fallback) are absent from admin_audit_log constants.
10. **Second highest-risk:** Uptime SLA values in `entitlements.py` are below Vision §7 commitments — Pro 99.5% vs sold 99.9%; Enterprise 99.9% vs MSA-committed 99.95%. This is a contractual liability.
11. **Retention drift:** Conversation transcript retention defaults (730d global) contradict Architecture §3.4.10 per-tier requirements (Free=30d, Pro=1y, Ent=7y). Free tenant data survives 2 years instead of 30 days — a PIPEDA Principle 4.5 limiting-retention concern.
12. **Architecture §8 path drift summary:** Three of nine doctrine-anchored paths are absent: `app/billing/metering.py` (→`app/runtime/budget_meter.py` + `app/services/overage_billing.py`), `app/notifications/` (stub only), `app/analytics/` (→`app/services/dashboard_service.py`). Remaining six paths not audited in this slice.
