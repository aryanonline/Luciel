# Manifest Section 01 — Security, Isolation, Auth/RBAC & Connections Layer

**Auditor:** Subagent 01  
**Slice:** Security, isolation, auth/RBAC, and the Connections layer  
**Source-of-truth docs read:**
- `ARCHITECTURE.txt` §1.3, §3.7, §3.7.1, §3.7.2, §3.7.2b, §3.7.3, §3.8 (full), §5.11, §9
- `VISION.txt` §5 (isolation walls)
- ARC15_BACKEND_REPORT.md, ARC15_DRIFT_CLEANUP_REPORT.md, ARC17_LOOKUP_RECORD_AMENDMENT.md

**Code examined:**
- `app/middleware/auth.py`
- `app/middleware/session_cookie_auth.py`
- `app/api/deps.py`
- `app/api/widget_deps.py`
- `app/db/tenant_context.py`
- `app/db/tenant_scope.py`
- `app/db/session.py` (grep for set_config / after_begin)
- `app/policy/permissions.py`
- `app/policy/scope.py`
- `app/models/permission_model.py`
- `app/models/scope_assignment.py`
- `app/models/instance_connection.py`
- `app/models/secret_cleanup_outbox.py`
- `app/api/v1/admin_custom_roles.py`
- `app/api/v1/admin_connections.py`
- `app/integrations/secrets/aws_store.py`
- `app/integrations/oauth/`
- `app/tools/byo/sandbox.py`, `circuit_breaker.py`, `subprocess_worker.py`
- `app/worker/tasks/refresh_connections.py`
- `app/services/connection_health_service.py`
- `app/core/config.py` (rls flag, break-glass reference)
- Alembic migrations: `arc9_c3_*`, `arc9_c5_*`, `arc11_d1`, `arc11_d2`, `arc14_u2`, `arc14_u4`,
  `arc15_b_instance_connections`, `arc17_a`, `arc17_b`, `arc12b_custom_roles_permission_model`,
  `arc12_wu4_sibling_call_grants`

---

## Audit Table

