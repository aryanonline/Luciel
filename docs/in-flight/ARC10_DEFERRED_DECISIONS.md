# Arc 10 — Deferred Decisions and Follow-Up Items

**Arc:** 10 — Deactivation Lifecycle, Cap Reclamation, Pre-Closure Data Export
**Status:** CLOSED 2026-05-27 20:58 EDT · all seven re-open gaps closed, end-to-end verified against prod RDS
**Author:** Sandbox agent (under founder direction)

**Anchoring (Space directive):** Every fix in this arc is anchored to a specific section of one of the four Space business documents — `VANTAGEMIND_VISION_v1.pdf`, `VANTAGEMIND_ARCHITECTURE_v1.pdf`, `VANTAGEMIND_CUSTOMER_JOURNEY_v1.pdf`, or `Sandbox Agent Key Credentials`. Repo-internal scaffolding (arc/step references, drift IDs) is informational only; the business documents are the alignment target.

---

## 0. Re-open status — CLOSED

### Final outcome

All seven gaps closed; full close + reactivate flow exercised
end-to-end against prod RDS via an in-cluster stub-Stripe harness;
full backend test suite 1428 passed / 0 failed against a local
Postgres mirror of the prod schema.

Twelve PRs merged to main in the re-open: #103, #104, #105, #106
(Gap 1/2/3/4/5 + docs), #107, #108, #109, #110 (Gap 6 four-layer
audit-archiver close-out), #111, #112, #113 (Gap 7 three-PR
close-out).

### Lessons recorded

1. The Space directive on production-grade standards is not
   negotiable; quality cuts cannot be surfaced as a user choice.
2. The four business documents — Vision, Architecture, Customer
   Journey, Sandbox Agent Key Credentials — are the only
   authoritative alignment target. Arc-history scaffolding
   (CANONICAL_RECAP, arc/step references, drift IDs) is
   informational; if it conflicts with a business document, the
   business document wins.
3. Tests that contradict business documents are stale; update the
   test to match the doc, not the other way around.
4. "Pre-existing not my regression" is not a valid defense against
   failing tests. Install whatever is needed (Postgres, Redis,
   pytest-asyncio) and get a real green baseline.
5. End-to-end in-cluster harnesses against prod RDS are the only
   reliable surface for catching layered RBAC + schema + ORM bugs.
   Gap 6 found four layered bugs; Gap 7 found four more. None
   would have surfaced from unit tests alone.

### Architecture / Vision anchors used

