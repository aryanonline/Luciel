# Arc 10 — Deferred Decisions and Follow-Up Items

**Arc:** 10 — Deactivation Lifecycle, Cap Reclamation, Pre-Closure Data Export
**Status:** CLOSED 2026-05-27 19:45 EDT (deployed end-to-end to prod, E2E verified)
**Author:** Sandbox agent (under founder direction)

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