| # | Requirement | Doc cite | Implementing artifact(s) | Status | Notes / evidence |
|---|-------------|----------|--------------------------|--------|-----------------|
| 1 | Two-plane split — Control Plane authenticates as Admin API key (`admin` in permissions); Data Plane as embed key (`chat` in permissions, `key_kind='embed'`) | Arch §1.3, §3.7.2 | `app/middleware/auth.py:131,163,246`, `app/api/widget_deps.py:126-151` | CONFORMS | `ADMIN_AUTH_PATHS` guard rejects any key without `"admin"` in permissions before handlers run; `require_embed_key()` rejects any non-`embed` key_kind at the widget endpoint. The two auth paths are fully separate. |
| 2 | Neither plane can produce the other tenant's `admin_id` | Arch §1.3, §3.7.2 | `app/middleware/auth.py:194`, `app/services/api_key_service.py` | CONFORMS | `admin_id` is stamped exclusively from `apikey.admin_id` (DB column, fixed at key issuance). No path allows a caller to supply or override `admin_id` via a query param; the ORM never reads `admin_id` from request body. |
| 3 | Cookie-auth path (Step 31.2): same two-plane discipline | Arch §3.7.2 | `app/middleware/session_cookie_auth.py:59-63`, `app/middleware/auth.py:151-152` | CONFORMS | Cookie auth is restricted to `/api/v1/admin/*` and `/api/v1/dashboard/*` only; widget/data-plane paths are explicitly excluded. Short-circuit fires only when `auth_method == "cookie"` already set. |
| 4 | **L1** App-layer WHERE admin_id filter in every repository query | Arch §3.7.2b | Across all repository files in `app/repositories/` | DRIFTED | ARC9 C1 audit found 18/19 tables correct; the pattern is largely enforced. However, `get_tenant_scoped_db` is a **parallel** dependency, not the sole path — legacy `DbSession` routes still exist and are not all migrated. The migration is staged (intentional design) but some routes still use the unscoped `DbSession`. This is a doc-acknowledged staged rollout, not a surprise gap. Low finding confidence: DRIFTED rather than BUG because the L2/L3 backstop is active. |
| 5 | **L2** PostgreSQL RLS on every tenant table listed in §3.7.2b: `knowledge_sources` | Arch §3.7.2b | `alembic/versions/arc11_d1_rls_knowledge_sources.py` | CONFORMS | ENABLE RLS + FORCE RLS + permissive policy confirmed. |
| 6 | L2 RLS: `knowledge_chunks` | Arch §3.7.2b | `alembic/versions/arc11_d2_rls_chunks_postrename_verify.py` | CONFORMS | RLS state preserved across rename confirmed. |
| 7 | L2 RLS: `instance_connections` | Arch §3.7.2b | `alembic/versions/arc15_b_instance_connections.py:236-248` | CONFORMS | ENABLE + FORCE + PERMISSIVE policy with USING + WITH CHECK both on `current_setting('app.admin_id', true)`. |
| 8 | L2 RLS: `sibling_call_grants` | Arch §3.7.2b | `alembic/versions/arc12_wu4_sibling_call_grants.py` | CONFORMS | ENABLE + FORCE + policy confirmed per ARC15 report. |
| 9 | L2 RLS: `admin_audit_log` | Arch §3.7.2b | `alembic/versions/arc9_c3_1_rls_admin_audit_logs.py` | CONFORMS | First per-table RLS rollout; confirmed. |
| 10 | L2 RLS: `escalation_events` | Arch §3.7.2b | `alembic/versions/arc14_u2_escalation_events.py:223` | CONFORMS | `escalation_events_tenant_isolation` policy present. |
| 11 | L2 RLS: `leads` | Arch §3.7.2b | `alembic/versions/arc14_u4_leads.py:201` | CONFORMS | `leads_tenant_isolation` policy present. |
| 12 | L2 RLS: `sessions` | Arch §3.7.2b | `alembic/versions/arc9_c3_2e_rls_sessions.py` | CONFORMS | ENABLE + policy confirmed. |
| 13 | L2 RLS: `transcripts` (doc label) | Arch §3.7.2b | `alembic/versions/arc9_c3_2c_rls_conversations.py` + `arc9_c5_1_rls_messages.py` | DRIFTED | Architecture §3.7.2b names the table `transcripts` but no table named `transcripts` exists. The actual tables are `conversations` (RLS ✓ via `arc9_c3_2c`) and `messages` (RLS ✓ via `arc9_c5_1`). Architecture uses a conceptual name not the actual DDL name. Both physical tables are covered; the doc name is imprecise but the intent is satisfied. |
| 14 | L2 RLS: `session_summaries` (doc label) | Arch §3.7.2b | No standalone migration found | DRIFTED | Architecture §3.7.2b names `session_summaries` as a tenant table requiring RLS. No `session_summaries` table exists anywhere in migrations or ORM models. Session summaries appear to be stored in `leads.summary` (a column, not a table) or derived at runtime by cognition service. If the architecture names a table that was never created, either (a) the doc is aspirational/ahead of implementation, or (b) summaries were intentionally stored inline. No independent table means no standalone RLS. **This is a doc-vs-reality gap; cannot verify intent without founder clarification.** |
| 15 | L2 RLS: `instance` (table) | Arch §3.7.2b | `alembic/versions/arc9_c3_5d_rls_instances.py` | CONFORMS | Confirmed. |
| 16 | **L3** `app.admin_id` GUC set once per request via `set_config`, never overwritten | Arch §3.7.2b | `app/db/tenant_context.py`, `app/db/session.py:133,165,179` | CONFORMS | `after_begin` event listener issues `SELECT set_config('app.admin_id', %s, true)` (SET LOCAL equivalent) on every transaction BEGIN. Uses `asyncio.ContextVar` for per-coroutine isolation. Reset independently in `finally` block in `get_tenant_scoped_db`. `rls_tenant_context_enabled` defaults `True` (since Arc 15). |
| 17 | L3 GUC never overwritten mid-request | Arch §3.7.2b | `app/db/tenant_context.py` (module design) | CONFORMS | ContextVar is set once at request entry; no ORM or handler path reads `admin_id` from request body to overwrite it. The `reset_current_admin_id` helper exists for cross-tenant background jobs but is not callable from HTTP handler scope. |
| 18 | Four locked roles immutable/platform-defined: `admin_owner`, `admin_manager`, `instance_operator`, `read_only_viewer` | Arch §3.7.1 | `app/policy/permissions.py:LOCKED_ROLE_PERMISSIONS_FALLBACK`, `alembic/versions/arc12b_custom_roles_permission_model.py:97-105` | CONFORMS | Four roles seeded in `role_permissions` table; locked roles cannot be modified by tenants. DB seed and Python fallback constant kept in sync by test. |
| 19 | Atomic permission set — 14 permissions per §3.7.3 | Arch §3.7.3 | `app/policy/permissions.py:ALL_PERMISSIONS` | DRIFTED | Architecture §3.7.3 specifies 14 atomic permissions: `can_configure_instance`, `can_edit_knowledge`, `can_delete_knowledge`, `can_configure_connections`, `can_author_sibling_grants`, `can_view_leads`, `can_view_conversations`, `can_view_audit_log`, `can_manage_team`, `can_manage_lifecycle`, `can_erase_lead_data`, `can_manage_billing`, `can_export_data`, `can_manage_org_policy`. Code (`app/policy/permissions.py`) implements 14 permissions but with different names/scope: adds `can_view_knowledge`, `can_ingest_knowledge`, `can_view_tools`, `can_configure_tools`, `can_configure_channels`, `can_approve_sibling_grants`, `can_author_custom_roles`, `can_assign_roles`. Missing from code: `can_configure_instance` (replaced by per-resource `can_configure_channels`+`can_configure_tools`?), `can_view_leads`, `can_view_conversations`, `can_manage_team`, `can_manage_lifecycle`, `can_erase_lead_data`, `can_export_data`, `can_manage_org_policy`. Code has evolved the permission vocabulary beyond what §3.7.3 documents. |
| 20 | Approval workflow: creating/modifying custom role with `can_configure_connections` or `can_manage_billing` requires second `admin_owner` approval | Arch §3.7.3 | `app/api/v1/admin_custom_roles.py` (full file reviewed) | MISSING | No pending-approval state, no second-approver endpoint, no `approval_state` column on `custom_roles` table in `arc12b_custom_roles_permission_model.py`. The `author_custom_role` and `update_custom_role` endpoints are immediate — no async approval step. By contrast, `sibling_call_grants` correctly implements `pending_approval → live` with a second-owner approve endpoint (`admin_sibling_grants.py:463`). The §3.7.3 approval workflow for sensitive custom roles is unimplemented. |
| 21 | `instance_connections` schema: columns `connection_id`, `admin_id`, `instance_id`, `connection_type` enum, `provider`, `status` enum, `secret_ref`, `non_secret_config`, `created_by_user_id`, `created_at`, `last_health_check_at`, `status_detail` | Arch §3.8.2 | `app/models/instance_connection.py`, `alembic/versions/arc15_b_instance_connections.py` | DRIFTED | Most schema elements present. Column name drifts: `secret_ref` → `credential_ref` (code name); `non_secret_config` → `config_json` (code name). Missing columns: `created_by_user_id` (FK to team member who configured) and `status_detail` (human-readable error/expired description) are absent from both migration and ORM model. `last_health_check_at` present (renamed from `last_verified_at` by arc17_a). |
| 22 | `status` enum values: `unconfigured \| connected \| error \| expired \| revoked \| dormant` | Arch §3.8.2 | `alembic/versions/arc15_b_instance_connections.py:99-102`, `app/models/instance_connection.py:50-53` | DRIFTED | Migration and ORM enum only contain 4 values: `unconfigured \| connected \| error \| expired`. The values `revoked` and `dormant` from §3.8.2 are absent from the PG enum. Revocation is handled via `revoked_at IS NULL` partial index (soft-delete column) rather than a status value. Architecture §3.8.4 also shows `revoked` and `dormant` as status values in its lifecycle diagram. |
| 23 | Single-active-per-type unique constraint: `(admin_id, instance_id, connection_type)` where status ≠ 'revoked' | Arch §3.8.2 | `alembic/versions/arc15_b_instance_connections.py:218-231` | DRIFTED | Two indexes exist: `uq_instance_connections_active` is on `(admin_id, instance_id, connection_type, **provider**)` (not just connection_type); a covering index `ix_instance_connections_lookup` on `(admin_id, instance_id, connection_type)` is non-unique. Arch §3.8.2 specifies the unique constraint is on `(admin_id, instance_id, connection_type)` for single-active-per-type enforcement, but code enforces uniqueness at the 4-tuple level including `provider`. This allows multiple providers of the same connection_type simultaneously, which may be intended (e.g., both Google Calendar and Calendly) but differs from the exact constraint documented. |
| 24 | Credential storage contract: secrets NEVER in Postgres; `secret_ref` = ARN only | Arch §3.8.3 | `app/integrations/secrets/aws_store.py`, `app/api/v1/admin_connections.py:111-130` | CONFORMS | `AwsSecretsManagerStore.put()` returns ARN; only ARN persisted in `credential_ref`. Secret-looking keys in `config_json` rejected with `422 secret_in_config_json`. `SecretCleanupOutbox` stores only the pointer. DEPLOY-GATED behind `connections_live_secrets_enabled`. |
| 25 | Secret ARN path convention: `vm/{admin_id}/{instance_id}/*` | Arch §3.8.3 | `app/integrations/secrets/aws_store.py:23`, `app/api/v1/admin_connections.py:193-202` | DRIFTED | Architecture §3.8.3 specifies `vm/{admin_id}/{instance_id}/{connection_type}` convention. Actual code uses `luciel/connections/{admin_id}/{instance_id}/{connection_type}` (prefix is `luciel/connections/` not `vm/`). The tenant-scoping structure is identical; only the platform prefix differs. The IAM least-privilege policy in Architecture §3.8.3 references `vm/{admin_id}/{instance_id}/*` — if the live IAM policy uses that prefix, the code's `luciel/connections/` prefix would not match. **BLOCKED-EXTERNAL** to verify live IAM policy. |
| 26 | Secrets never logged / exported | Arch §3.8.3 | `app/api/v1/admin_connections.py:380` (audit records only config_json keys), `app/models/secret_cleanup_outbox.py` | CONFORMS | Audit rows record only `config_json` key names (not values) and never `credential_ref` value. Outbox stores pointer only. |
| 27 | Connection status lifecycle: `unconfigured → connected → error → expired` + status-change audit events | Arch §3.8.4 | `app/services/connection_health_service.py`, `app/worker/tasks/refresh_connections.py` | CONFORMS (partial) | Lifecycle transitions implemented. Status-change writes `ACTION_CONNECTION_TOKEN_REFRESHED` to `admin_audit_log`. `revoked` and `dormant` states not present in enum (see row 22). |
| 28 | Health-check + token-refresh worker §3.8.5 | Arch §3.8.5 | `app/worker/tasks/refresh_connections.py`, `app/services/connection_health_service.py` | CONFORMS (partial) | Worker exists and runs per-connection status checks + OAuth token refresh. Secret fetched from store, new token written back, `last_health_check_at` updated. OAuth deferred connectors are skipped honestly (DEPLOY-GATED). Exact cadence (OAuth: 15min, API-key: 60min, SMS: 4hr per §3.8.5) is implemented as Celery task schedule — **BLOCKED-EXTERNAL** to verify the live Celery beat schedule matches spec cadences. |
| 29 | BYO subprocess sandbox — subprocess isolation (§3.8.6) | Arch §3.8.6 | `app/tools/byo/sandbox.py`, `app/tools/byo/subprocess_worker.py` | CONFORMS | Subprocess spawned via `asyncio.create_subprocess_exec`; main-process memory/DB pool not accessible by subprocess. Auth header added by parent before subprocess call; subprocess never sees raw token. |
| 30 | BYO sandbox — enforced timeout: 10 seconds (§3.8.6) | Arch §3.8.6 | `app/tools/byo/sandbox.py:89` | DRIFTED | Architecture §3.8.6 specifies **10 seconds** timeout. Code uses `BYO_HARD_TIMEOUT_SECONDS = 30` (30 seconds). Child HTTP timeout is 25 seconds (`_CHILD_REQUEST_TIMEOUT_SECONDS`). This is a 3× difference from the documented spec. |
| 31 | BYO sandbox — request/response schema validation (§3.8.6) | Arch §3.8.6 | `app/tools/byo/sandbox.py:395-415` | CONFORMS | Input validated against registered `input_schema` before dispatch; output validated against `output_schema` after subprocess returns. Schema failures are terminal (no retry). |
| 32 | BYO sandbox — per-endpoint circuit breaker: 5 consecutive failures (§3.8.6) | Arch §3.8.6 | `app/tools/byo/circuit_breaker.py:88 (FAILURE_THRESHOLD = 5)` | CONFORMS | `FAILURE_THRESHOLD = 5`, `failure_window_seconds = 60`, `open_duration_seconds = 60`. Circuit state changes logged to `admin_audit_log`. |
| 33 | BYO sandbox — SSRF egress: allowlist + RFC1918/link-local/metadata IP blocks + DNS-rebind protection (§3.8.6) | Arch §3.8.6 | `app/tools/byo/sandbox.py:155-165`, `app/tools/byo/subprocess_worker.py:67-80` | DRIFTED | Egress allowlist (FQDN-based) is implemented in both parent and child (defence in depth). **However, RFC1918 private IP range blocking, link-local (169.254.x.x), loopback (127.x, ::1), and AWS instance metadata IP (169.254.169.254) blocking are NOT implemented anywhere in sandbox.py or subprocess_worker.py.** DNS rebind protection (resolve hostname and check resulting IP against blocked ranges) is also absent. The FQDN allowlist alone is insufficient — a DNS rebind attack pointing an allowed FQDN to 169.254.169.254 would not be caught. This is a material SSRF protection gap relative to §3.8.6. |
| 34 | §5.11 Internal-access / break-glass posture — code artifacts | Arch §5.11 | `app/core/config.py:572` (comment only) | AMBIGUOUS (policy-only) | No break-glass code artifacts exist: no `internal_access_log` table, no break-glass request/approval API, no time-boxed access provisioner. The only reference is a config.py comment explaining `rls_tenant_context_enabled=False` as a "forensic break-glass session against a non-RLS replica" — which is an operator-level toggle, not a code-implemented break-glass workflow. Architecture §5.11 is detailed operational policy; the code provides the *infrastructure* (RLS, encryption, KMS key access controls) that makes break-glass meaningful, but the procedure itself (justification system, second-person approval, time-boxing, internal access log, Enterprise notification) has no code implementation. **This is as expected for an operational policy — marking AMBIGUOUS as the spec is likely intentionally policy-only.** |

