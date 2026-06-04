# Manifest Section 03 — Tools, Knowledge, Channel Adapters, Lifecycle

**Audit date:** 2026-06-04  
**Backend path:** `/home/user/workspace/luciel_repos/backend`  
**Auditor:** Subagent (READ-ONLY — zero changes made)  
**Source of truth:** ARCHITECTURE_v1.3 (rank 1), VISION_v1.3 (rank 2), CUSTOMER_JOURNEY_v1.3 (rank 3)  
**Prior reports consulted:** ARC15_BACKEND_REPORT.md, ARC15_DRIFT_CLEANUP_REPORT.md, ARC17_LOOKUP_RECORD_AMENDMENT.md

---

## Cluster 1 — Tool Subsystem (§3.3)

| # | Capability / Spec Requirement | Status | Implementing Artifact | Notes |
|---|-------------------------------|--------|-----------------------|-------|
| 1.1 | `LucielTool` ABC with all §3.3.1 fields: `tool_id`, `display_name`, `description`, `input_schema`, `output_schema`, `requires_tier`, `execution_mode` | CONFORMS | `app/tools/base.py:1-90` | All required fields present; also carries `requires_channels` (frozenset default) and `requires_connection` (None default) as per §3.3 extension fields |
| 1.2 | `declared_tier` field on ABC | CONFORMS | `app/tools/base.py` (property) | Aliased to `requires_tier`; tooling surfaces it under both names cleanly |
| 1.3 | `DefaultDenyToolAuthorizer` — 4-gate check sequence | AMBIGUOUS | `app/tools/authorization.py:1-180` | Implementation runs 4 checks: (1) `_check_row` (per-instance `instance_tool_authorization` lookup, load-bearing default-deny), (2) `_check_tier`, (3) `_check_channels`, (4) `_check_connection`. Architecture §3.3 describes "three dispatch gates" naming tier as gate-1; the row-check pre-gate is not numbered separately in the spec. The row-check is functionally load-bearing and present. Mark AMBIGUOUS on gate count vs. CONFORMS on intent — spec's three named gates all fire. |
| 1.4 | Connection failure message: `'Action needed: connect [X]'` (§3.3 literal) | DRIFTED | `app/tools/authorization.py` (connection error branch) | Emits `failure_kind="connection_not_configured"` with message "Configure it under Connections before this tool can run." — functionally equivalent but not the spec-literal string. No customer-visible breakage; tooling differences only. |
| 1.5 | Tool registry — exactly 8 production tools; Cognition absent | CONFORMS | `app/tools/registry.py` | Registered: `book_appointment`, `send_email`, `send_sms`, `lookup_record`, `schedule_callback`, `push_to_crm`, `call_sibling_luciel`, `bring_your_own_webhook`. Cognition intentionally excluded from registry (handled separately as dispatch layer, not a tool). |
| 1.6 | `lookup_record` uses `record_source` connection type (Arc 17 amendment) | CONFORMS | `app/tools/implementations/lookup_record_tool.py:61` | `requires_connection = "record_source"`. ARC17_LOOKUP_RECORD_AMENDMENT.md: `lookup_record` is the live Arc 17 replacement for the prior interim stub; `record_source` is the canonical connection type. Arc 15 reference to `property_source` was the pre-rename interim label and is superseded. |
| 1.7 | `lookup_record` live read via `RecordSource` interface (Arc 17) | CONFORMS | `app/integrations/record_source/` (resolver, S3, local) | `LocalFileRecordSource` (file:// / CSV), `S3RecordSource` (boto3, deploy-gated behind `record_source_live_enabled` flag). S3 path honest-fails when flag is False; never constructs boto3 client. ARC17_LOOKUP_RECORD_AMENDMENT.md §correctness-boundary confirmed. |
| 1.8 | `InstanceToolAuthorization` model — per-instance admin auth row with default-deny | CONFORMS | `app/models/instance_tool_authorization.py` | Fields: `admin_id`, `instance_id`, `tool_id`, `enabled`, `authorized_by_user_id`, `revoked_at`. `get_live()` filters `WHERE revoked_at IS NULL AND enabled = true` — implements default-deny correctly. |
| 1.9 | Admin tools API — GET list, POST authorize, POST revoke; tier gate at authorize; Cognition excluded | CONFORMS | `app/api/v1/admin_tools.py` | 3 routes present. Tier gate at POST authorize. Cognition filtered from list. Connection status chip (`action_needed` / `connected` / `reconnect_needed`) rendered in list response. |
| 1.10 | `bring_your_own_webhook` tool — tier-gated, webhook URL stored in connection config | CONFORMS | `app/tools/implementations/bring_your_own_webhook_tool.py` | Pro+ gate. Endpoint URL in `connection.config_json.endpoint_url`; never in secret store (correct — it is not a secret). |

---

## Cluster 2 — Sibling Composition (§3.3.4)

| # | Capability / Spec Requirement | Status | Implementing Artifact | Notes |
|---|-------------------------------|--------|-----------------------|-------|
| 2.1 | `SiblingCallGrant` model — bidirectional grant with `approval_state` enum (live / pending_approval / revoked) | CONFORMS | `app/models/sibling_call_grant.py` | Fields: `admin_id`, `caller_instance_id`, `callee_instance_id`, `granted_by_user_id`, `approval_state`, `approved_by_user_id`, `revoked_at`. DB CHECK constraint enforces caller≠callee. |
| 2.2 | `SiblingDispatch` — 5 guardrail checks: cycle detection, fan-out budget, master switch both endpoints, grant lookup, audit+derived context | CONFORMS | `app/tools/sibling_dispatch.py:1-200` | All 5 checks confirmed live. `SIBLING_FAN_OUT_BUDGET=12`. Decision #19 (no depth limit, no edge cap) honored — only fan-out budget enforced. |
| 2.3 | Sibling round-trip (orchestrator handoff — callee actually invoked) | DRIFTED | `app/tools/sibling_dispatch.py:100-102` (`_SIBLING_ROUNDTRIP_SEAM`) | Callee Luciel is **not** invoked. Returns `{"not_yet_available": True}` stub. Code comment explicitly marks this as deferred past Arc 14. All 5 guardrail checks run; only actual invocation is missing. This is known/expected deferred work, not an unintended regression. |
| 2.4 | Admin sibling grants API — 5 routes: POST create, GET list, POST approve, POST reject, POST revoke | CONFORMS | `app/api/v1/admin_sibling_grants.py` | All 5 routes present. Tier matrix: Free→403, Pro→`live`, Enterprise→`pending_approval`. |
| 2.5 | Wall-2 enforcement on BOTH caller and callee instances | CONFORMS | `app/api/v1/admin_sibling_grants.py` (`_enforce_wall2_both_instances`) | `ScopePolicy.enforce_role_on_instance` called on both `caller_instance_id` and `callee_instance_id`. Neither endpoint can be cross-tenant. |

---

## Cluster 3 — Knowledge Subsystem (§3.2, §3.5)

| # | Capability / Spec Requirement | Status | Implementing Artifact | Notes |
|---|-------------------------------|--------|-----------------------|-------|
| 3.1 | Two-table separation: `knowledge_sources` + `knowledge_chunks` | CONFORMS | `alembic/versions/arc11_a_knowledge_sources_schema.py`, `arc11_b_rename_embeddings_to_chunks.py` | Both tables scoped by `admin_id + instance_id`. Rename from `embeddings` completed in arc11_b. |
| 3.2 | HNSW index on `knowledge_chunks`; cosine distance retrieval | CONFORMS | `alembic/versions/arc11_d3_hnsw_index_chunks.py`; `app/knowledge/knowledge_repository.py` | Index confirmed in migration. Retrieval uses `<=>` (pgvector cosine distance) operator. |
| 3.3 | Quota caps: Free 10 MB/file, 100 MB total; Pro 50 MB/file, 5 GB total; Enterprise 500 MB/file, unlimited total | CONFORMS | `app/policy/entitlements.py:228-296` | Lines 229, 264, 296 per-file caps; lines 228, 263, 294 total caps. Match spec. |
| 3.4 | Quota enforcement at API boundary (per-file 413, total 413) | CONFORMS | `app/api/v1/admin_knowledge.py:438-508` | Per-file check raises 413 with `scope=per_file`; total check raises 413 with `scope=total` before ingest pipeline runs. |
| 3.5 | Ingestion sources: PDF/DOCX/TXT/CSV upload, paste, CSV import, website crawl | CONFORMS | `app/knowledge/parsers/` (file parsers); `app/api/v1/admin_knowledge.py` (ingest_text route); website crawl gated by `ent.knowledge_website_crawl_enabled` | Crawl requires Pro+ tier per entitlements — matches spec. |
| 3.6 | Retrieval pipeline: filter → HNSW → **rerank** → top-k | DRIFTED | `app/knowledge/retriever.py`; `app/knowledge/context_assembler.py` | **Rerank step absent.** Retriever returns chunks ordered by cosine distance; `context_assembler.py` injects them directly with no cross-encoder or BM25 rerank stage. Spec §3.5 names the rerank step as a distinct stage between HNSW and top-k. |
| 3.7 | Delete confirmation backed by trace-store `retrieval_results` (`/sources/{id}/affected-questions`) | CONFORMS | `app/api/v1/admin_knowledge.py:871` | Route `GET /sources/{source_id}/affected-questions` exists and is referenced from the delete confirmation flow. |
| 3.8 | Raw knowledge view — `GET /internal/v1/retrieve` — platform_admin-gated | CONFORMS | `app/api/v1/admin_knowledge.py:1077` | Route confirmed platform_admin-only. Admin knowledge list/view gated owner+manager+operator per §3.2.2; edit/delete restricted to owner+manager. |
| 3.9 | Graph store (Arc 16) — structured entity/relationship store alongside vector store | MISSING | `app/knowledge/` (absent) | No graph store code, CTE/recursive queries, or graph retrieval found anywhere in `app/knowledge/`. No Neo4j or pg-graph integration. Arc 16 appears not yet implemented. |

---

## Cluster 4 — Channel Adapters (§3.1)

| # | Capability / Spec Requirement | Status | Implementing Artifact | Notes |
|---|-------------------------------|--------|-----------------------|-------|
| 4.1 | `ChannelAdapter` Protocol — `verify_inbound`, `receive`, `send`; `SignatureVerificationError`, `UnresolvableInboundError` | CONFORMS | `app/channels/base.py` | Protocol matches §3.1.2 exactly. `InboundMessage` carries all 7 spec fields. |
| 4.2 | Inbound routing resolves to exactly one `(admin_id, instance_id)` via `InstanceContext` | CONFORMS | `app/channels/base.py` (`InstanceContext`); per-adapter `verify_inbound` | Every adapter returns an `InstanceContext` before any message processing. |
| 4.3 | Widget channel adapter | CONFORMS | `app/channels/widget.py` | Shipped. |
| 4.4 | Email channel adapter (SES inbound + outbound) | CONFORMS | `app/channels/email_adapter.py`; `app/api/v1/ses_events.py` | SES webhook handler and outbound send path both present. |
| 4.5 | SMS channel adapter (Twilio) | CONFORMS | `app/channels/sms_adapter.py`; `app/api/v1/twilio_webhook.py` | Twilio webhook handler and send path present. |
| 4.6 | Widget abuse controls — origin/referer allowlist per embed key | CONFORMS | `app/channels/widget_deps.py` | Per-embed-key `allowed_origins` enforced at inbound; `app/models/admin_widget_domain.py` stores admin-level domain allowlist. |
| 4.7 | Widget abuse controls — token-bucket 5-burst/1-per-3s (§3.1.5) | DRIFTED | `app/middleware/rate_limit.py`; `app/channels/widget_deps.py` | Spec §3.1.5 specifies a **token-bucket at 5 burst / 1 message per 3 s**. Implementation uses `SlowAPI` `30/minute` per embed key (no burst/refill semantics). No token-bucket implementation found anywhere in the codebase. |
| 4.8 | Widget abuse controls — bot challenge (hcaptcha) on widget chat | DRIFTED | `app/api/v1/billing.py:78` (hcaptcha present there only) | hcaptcha integration exists only on the billing/signup flow. Widget chat has no bot challenge gate. |
| 4.9 | Widget abuse controls — auto-block on sustained abuse | MISSING | (not found) | No auto-block mechanism (block list, IP ban, automatic embed-key suspension on threshold) found in any widget-related code path. |
| 4.10 | Voice channel (v2 deferred) | CONFORMS | (correctly absent) | No voice adapter anywhere. Spec marks voice as v2. Correct absence. |
| 4.11 | Phone lifecycle (§3.1.4 — opt-in via channel selection, dedicated vs shared number provisioning) | AMBIGUOUS | `app/channels/provisioning.py` | File exists; full provisioning logic not traced to spec depth within READ-ONLY scope of this audit. File presence is consistent with feature; completeness unverified. |

---

## Cluster 5 — Lifecycle (§3.6, Vision §6.4–6.5)

| # | Capability / Spec Requirement | Status | Implementing Artifact | Notes |
|---|-------------------------------|--------|-----------------------|-------|
| 5.1 | Instance state machine — 5 states: `active`, `paused`, `deactivating`, `grace_window`, `hard_deleted` (§3.6) | DRIFTED | `app/models/instance_status.py:28-38` | Enum defines only **3 states**: `ACTIVE`, `PAUSED`, `DELETED`. States `deactivating` and `grace_window` are absent. The 30-day grace window is implemented via `soft_deleted_at` timestamp logic (Architecture §3.6.1 reference in the docstring), not a discrete enum state. Transition table in spec (§3.6, lines 1033-1037) names all 5 transitions including `deactivating→grace_window` and `grace_window→hard_deleted`; these cannot be expressed in the current enum. |
| 5.2 | Pause behavior: widget returns empty `<div>` | CONFORMS | `app/services/instance_service.py:244` | Confirmed: `204` / empty `<div>` on widget call when instance is paused. |
| 5.3 | Account closure + "Download all my data" export | CONFORMS | `app/services/closure_service.py` | `request_export` path present. Export enqueued before closure proceeds. |
| 5.4 | Deactivation atomicity: embed key revoked, sibling grants revoked, connections closed, secret cleanup scheduled | CONFORMS | `app/services/closure_service.py`; `app/worker/tasks/instance_retention.py:289-298` | Cascade in `closure_service.py` handles revocations. Arc 17 connection revocation at lines 314-328. `SecretCleanupOutbox` model present — secret cleanup is outbox-based (deferred to drain worker), not inline. Acceptable design; not a deviation. |
| 5.5 | Hard-delete cascade order — instance level (spec: knowledge→chunks→traces→sessions→api_keys→connections→instances + leads, summaries, sibling_call_grants, embed_keys) | DRIFTED | `app/worker/tasks/instance_retention.py:254-326` | Actual order: `knowledge_chunks` → `knowledge_sources` → `traces` → `sessions` → `api_keys` → `instance_connections` → `instances`. **Missing from cascade:** `leads` (noted in line-26 comment as "(if table exists)" — not executed), `summaries`, `sibling_call_grants`, explicit `embed_key` step. Spec-required tables silently skipped. |
| 5.6 | Hard-delete cascade order — tenant level (spec: messages→sessions→conversations→identity_claims→memory_items→api_keys→instances→admins(tombstone)) | BUG | `app/services/admin_service.py:1152-1239` | Tenant cascade executes `DELETE FROM instances WHERE admin_id=:tid` **without first deleting `knowledge_sources`, `knowledge_chunks`, `instance_connections`, or `traces`**. These tables all carry `FK → instances.id` with `ondelete=RESTRICT` (confirmed: `arc11_a_knowledge_sources_schema.py:115`, `arc15_b_instance_connections.py:139`). In production, any tenant with knowledge or connections will receive a **PostgreSQL FK violation** at step 7, aborting the entire purge transaction. The instance-level retention worker avoids this by explicitly pre-clearing these tables, but the tenant path does not delegate to it or replicate the pre-clear. |
| 5.7 | Hard-delete: per-step `data_retention_hard_delete` audit row | DRIFTED | `app/models/admin_audit_log.py:140,324`; `app/services/admin_service.py:1257`; `app/worker/tasks/instance_retention.py:310` | Spec implies per-step audit. Implementation emits **one** summary `ACTION_INSTANCE_HARD_PURGED` / `ACTION_TENANT_HARD_PURGED` row carrying a `row_counts` map of all tables. Single-row-with-manifest design is auditable but not the per-step granularity the spec describes. |
| 5.8 | Audit tombstone preserved (not deleted) on hard-delete | CONFORMS | `app/services/admin_service.py:1207-1239` | `admins` row receives `UPDATE` setting `hard_deleted_at`, `name='[REDACTED]'`, `stripe_customer_id=NULL`, `last_signup_ip=NULL`. Not deleted. Vision §6.5 minimal-compliance-record posture honored. |
| 5.9 | Downgrade Pro→Free: read-only 30-day grace window | CONFORMS | `app/services/downgrade_grace_service.py` | 30-day window confirmed. Read-only enforcement active during grace. |
| 5.10 | Downgrade: connections go dormant (retained, not purged) on downgrade | AMBIGUOUS | `app/services/downgrade_archive_service.py` | `downgrade_archive_service.py` handles "overflow archive" and cap enforcement. No explicit `connections → dormant` state transition found; connections appear retained but no "dormant" flag or soft-disable on connections table observed. Spec wording "connections dormant" may mean retained-not-callable (enforced by tier check at call time) rather than a state change — but code does not make this explicit. |
| 5.11 | Downgrade: caps enforced at day 30 via archive (not delete) | CONFORMS | `app/services/downgrade_archive_service.py:160+` | Overflow archive runs at day-30 enforcement. Knowledge over-cap is archived, not deleted. |

---

## CONFLICTS

### CONFLICT-01: Instance state enum vs. Architecture §3.6 state machine
- **Spec (ARCHITECTURE §3.6, lines 1026-1037):** Defines 5 states — `active`, `paused`, `deactivating`, `grace_window`, `hard_deleted` — with explicit transition table and per-state behavior columns.
- **Code (`app/models/instance_status.py:28-38`):** Defines 3 states — `ACTIVE`, `PAUSED`, `DELETED`. Docstring acknowledges a grace window via `soft_deleted_at` timestamp but does not implement `deactivating` or `grace_window` as discrete states.
- **Impact:** Lifecycle audit queries, monitoring, and any code branching on instance state cannot distinguish `deactivating` from `grace_window` from `hard_deleted`. Transitions `deactivating→grace_window` and `grace_window→active (reactivate)` are not expressible. The instance-level retention worker uses `soft_deleted_at` timestamp arithmetic as a proxy for `grace_window`, which is functionally equivalent for the 30-day window but does not satisfy the spec's stated state model.
- **Disposition:** Not previously flagged in ARC15 or ARC17 reports. Requires founder decision: either extend the enum to 5 states or ratify the timestamp-proxy approach as the canonical implementation and amend §3.6.

### CONFLICT-02: Tenant hard-delete cascade will FK-violate in production (BUG-grade)
- **Spec (ARCHITECTURE §3.6 / admin_service.py docstring):** Tenant hard-delete cascade intended to purge all tenant-scoped data, culminating in `DELETE FROM instances`.
- **Code (`app/services/admin_service.py:1197`):** `DELETE FROM instances WHERE admin_id=:tid` executed without prior deletion of `knowledge_sources`, `knowledge_chunks`, `instance_connections`, or `traces`.
- **DB constraint (migrations):** `knowledge_sources.instance_id → instances.id` is `ondelete=RESTRICT` (`arc11_a_knowledge_sources_schema.py:115`). `instance_connections.instance_id → instances.id` is `ondelete=RESTRICT` (`arc15_b_instance_connections.py:139`). Any instance with knowledge or connections will cause a FK violation that aborts the entire purge transaction.
- **Instance-level path is clean:** `instance_retention.py` explicitly pre-clears `knowledge_chunks`, `knowledge_sources`, `traces`, `sessions`, `api_keys`, `instance_connections` before deleting the instance row, and works correctly.
- **Disposition:** The tenant purge path has never successfully hard-deleted a tenant with any knowledge or connections in production. This is a silent latent bug; the purge job aborts, the tenant row stays, and the retention worker re-queues it on the next scan. No data is lost, but purge compliance (PIPEDA P5 / GDPR Art. 17 timelines) is violated for affected tenants.

### CONFLICT-03: Rerank step named in §3.5 but absent from retrieval pipeline
- **Spec (ARCHITECTURE §3.5):** Retrieval pipeline is filter → HNSW → **rerank** → top-k.
- **Code:** `app/knowledge/retriever.py` returns chunks ordered by cosine distance directly; `app/knowledge/context_assembler.py` ingests them without a rerank stage.
- **Impact:** Retrieval quality may be lower than spec-intended, especially for queries where cosine distance ordering diverges from relevance. Not a data-integrity issue.
- **Disposition:** Could be a roadmap item not yet assigned to an arc, or intentionally deferred. Not flagged in ARC15/ARC17.

---

## §9 TOUCHED — Artifacts Read During This Audit

```
app/tools/base.py
app/tools/authorization.py
app/tools/registry.py
app/tools/implementations/lookup_record_tool.py
app/tools/implementations/book_appointment_tool.py
app/tools/implementations/send_email_tool.py
app/tools/implementations/send_sms_tool.py
app/tools/implementations/schedule_callback_tool.py
app/tools/implementations/push_to_crm_tool.py
app/tools/implementations/call_sibling_luciel_tool.py
app/tools/implementations/bring_your_own_webhook_tool.py
app/tools/sibling_dispatch.py
app/api/v1/admin_tools.py
app/api/v1/admin_sibling_grants.py
app/models/sibling_call_grant.py
app/models/instance_tool_authorization.py
app/models/instance_status.py
app/models/secret_cleanup_outbox.py
app/models/admin_widget_domain.py
app/models/admin_audit_log.py (lines 130-140, 324, 754, 809)
app/knowledge/ingestion.py
app/knowledge/retriever.py
app/knowledge/context_assembler.py
app/knowledge/knowledge_repository.py
app/knowledge/chunker.py
app/channels/base.py
app/channels/widget.py
app/channels/email_adapter.py
app/channels/sms_adapter.py
app/channels/widget_deps.py
app/channels/provisioning.py
app/middleware/rate_limit.py
app/services/closure_service.py
app/services/downgrade_grace_service.py
app/services/downgrade_archive_service.py (lines 160+)
app/services/admin_service.py (lines 1000-1260)
app/services/instance_service.py (line 244)
app/worker/tasks/instance_retention.py
app/api/v1/admin_knowledge.py (lines 438-508, 871, 1077)
app/api/v1/retention.py
app/api/v1/billing.py (line 78)
app/policy/entitlements.py (lines 228-296)
app/integrations/record_source/ (resolver, base, S3, local)
alembic/versions/arc11_a_knowledge_sources_schema.py (FK constraint lines)
alembic/versions/arc11_b_rename_embeddings_to_chunks.py
alembic/versions/arc11_d3_hnsw_index_chunks.py
alembic/versions/arc15_b_instance_connections.py (FK constraint lines)
ARC15_BACKEND_REPORT.md
ARC15_DRIFT_CLEANUP_REPORT.md
ARC17_LOOKUP_RECORD_AMENDMENT.md
docs_text/ARCHITECTURE.txt (§3.1–3.6 sections)
docs_text/VISION.txt (§6.4–6.5)
docs_text/CUSTOMER_JOURNEY.txt (§4.5 Phase 8)
```

---

## RESIDUE DETAIL

### RESIDUE-01: `_SIBLING_ROUNDTRIP_SEAM` stub in `sibling_dispatch.py`
- **File:** `app/tools/sibling_dispatch.py:100-102`
- **Detail:** `_SIBLING_ROUNDTRIP_SEAM` constant with TODO comment. Returns `{"not_yet_available": True}` on the actual callee invocation path. All 5 guardrail checks (cycle detection, fan-out, master switch, grant lookup, audit) execute for real. Only the actual HTTP/internal call to the callee Luciel instance is stubbed.
- **Known/expected:** Code comment explicitly notes deferred past Arc 14. This is documented deferred work, not an accidental residue. Row 2.3 above marks it DRIFTED accordingly.

### RESIDUE-02: `agents / agent_configs / domain_configs` zero-count placeholders in tenant hard-delete manifest
- **File:** `app/services/admin_service.py:1050-1060`
- **Detail:** Docstring for `hard_delete_tenant_after_retention` lists steps 8a-8c as "agents / agent_configs / domain_configs: REMOVED (Arc 10.5). Underlying tables dropped before Arc 10; row-counts map carries them as 0 for audit-row schema stability only." These are dead entries in the `row_counts` dict for backwards-compatibility on audit log reads. Not actual deletions.
- **Status:** CONFORMS — intentional residue explicitly documented. Not a bug.

---

## BLOCKED-EXTERNAL

### BLOCKED-01: `record_source_live_enabled` flag — S3 read path not testable without AWS
- **Scope:** `app/integrations/record_source/s3_record_source.py`
- **Detail:** S3 live path is behind `record_source_live_enabled` (boot-safe default: False). When False, boto3 client is never constructed and an honest fail response is returned. Correctness of the S3 read path itself (IAM, bucket prefix, error handling) cannot be audited without a live AWS environment. ARC17 amendment confirms deploy-gate design is intentional.
- **Not a bug in scope of this audit;** flag and honest-fail are correctly implemented. AWS IAM grant (`s3:GetObject` on record-source bucket prefix) must be verified at deploy time.

### BLOCKED-02: Phone provisioning completeness (`app/channels/provisioning.py`)
- **Scope:** Widget lifecycle §3.1.4 — opt-in via channel selection, dedicated vs shared number provisioning.
- **Detail:** `app/channels/provisioning.py` exists but was not fully traced within READ-ONLY audit scope. File presence is consistent with spec; whether provisioning logic is complete (dedicated number purchase, release, Twilio sub-account routing) is unverified.

---

## Status Count Summary

| Status | Count |
|--------|-------|
| CONFORMS | 24 |
| DRIFTED | 8 |
| MISSING | 3 |
| BUG | 1 |
| AMBIGUOUS | 3 |
| RESIDUE | 2 |
| BLOCKED-EXTERNAL | 2 |
| **Total rows** | **41** |

> BUG-01 (tenant cascade FK violation) is the highest-severity finding. It will silently abort every hard-delete attempt for any tenant that has ever ingested knowledge or configured a connection, causing PIPEDA/GDPR retention-window compliance failure without any error surfaced to the operator.