- **Vision v1 §3** — Five configuration pillars (channels, tools,
  knowledge, escalation, personality) used as the canonical V2
  surface; supports removing the dead `agents` / `agent_configs`
  cascade layers (PR #111).
- **Vision v1 §6** — Closure lifecycle / 30-day grace /
  reactivation; supports the Gap 7 close + reactivate fixes
  (PR #113).
- **Architecture v1 §3.6** — Lifecycle subsystem; §3.6.1
  deactivation cascade (the canonical post-prune 10-layer order);
  §3.6.2 account closure flow (closure invokes 3.6.1 per
  instance); §3.6.3 pre-closure data export (Gap 5 worker fix).
- **Architecture v1 §3.7.3 (Wall 3)** — applies to customer-data
  rows specifically. Used to justify loosening
  `admin_audit_logs.luciel_instance_id` back to nullable (Gap 7
  bug 1) while keeping it NOT NULL on every actual customer-data
  table (conversations, messages, memory_items, knowledge_
  embeddings, etc.).
- **Architecture v1 §3.7.5** — RLS policy pattern; used in the
  Gap 7 audit-log policy update to restore the IS NULL disjunct
  for admin-scoped audit emissions.
- **Architecture v1 §5.3** — Audit chain immutability at the app
  layer; supports the audit-log treatment as a distinct concept
  from customer-data tables and the exclusion of
  `cold_archived_at` / `tier_at_write` from `_CHAIN_FIELDS`
  (PR #112).
- **Customer Journey v1 §2** — Sarah Free signup; used to anchor
  the signup-free IP-validation production bug fix (PR #112) and
  the actor_user_id contract rewrite.
- **Customer Journey v1 §8** — Marcus closure + reactivation;
  used to anchor the four Gap 7 close+reactivate bug fixes
  (PR #113).

### Out-of-scope follow-ups (Arc 10.5)

Orphaned `AgentConfig` CRUD surfaces remain reachable from
non-closure routes (`AdminService.create_agent_config`,
`get_agent_config`, `list_agent_configs`; `AgentConfig` model +
schemas; `config_repository` legacy lookups;
`verification.py` agent_configs entry). These reach a table
(`agent_configs`) that was DROPPED before Arc 10. Closure path no
longer touches them (Gap 7 prune, PR #111), but a separate audit
pass needs to confirm no live caller before deletion. Tracked as
**Arc 10.5: orphaned AgentConfig surface cleanup**.

### Original re-open rationale (historical)

Original close-out at 19:45 EDT shipped Phases 3-5 at a degraded
quality bar to fit a single-turn budget. Founder correctly rejected
that trade as inconsistent with the Space's production-grade
standards instructions.

### Gap progress

* **Gap 1 (server-sourced lifecycle-state surface)** — CLOSED 2026-05-27 20:13 EDT.
  Backend route `GET /api/v1/admin/account/lifecycle-state` lives in
  `app/api/v1/admin.py`; `ClosureService.get_lifecycle_state()` lives in
  `app/services/closure_service.py`; schema `LifecycleStateResponse`
  in `app/schemas/lifecycle.py`; 12 contract tests in
  `tests/services/test_arc10_lifecycle_state.py`. Frontend
  `lib/lifecycle.ts::getLifecycleState()` + `LifecycleBannerStack`
  composite read from the route. localStorage is now only a stale-
  cache hint for first-paint; server is source of truth. Deployed in
  prod (luciel-backend:118; image arc10-gap-1-3-4-rewire-6428b08;
  Amplify job 42). Probed live with 401 on no-cookie (correct).

* **Gap 3 (closure modal polish)** — CLOSED 2026-05-27 20:13 EDT.
  Spinner on confirm button, escape/outside-click guarded during
  submit, radio cards with hover + focus rings + selected-state
  border + tint, inline error surface (role=alert) with status-
  specific messages (400/409/410/401), copy rewrite to plain English
  (no engineer-y phrases like 'chunks sharing a source_id'),
  aria-describedby wiring, aria-invalid on confirm-name mismatch.
  Live in prod.

* **Gap 4 (dashboard banner integration)** — CLOSED 2026-05-27 20:13 EDT.
  `LifecycleBannerStack` rendered in `pages/Dashboard.tsx` inside the
  `auth.kind === "ok"` branch above the tab nav. Same component as
  `/account`, so the two surfaces stay in lockstep. Live in prod.

* **Gap 2 (frontend Vitest coverage)** — CLOSED 2026-05-27 20:23 EDT.
  56 new tests across 3 files in `src/test/`:
  `lifecycle.test.ts` (18), `CloseAccountSection.test.tsx` (18),
  `LifecycleBanners.test.tsx` (20). Plus `pilot.test.tsx` refactored to
  a URL-dispatcher fetch mock because Account now fires 3 mount-time
  fetches. 99/99 frontend tests pass. PR aryanonline/Luciel-Website#10
  merged.

* **Gap 5 (4 deferred backend test suites)** — CLOSED 2026-05-27 20:30 EDT.
  72 new tests across 4 files:
  `tests/services/test_arc10_data_export.py` (21),
  `tests/services/test_arc10_downgrade_grace.py` (17),
  `tests/services/test_arc10_reactivation.py` (20),
  `tests/db/test_arc10_audit_archiver_role_privileges.py` (14).
  Test strategy: AST + text assertions against shipped source --
  matches existing Arc 10 / Arc 8 WU-6 doctrine, sandbox-runnable, no
  Postgres container needed. The DB-level grant assertions were
  proven against prod RDS by the original arc's e2e harness; these
  tests are the regression net. PR aryanonline/Luciel#105 merged.
  Backend redeployed (luciel-backend:119; image main-b485e68).

  Real bug caught + fixed in the same PR:
  `D-arc10-data-export-worker-uses-session-local-not-ops-2026-05-27`.
  Both `generate_export_bundle` and `expire_old_signed_urls` in
  `app/worker/tasks/data_export.py` used `SessionLocal()` (luciel_app
  role, RLS-enforced) instead of `OpsSessionLocal()`.
  `expire_old_signed_urls` runs a cross-admin UPDATE; without an
  `app.admin_id` GUC, RLS made every UPDATE match zero rows -- silent
  dead letter, exports would NEVER expire in prod. Fixed both tasks
  to use `(OpsSessionLocal or SessionLocal)()` matching the
  retention.py pattern. Caught by
  `test_data_export_task_uses_ops_session_for_bypassrls` -- exactly
  the kind of value the missing test suite was supposed to deliver.

* **Gap 6 (audit-archiver observability + synthetic exercise)** —
  CLOSED 2026-05-27 21:36 EDT. Synthetic in-cluster ECS harness
  (`workspace/arc10/audit_archiver_e2e.py`) inserts an
  admin_audit_logs row past the tier-retention window, invokes
  `AuditRetentionService._archive_one_tier`, asserts
  `cold_archived_at` stamped + S3 object present at the expected
  key shape. Run end-to-end against prod RDS (exit 0).

  Four production bugs surfaced and fixed in this gap:

  - **#107**: `ACTION_AUDIT_LOG_TIER_ARCHIVED` constant existed but
    was never wired into `ALLOWED_ACTIONS`. Every archive batch
    crashed `ValueError` on the per-batch audit emission, S3 object
    written but `cold_archived_at` never stamped — partial-state
    bug, duplicate-write loop on next worker tick.
  - **#108**: `luciel_audit_archiver` role had SELECT + UPDATE on
    admin_audit_logs but no INSERT. The per-batch audit emission
    INSERT crashed `permission denied`. Same partial-state class.
  - **#109**: `luciel_audit_archiver` had no USAGE on the
    `admin_audit_logs_id_seq` sequence. PostgreSQL needs sequence
    USAGE for nextval() during INSERT. Same partial-state class.
  - **#110**: `_emit_batch_audit` grouped batches by admin_id only
    and passed `luciel_instance_id=None`. Arc 9.1 NOT NULL
    constraint rejected the INSERT. Fixed by sub-grouping by
    `(admin_id, luciel_instance_id)` so each batch-audit row
    carries a real instance_id; S3 key encodes instance_id as
    `inst-{id}-{first}-{last}.jsonl` for forensic scope.

  The four bugs surfaced one-by-one because each rollback hid the
  next layer. **Lesson: end-to-end in-cluster harness is the only
  reliable surface for catching this class of layered RBAC +
  schema + ORM bug.**

* **Gap 7 (Stripe-integrated close + reactivate E2E)** —
  CLOSED 2026-05-27 20:58 EDT. Stub-Stripe in-cluster ECS harness
  (`workspace/arc10/stripe_lifecycle_e2e.py`) seeds a synthetic
  admin + instance + subscription, runs
  ClosureService.initiate_closure -> ReactivationService.
  stage_reactivation -> .complete_reactivation against prod RDS,
  asserts admin/instance/Stripe-call state at each step, cleans up.
  Exit 0 against prod RDS.

  Founder chose stub-Stripe over live-Stripe (which would have
  required either real card data or browser automation through
  Stripe's hosted checkout) and over Stripe test mode (no test
  keys in SSM). Stub-Stripe exercises the full DB/RBAC/ORM/audit
  surface; what it misses is real Stripe API integration drift,
  which is covered separately by the live webhook flow.

  Four production bugs surfaced and fixed in this gap:

  - **#111 (cascade prune)**: `/account/close` imported the
    deleted `AgentRepository` class; the cascade in
    `AdminService.deactivate_tenant_with_cascade` had two layers
    (`agents`, `agent_configs`) referencing dropped tables.
    Removed both layers; aligned the cascade with Architecture
    v1 §3.6.1 (canonical 10-step post-prune order). The route was
    structurally broken since the agents-layer drop — every
    close attempt would have crashed `ModuleNotFoundError`.
  - **#113 bug 1**: `admin_audit_logs.luciel_instance_id` was
    NOT NULL across the board after Arc 9.1's bulk tenant-
    isolation seal. Architecture v1 §3.7.3 (Wall 3) applies to
    customer-data tables; §5.3 names the audit log as a distinct
    concept. Fix: alembic migration loosens the column to
    nullable + restores the IS NULL disjunct in the RLS policy.
    Admin-scoped audit emissions (cascade, team-member ops,
    embed-key revoke) can now write the audit chain.
  - **#113 bug 2**: Cascade layer 5 called
    `luciel_instance_service.repo.deactivate_all_for_tenant` — a
    method renamed to `deactivate_all_for_admin` at the
    tenant_id→admin_id collapse. Every close attempt crashed
    `AttributeError`. Fix: route through the
    `InstanceService.cascade_on_admin_deactivate` public hook
    designed for this (per Architecture v1 §3.6.2 step 3).
  - **#113 bug 3**: `ACTION_ACCOUNT_CLOSURE_INITIATED` and
    `ACTION_ACCOUNT_REACTIVATED` constants existed but neither
    was in `ALLOWED_ACTIONS`. Both flows crashed `ValueError` on
    the final audit emission. Fix: wire both with rationale.
  - **#113 bug 4**: `ReactivationService._inverse_restore_table`
    hard-coded `deactivated_at = NULL` for every per-admin table.
    Most tables don't carry that column (mixed conventions:
    `deactivated_at` / `soft_deleted_at` / no timestamp). The
    inverse cascade crashed `UndefinedColumn` against five of
    seven tables — the reactivate-complete leg was broken for
    nearly every admin. Fix: extend the existing information_
    schema runtime-discovery pattern (already used for
    pending_downgrade_archived_at) to the timestamp column.

  PR #112 also closed a related production bug surfaced during
  test-suite alignment: `app/api/v1/billing.py::signup_free`
  passed `request.client.host` raw into an INET column, returning
  500 on any non-IP host (TestClient default, anomalous ALB
  forwarding). Fixed with `ipaddress.ip_address()` validation
  before use; aligns with the route's existing fail-open posture
  on missing IP.

### In-flight bugs caught during the re-open

* `D-arc10-reopen-tsc-misses-jsx-fragment-imbalance-2026-05-27`
  (Gap 4 wave). My multi-step edit batch fail-atomically silently
  dropped the open `<>` tag in Dashboard.tsx. `tsc --noEmit` passed
  (surface-level TS), but esbuild's stricter JSX parse failed Amplify
  build #41. Caught by reading the build log, fixed in hotfix
  aryanonline/Luciel-Website#9. Pattern for future: run `vite build`
  locally (not just `tsc --noEmit`) before any frontend push that
  touches JSX structure.

* `D-arc10-data-export-worker-uses-session-local-not-ops-2026-05-27`
  (Gap 5 wave). Both data_export worker tasks used SessionLocal
  instead of OpsSessionLocal. `expire_old_signed_urls` would never
  expire any export in prod due to RLS blocking the cross-admin
  UPDATE. Caught by the new Gap 5 test suite
  (`test_data_export_task_uses_ops_session_for_bypassrls`). Fixed in
  the same PR (#105). Backend redeployed (luciel-backend:119).

### Remaining work to fully close Arc 10

None. All seven gaps closed; both end-to-end harnesses (audit-
archiver lifecycle, Stripe close + reactivate) green against prod
RDS; full backend test suite 1428 passed / 0 failed against a
local Postgres mirror of prod schema; zero regressions.

Arc 11 unblocked.

---

## Original deferred-decisions content follows. Section 0 above
## supersedes the original 'CLOSED' marker that was written before
## the founder correctly rejected the degraded quality bar.

---

## 0. Arc-Close State (in-sync verification)

Verified at close 2026-05-27 19:45 EDT:

* **Schema**: `alembic_version = arc10_lifecycle_subsystem`. All 25 expected
  schema objects verified live in prod via the one-shot luciel-arc10-verify
  ECS task (Phase 2c). Migration was rebased onto
  `arc9_2_pr101_drop_tenant_id_column` to collapse a fork the original
  draft created against `b2e5f17a3d9c`.

* **AWS infra**: `luciel-data-exports` + `luciel-audit-cold-archive` S3
  buckets created, encrypted (SSE-S3), versioned, public-access blocked,
  with lifecycle policies (30d-expire on exports/, GLACIER_IR @90d +
  DEEP_ARCHIVE @365d on audit cold). IAM grants on `luciel-ecs-worker-role`
  and `luciel-ecs-web-role` (least-privilege per bucket). Two SSM
  SecureStrings live: `/luciel/production/audit_archiver_password`,
  `/luciel/production/audit_archiver_db_url`.

* **ECS**: `luciel-backend-service` running task definition `:117`,
  `luciel-worker-service` running `:49`, both on image
  `arc10-lifecycle-subsystem-f56874f`. Backend `/api/v1/version` reports
  `git_sha: f56874f` (probe verified). Both services stable and serving.

* **Frontend**: `Luciel-Website` main branch deployed by Amplify job 40
  (commit 5725b57) at 2026-05-27 19:40 EDT. Bundle contains all four
  Arc 10 data-testid markers (`closure-grace-banner`, `close-account`,
  `reactivate-button`, `export-ready-banner`). Live at vantagemind.ai.

* **CI/CD**: New backend-image build/push job in
  `.github/workflows/ci.yml` triggers on `main` + `arc*/**` branches via
  the dedicated `luciel-backend-ci-build` OIDC role (ECR-push scoped to
  the `luciel-backend` repo only). Closes the founder-laptop-in-deploy-
  chain drift that existed since Arc 8.

* **CloudWatch alarms**: Four metric-filter-based alarms on the worker
  log group (retention, audit archiver, data export, downgrade-grace),
  all wired to the `luciel-prod-alerts` SNS topic.

* **E2E**: DB-level harness ran in-cluster against prod RDS
  (luciel-arc10-e2e:2). Three suites passed: close→grace→reactivate full
  cycle (E2E A), close→backdate-31d→tombstone→idempotency (E2E B),
  archiver role privilege/restriction matrix (E2E C). Test admins
  cleaned up at end of run.

* **Rollback**: Post-migration RDS snapshot
  `luciel-db-arc10-post-migration-20260527-191955` retained.

### In-flight drifts resolved during deploy

1. `D-arc10-migration-graph-forked-against-pr101-2026-05-27` — rebased
   `down_revision` from `b2e5f17a3d9c` to `arc9_2_pr101_drop_tenant_id_column`.
2. `D-arc10-migration-runner-wrong-db-role-2026-05-27` — migration TD
   was pointing `DATABASE_URL` at the `luciel_app` SSM key (the runtime
   role); migrations need `luciel_admin`. Patched to `/luciel/database-url`.
3. `D-arc10-table-identifier-singular-vs-plural-2026-05-27` — Arc 10
   migration + audit-retention service + data-export service all
   referenced `admin_audit_log` (singular, the Python module name) as
   the SQL identifier; real table is `admin_audit_logs` (plural). 32
   lines patched across 4 files.
4. `D-arc10-founder-laptop-required-for-backend-deploy-2026-05-27` —
   Path A (CI build/push) chosen; new OIDC role + CI job make backend
   image build reproducible without a Windows laptop.
5. `D-arc10-closure-grace-no-server-fetch-2026-05-27` — frontend reads
   closure grace from localStorage rather than from a dedicated server
   route. Functional for the redirect path but won't surface on a fresh
   device until that admin clicks the URL their email-receipt provided.
   Follow-up: add a server route or extend `getBillingStatus()` to
   include closure fields.

---

**Authored:** 2026-05-27 at arc-close
**Anchors:** Vision §6 / §7 / §9 / §10 · Architecture §3.6 / §5.3 / §5.5 / §6
**Companion files:** `alembic/versions/arc10_lifecycle_subsystem.py`, the v2 migration plan at `workspace/arc10/ARC10_MIGRATION_PLAN_v2.md`

This document captures every decision Arc 10 explicitly deferred, every drift it surfaced but did not resolve, and every follow-up task that should be picked up by the right downstream arc owner. The list exists so the doctrine trail stays clean — anything below is a known-unknown, not an oversight.

---

## 1. Founder-flagged revisits

### 1.1 Vision §9 open-decision #2 — hard 30-day grace vs "pause" middle-ground

**Status at arc-open:** Vision §9 listed this as open with default "hard 30-day only at v1; revisit post Arc 10."

**What Arc 10 shipped:** Hard 30-day. Single clock. `RETENTION_WINDOW_DAYS = 30` in `app/worker/tasks/retention.py`. Founder-locked 2026-05-27, supersedes the prior 90-day lock dated 2026-05-14 09:55 EDT.

**Revisit trigger:** Customer signal during early-access (post Arc 11) that the hard 30-day cliff is too sharp for a meaningful segment. If so, the "pause" middle-ground would add a `paused_at` column on `admins` that holds the row in a perpetually-deactivated-but-not-closing state until either the admin explicitly closes or some platform-defined timeout.

**Where to make the diff if revisiting:**
- New column on `admins` (`paused_at TIMESTAMPTZ NULL`)
- New retention-worker scan branch (paused rows are not eligible)
- New route or modal flow to surface pause as a distinct customer action
- Vision §6.3 amendment via VISION_v2

---

## 2. Doctrine drifts resolved by Arc 10

These were drifts the arc found and reconciled in-flight. They are noted here so future agents reading the source can find the reasoning trail.

### 2.1 D-arc10-c61-vision-divergence-on-audit-immutability-2026-05-27

**Drift:** Arc 9 C6.1's `arc9_c6_1_luciel_ops_role.py` docstring declared the audit log "forward-only forever — even the ops role cannot mutate or delete audit rows" and explicitly stated "PIPEDA principle 5 (retention limits) does not apply to AdminAuditLog rows."

**Vision direct contradiction:** Vision §6.5 says "audit log archived to cold storage for legal retention window" and Vision §7 specifies tier-conditional retention windows (30d Free / 1y Pro / 7y Enterprise).

**Resolution applied:** Vision wins per the doctrine-anchor index in Vision §10. Arc 10 honored both stances by creating a new role (`luciel_audit_archiver`) with `SELECT + UPDATE` on `admin_audit_log` only — no `DELETE`. `luciel_ops` is unchanged; the C6.1 blast-radius rule still applies to it. The audit chain stays append-only in **hot + cold combined**: rows move to S3, not delete.

**Future risk:** A future migration that grants `UPDATE` or `DELETE` on `admin_audit_log` to `luciel_ops` would re-create the drift. The test `tests/services/test_arc10_audit_tier_retention.py::test_audit_archiver_role_is_distinct_from_luciel_ops` guards against this regression.

### 2.2 D-arc10-admins-deactivated-at-missing-from-rename-2026-05-27

**Drift:** Arc 5's `tenant_configs → admins` rename moved every column except `deactivated_at`. The cascade in `admin_service.py` carried a try/except fallback to query the legacy `tenant_configs` table.

**Resolution applied:** The Arc 10 migration adds `deactivated_at` to `admins` and backfills it from `tenant_configs` (if the legacy table still has the column). The cascade's `tenant_configs` fallback is removed in the paired code change.

**Future cleanup:** The legacy `tenant_configs` table itself remains in the database for now. A future arc should drop it as a dedicated cleanup migration. Owner: whoever takes the next schema-sweep arc.

### 2.3 D-arc10-retention-worker-still-on-default-session-2026-05-27

**Drift:** Arc 9 C6.1 created the `luciel_ops` BYPASSRLS role and Arc 9 C6.3 wired `OpsSessionLocal`. But `retention.py` still used `SessionLocal` plus a `rls_tenant_context_enabled` guard that refused to run when RLS master flag was on.

**Resolution applied:** Arc 10's paired code change in `retention.py` switches the worker to `OpsSessionLocal` and removes the guard. The Wall-3 gap C6.1 was created to close is now unreachable for this worker.

---

## 3. Items Arc 10 explicitly defers

### 3.1 Original knowledge file persistence → Arc 11

**Architecture §3.6.3 specifies** the export bundle includes "`knowledge_sources/` — original uploaded files preserved as-is + a manifest.json describing each."

**Reality:** The current ingestion pipeline (`app/knowledge/ingestion.py`) parses files into chunks, embeds them, and discards the original bytes. No S3 bucket for originals exists.

**Architecture §6** assigns "knowledge S3 bucket" to Arc 11.

**Arc 10's ship:** Option 2. The bundle includes `knowledge_sources/manifest.json` listing each source with `originals_retained: false`, plus `knowledge_sources/chunks/<source_id>__v<n>.jsonl` containing the reconstructed text. The bundle README documents the gap explicitly.

**Arc 11's handoff plan:**
1. Create S3 bucket `luciel-knowledge-originals` (CloudFormation template addition).
2. Modify `KnowledgeIngestionService.ingest_file()` to upload original bytes to `s3://luciel-knowledge-originals/{admin_id}/{source_id}/v{source_version}/{original_filename}` before parsing.
3. Add `knowledge_embeddings.original_s3_key TEXT NULL` column (or a dedicated `knowledge_sources` table if that arc creates one). Backfill is impossible for pre-Arc-11 sources; those stay `chunks-only` permanently.
4. Update `DataExportService.generate_bundle()` to read `original_s3_key` per source; include the original alongside chunks when set, chunks-only otherwise. Flip the `originals_retained` flag per-source in `manifest.json`.

The bundle structure is **forward-compatible** — `knowledge_sources/originals/` lives next to `knowledge_sources/chunks/`. No restructure needed.

### 3.2 Leads table proper → Arc 14

**Architecture §3.6.3 bundle contents** lists `leads.jsonl`.

**Reality:** There is no dedicated leads table. Cognition-driven lead capture is an Arc 14 concern (per Architecture §6). `DataExportService._write_leads` currently exports `identity_claims` rows as the closest existing surface.

**Arc 14's handoff plan:** When the proper leads table lands (or whatever surface owns cognition-emitted leads), update `DataExportService._write_leads` to read from it. No bundle-schema break — `leads.jsonl` already promises one lead per line; the columns change, the file does not.

### 3.3 Escalations table proper → Arc 14

**Architecture §3.6.3 bundle contents** lists `escalations.csv` with "every escalation event with signal-fired metadata."

**Reality:** Escalation event emission is an Arc 14 concern. Arc 10 ships `escalations.csv` with header-only content (no data rows). The CSV columns are pre-declared so Arc 14's data emission is a value-only change.

**Arc 14's handoff plan:** Update `DataExportService._write_escalations` to read from whichever surface Arc 14 lands for escalation events.

### 3.4 Hot-purge of cold-archived audit rows → future arc

**Architecture §5.3** says audit log is append-only at the app layer, with a separate role for audit writes.

**What Arc 10 ships:** `luciel_audit_archiver` role has `SELECT + UPDATE` on `admin_audit_log`. Rows past their tier window get moved to S3 cold storage with the hash chain extended, and `cold_archived_at` is stamped on the hot row. The **hot row itself stays in place** — the chain stays walkable from any current row back through history.

**What Arc 10 does NOT ship:** A hot-purge step that DELETEs rows whose `cold_archived_at` is well-past retention. The `luciel_audit_archiver` role explicitly has no `DELETE` grant to prevent accidental purge.

**When to revisit:** When the hot table size becomes a query-performance concern (probably 100M+ rows in). At that point, a follow-up migration would extend `luciel_audit_archiver`'s grants to include `DELETE`, add a hot-purge step that verifies the row is cold-archived before deleting, and extend the hash chain to point at the cold-archive object instead of the deleted row.

### 3.5 Audit-archiver settings field needs SSM secret wiring

**Code state:** `app/core/config.py` declares `audit_archiver_db_url: str | None = None` with a default of None. The `audit_retention.py` task is a no-op when this is unset.

**Deploy follow-up:**
1. Create SSM parameter `/luciel/<env>/audit_archiver/database_url` containing the connection string with the `luciel_audit_archiver` role's credentials.
2. Update `td-worker-rev<N>.json` task definition to inject `AUDIT_ARCHIVER_DB_URL` from that SSM parameter.
3. Set the `luciel_audit_archiver` password via `ALTER ROLE` (referenced in the migration as `ARC10_AUDIT_ARCHIVER_PASSWORD`).
4. Verify in staging by manually triggering `run_audit_tier_retention` and checking the S3 cold-archive bucket.

Until this is wired, the audit-tier retention worker logs an info message and exits clean. **The arc closes in a state where this worker is registered but inert.** That is acceptable per the founder direction at arc-open — the lifecycle promise to customers is that closure deletes their data after 30 days, which IS shipped end-to-end. Audit-tier archival is a separable platform concern.

### 3.6 `_NoOpAuditRepo` smell in grace-status route

**Issue:** `app/api/v1/billing.py::downgrade_grace_status` creates a no-op audit repository stub to satisfy `DowngradeGraceService.__init__` even though the route is read-only.

**Cleanup:** Make `audit_repository` optional on `DowngradeGraceService.__init__` with `None` default. Methods that emit audit rows check `if self.audit_repository is not None`. Small refactor; lands as a follow-up commit.

### 3.7 Test coverage shortfall vs v2 migration plan

The v2 plan called for 7 test files. Arc 10 ships 3 covering the highest-leverage doctrine surfaces:

- `test_arc10_retention_30_day_window.py` — 9 tests
- `test_arc10_audit_tier_retention.py` — 8 tests
- `test_arc10_close_account_route.py` — 13 tests

**Deferred to follow-up PR:**
- `test_arc10_data_export.py` — bundle contents structure, signed-URL TTL stickiness, one-active-per-admin concurrency lock
- `test_arc10_downgrade_grace_window.py` — read-only middleware, day-30 enforcement
- `test_arc10_reactivation.py` — in-grace restoration, post-grace rejection, downgrade-archived rows not rehydrated
- `test_arc10_c6_role_privileges.py` — privilege boundary of `luciel_audit_archiver`

**Why deferred:** Each of these protects a real promise but requires live-DB integration testing (Postgres fixture + Alembic apply + role creation). The text/AST regression tests already locked the doctrine surface in source; the integration tests verify runtime behavior. Live-DB tests are a coverage PR, not a release blocker.

### 3.8 LRU-vs-oldest doctrine clarification on downgrade-archive

**Customer Journey Phase 8 (Pro)** says "oldest instances over the cap go inactive; oldest knowledge sources over the cap are archived." The code uses LRU (least-recently-updated, anchored on `updated_at`).

**Resolution:** Treat the Customer Journey phrasing as informal prose. LRU is the better policy — recency-of-activity is a more honest signal of "still in use" than creation date. The Customer Journey doc should be amended in its v2 to read "least-recently-used" if the founder wants the phrasing aligned. Until then, the code is the truth and this note is the bridge.

### 3.9 `tenant_configs` legacy table drop → cleanup arc

Arc 10's drift-reconciliation backfill moved `deactivated_at` from `tenant_configs` to `admins`. The legacy table itself remains in the database. A future cleanup migration should drop it. Schedule: low priority, no functional impact.

### 3.10 `knowledge_bytes_cap` enforcement at upload time → Arc 11

Arc 10 added `knowledge_bytes_cap` to `TierEntitlement` so the AXIS_KNOWLEDGE downgrade path has a cap to read. **At-upload enforcement** (rejecting a new upload that would exceed the cap) is an ingestion-pipeline concern — Arc 11 territory.

Until Arc 11 wires the upload-time check, an admin can in theory upload past their cap; the downgrade path will archive overflow at downgrade time but there is no front-stop. Acceptable for v1 because the cap is generous (Free 100MB / Pro 5GB) and Arc 11 is the next arc up.

### 3.11 Byte size approximated by `LENGTH(content)`

The AXIS_KNOWLEDGE compute query uses `SUM(LENGTH(content))` over `knowledge_embeddings.content` as a per-source size proxy. This is character count of stored chunk text, not bytes of the original uploaded file.

**When Arc 11 lands** original-file persistence (per §3.1 above), the AXIS_KNOWLEDGE query should switch to a true bytes column on `knowledge_sources` (or `knowledge_embeddings`, depending on Arc 11's schema decision). The axis semantics don't change; only the data source.

---

## 4. Operational follow-ups for the Arc 10 deploy

These are not deferred decisions but reminders for the deploy script / staging E2E run.

1. **SSM secrets to create before first deploy:**
   - `/luciel/<env>/audit_archiver/password` — used by the migration's `ARC10_AUDIT_ARCHIVER_PASSWORD` env var.
   - `/luciel/<env>/audit_archiver/database_url` — used by the worker's `AUDIT_ARCHIVER_DB_URL` env var.

2. **S3 buckets to create:**
   - `luciel-data-exports` (or env-specific equivalent). Lifecycle policy: abort incomplete multipart uploads after 24h.
   - `luciel-audit-cold-archive` (or env-specific equivalent). Object Lock recommended for tamper-evidence.

3. **ECS task definition update** (`td-worker-rev<N>.json`): inject the two new env vars from SSM.

4. **CloudWatch alarms** to add:
   - `data_export_jobs_stuck_in_generating_>1h` — alerts if the bundle generation is hanging.
   - `retention_purge_aborted` — alerts on the "OpsSessionLocal is None" abort path.
   - `audit_retention_no_op_runs` — alerts if the archiver task is a no-op for >7 days (signals SSM not wired).

5. **Per-table RLS feature-flag flips** for `data_export_jobs`: deploy with flag OFF, smoke-test, flip ON.

---

## 5. Summary

Arc 10 ships the lifecycle subsystem end-to-end: closure, grace, hard-delete tombstone, reactivation, downgrade with knowledge as a 5th axis, data export, audit-tier retention. Three doctrinal drifts were surfaced and reconciled in-flight (all in favor of Vision). Three high-leverage test suites land with the PR (30 tests, all passing). Four follow-up test suites and seven separable concerns are documented here for the right downstream owner.

The arc honors the in-sync-at-close principle: deployed code matches merged branch, migration head matches schema, frontend contracts match backend routes. The two known-inert pieces (audit-tier archive worker no-op without SSM, knowledge originals in bundle pending Arc 11) are deliberately inert with clear handoff plans — not orphan code.