---

## CONFLICTS

### CONFLICT-01 — Architecture §3.7.2b table name "transcripts" vs actual DDL
**Type:** Doc-vs-reality  
**Doc cite:** Architecture §3.7.2b, line ~1187-1188: "…sessions, transcripts, session_summaries, and the instance table itself."  
**Reality:** No table named `transcripts` exists. The actual tables covering conversation content are `conversations` (RLS via `arc9_c3_2c`) and `messages` (RLS via `arc9_c5_1`). The architecture uses a logical/UX label for a table that has a different DDL name.  
**Impact:** RLS intent is satisfied; doc precision is low. Recommend founder update §3.7.2b to name `conversations` and `messages`.

### CONFLICT-02 — Architecture §3.7.2b table "session_summaries" does not exist
**Type:** Doc-vs-reality  
**Doc cite:** Architecture §3.7.2b, line ~1188: "…session_summaries, and the instance table itself."  
**Reality:** No `session_summaries` table exists in any migration. Session summaries are stored as a column (`leads.summary`) or generated at runtime. There is no standalone table and therefore no standalone RLS policy.  
**Impact:** If session summaries are stored only in `leads` (which has RLS), the data is protected. But if the architecture envisioned a separate `session_summaries` table that was never built, this is a data-model gap. Requires founder clarification.

### CONFLICT-03 — Secret path convention: `vm/` vs `luciel/connections/`
**Type:** Doc-vs-code  
**Doc cite:** Architecture §3.8.3: "vm/{admin_id}/{instance_id}/* convention"  
**Code:** `app/integrations/secrets/aws_store.py:23` uses prefix `luciel/connections/`; `admin_connections.py:193-202` builds `{admin_id}/{instance_id}/{connection_type}` as the logical name, yielding full path `luciel/connections/{admin_id}/{instance_id}/{connection_type}`.  
**Impact:** If the production IAM policy grants `secretsmanager:GetSecretValue` on `vm/{admin_id}/{instance_id}/*`, the code's `luciel/connections/*` paths would not match — resulting in runtime failures or overly broad IAM grant. This is a material conflict if the live AWS environment follows the architecture spec's `vm/` prefix.

### CONFLICT-04 — BYO timeout: Architecture §3.8.6 10s vs code 30s
**Type:** Doc-vs-code  
**Doc cite:** Architecture §3.8.6: "Enforced timeout: 10 seconds."  
**Code:** `app/tools/byo/sandbox.py:89`: `BYO_HARD_TIMEOUT_SECONDS = 30`  
**Impact:** Conversations could hang for up to 30 seconds waiting for a misbehaving BYO webhook, vs. the 10-second SLO implied by §3.8.6. The longer timeout also means the "agentic loop reasons about this as a tool unavailability" path fires later than spec'd.

### CONFLICT-05 — unique constraint on `(admin_id, instance_id, connection_type)` vs `(admin_id, instance_id, connection_type, provider)`
**Type:** Doc-vs-code  
**Doc cite:** Architecture §3.8.2: "Unique constraint on (admin_id, instance_id, connection_type) over rows where status ≠ 'revoked' — enforces single-active-per-type per instance"  
**Code:** `uq_instance_connections_active` is on `(admin_id, instance_id, connection_type, provider)` — including the provider. This permits two simultaneous active connections of the same type (e.g., `calendar/google_calendar` and `calendar/calendly`).  
**Impact:** May be intentional to support multi-provider scenarios but contradicts the documented "single-active-per-type" constraint. Requires founder decision.

### CONFLICT-06 — Permission vocabulary drift between §3.7.3 and code
**Type:** Doc-vs-code  
**Doc cite:** Architecture §3.7.3 (14 named permissions including `can_configure_instance`, `can_view_leads`, `can_view_conversations`, `can_manage_team`, `can_manage_lifecycle`, `can_erase_lead_data`, `can_export_data`, `can_manage_org_policy`)  
**Code:** `app/policy/permissions.py` uses a different vocabulary (14 permissions but different names/granularity): includes `can_view_knowledge`, `can_ingest_knowledge`, `can_view_tools`, `can_configure_tools`, `can_configure_channels`, `can_approve_sibling_grants`, `can_author_custom_roles`, `can_assign_roles`. Missing from code: 8 of the 14 permissions named in §3.7.3.  
**Impact:** Architecture §3.7.3 describes the product-facing permission names (what the admin UI shows); the code has evolved a more granular internal catalog. The route enforcement uses the code-level permissions, not the §3.7.3 labels. Doc and code are out of sync. Recommend updating §3.7.3 to reflect the actual arc12b permission catalog or aligning the catalog to the doc.

---

## §9 TOUCHED — Authored Commitments Relevant to This Slice

| §9 # | Authored Value | Found Value | Match? |
|------|---------------|-------------|--------|
| 17 | Tenant isolation: three-layer (app-layer filter + PostgreSQL RLS + per-request admin_id setting) | All three layers implemented: `app/repositories/` (L1), `alembic/versions/arc9_c3_*` etc (L2), `app/db/tenant_context.py` + session.py after_begin (L3). `rls_tenant_context_enabled=True` by default. | YES |
| 18 | Connection credentials: AWS Secrets Manager; pointer in Postgres; never plaintext | `aws_store.py` uses Secrets Manager; `credential_ref` stores ARN only; `secret_in_config_json` guard rejects plaintext. DEPLOY-GATED. | YES |
| 30 | Internal access: no standing staff access; break-glass only (time-boxed, second-person approved, fully logged) | No code implementation of break-glass workflow. Operational posture only. | POLICY-ONLY (as expected) |
| 34 | Enterprise break-glass notification: Enterprise admin notified of break-glass events on request | No code implementation. CSM-delivered per §5.11 narrative. | POLICY-ONLY (as expected) |

---

## RESIDUE DETAIL

| Residue | Location | Doc justification? | Dependency impact |
|---------|----------|--------------------|-------------------|
| `EMBED_WIDGET_RATE_LIMIT = "30/minute"` static constant | `app/api/widget_deps.py:215` | Explicitly marked backward-compat for old importers; replaced by dynamic per-key rate limiting (Arc 8). | Low — comment says "new callers should not reference it"; if any test/script still imports it, removing it would break that caller. Safe to remove once the backward-compat comment is resolved. |
| `get_agent_repository` stub (raises RuntimeError) | `app/api/deps.py:243-254` | Arc 5 Path A migration artifact; explicitly docs'd as "removed at B2". | Low — if any route still declares this as a dependency, FastAPI startup succeeds but the call path throws at runtime. Already flagged by the code comment as a B2 sweep target. |

---

## BLOCKED-EXTERNAL

| Item | What is needed | Why blocked |
|------|---------------|-------------|
| **IAM policy path prefix** | Verify the live AWS IAM policy grants match `luciel/connections/{admin_id}/{instance_id}/*` (code) vs `vm/{admin_id}/{instance_id}/*` (Architecture §3.8.3). | Requires access to the production AWS account IAM policies (CloudFormation stacks or `aws iam get-policy`). Founder must verify. If prod uses `vm/` prefix per the doc, connections would fail to store/retrieve secrets in production. |
| **Celery beat schedule for health-check worker** | Verify the deployed Celery beat schedule matches §3.8.5 cadences (OAuth: 15min, API-key: 60min, SMS: 4hr). | Requires access to production Celery beat configuration or ECS task definition (worker task). Not visible in the repo's Python code alone. |
| **KMS key setup** | Verify one KMS key per environment exists and is correctly scoped per §3.8.3. Enterprise per-tenant KMS option. | Requires live AWS account access. |
| **Production `connection_live_secrets_enabled` flag** | Verify the flag is `True` in production ECS task env vars (enabling live Secrets Manager calls). | Requires ECS task definition or SSM parameter access. |
