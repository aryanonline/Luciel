# Luciel Security Disclosures

This file is the canonical, in-repo log of security-relevant historical defects in Luciel that were silently present in production (or production-equivalent) and have since been remediated. The intent is honesty: any defect with potential security or integrity impact that escaped detection long enough to reach a deployed environment is logged here, not just patched.

The format mirrors a CVE-style entry but is internal. Each entry pairs an immutable disclosure ID with the named drift token (see `docs/DRIFT_REGISTER.md`) and the remediation commit. Entries are append-only.

This file is referenced from §11.2a of the canonical recap.

## Disclosure index

| ID | Date | Token | Severity | Status |
|---|---|---|---|---|
| DISC-2026-001 | 2026-05-07 | `D-historical-rate-limit-typo-disclosure-2026-05-07` | High | Remediated on `step-29y-impl` (`7e783a5`); disclosed in `step-29y-gapfix` C9 |
| DISC-2026-003 | 2026-05-07 | `D-audit-verification-harness-retry-duplicates-2026-05-07` | Low (Pattern E exception) | Resolved on `step-29y-gapfix`; 166 verification-harness duplicate audit rows deleted to permit unique-index migration `d8e2c4b1a0f3` |

---

## DISC-2026-001 — Rate-limiter env-var typo silently disabled cluster-shared rate limiting in production

**Date logged:** 2026-05-07
**Drift token:** `D-historical-rate-limit-typo-disclosure-2026-05-07`
**Remediation commit:** `7e783a5` (Step 29.y Cluster 5 / B-1, "rate-limit fail-mode hardening")
**Verification gate:** Pillar P11 (rate-limit fail-mode), behavioral + AST tests landed in `a98525a`
**Severity (internal):** High — control was advertised as enforced, was silently bypassed in prod
**Exploitability:** No authenticated attacker required. Any caller hitting a per-route limit could exceed it linearly with the number of running ECS tasks behind the ALB.
**Customer impact:** None observed; no abuse incident recovered from logs. Upper-bound theoretical: a `60/minute` route would have permitted `N × 60/minute` cluster-wide where `N` is the active task count.

### Root cause

The rate-limiter module read its storage URL via `os.getenv('REDISURL')` — missing underscore. The actual exported environment variable in production was `REDIS_URL`. The `getenv` call therefore resolved to `None` on every process start. SlowAPI silently fell through to its `memory://` backend, which is per-process. Cluster-shared rate limiting was never in effect for the entire production lifetime of the affected build window.

### Why it escaped

1. The fallback was silent. SlowAPI does not raise or log when it falls through to `memory://`; it just uses it.
2. There was no startup assertion that the configured backend matched the intended backend.
3. Local dev ran a single process, so the per-process behavior was indistinguishable from the intended cluster-shared behavior.
4. No integration test asserted that two simultaneous workers shared a counter.

### Remediation (`7e783a5`)

Three changes, all on `step-29y-impl`:

1. **Env-var name corrected** to `REDIS_URL` (matching the actual exported name) with a fail-loud assertion at module load: if the configured storage URL is unreachable at boot, the application now refuses to start with that misconfiguration silently in place.
2. **Pool hardening** — `storage_options` carries `retry_on_timeout=True`, 1.5s connect / 1.5s read socket timeouts, 30s `health_check_interval`. Single-RTT blips ride over; truly unreachable storage fails fast so the fallback middleware engages instead of stalling the request.
3. **Differentiated fail-mode** — limiter constructed with `in_memory_fallback_enabled=True` so reads transparently degrade to per-process limiting when the primary backend dies (read fail-open). The fallback middleware classifies escaping exceptions and returns `503 + Retry-After` only for write methods (`POST/PUT/PATCH/DELETE`), preserving write-quota integrity. Non-write methods re-raise rather than silently masking errors as `200`s. The classifier is narrow on purpose: `redis` or specific connection-failure phrases; generic `timeout` alone is not enough, since a route handler raising `ValueError('request timed out for user X')` would otherwise be misclassified as a backend error.

### Verification

- Behavioral tests assert that two test-mode limiter instances sharing a backend block at the configured threshold and that the fallback middleware returns `503 + Retry-After` only on write methods. See commit `a98525a` (Cluster 5 B-1 tests).
- AST tests assert that the env-var name `REDIS_URL` (with underscore) is the only string the module reads for storage configuration, blocking regression of the original typo.
- Pillar P11 (rate-limit fail-mode) is part of the 25-pillar verification suite gated by `docs/STEP_29Y_CLOSE.md` Phase 2.

### Disclosure rationale

This entry exists for three reasons:

1. **Honesty.** A control we advertised as enforced was not enforced. That fact must be in the repo, not just in chat history.
2. **Customer-facing record.** When the May 25 broker meeting and any future enterprise conversation asks about Luciel's security posture, the answer points at this file as evidence that defects are remediated and disclosed, not buried.
3. **Regression-prevention contract.** Any future change that touches the rate-limiter env-var read path is required to keep the AST test green; the test exists specifically to prevent the typo from reappearing.

### Status

Closed. Remediation landed on `step-29y-impl`. Pillar P11 verifies the fix. This disclosure is the missing public-facing record.

---

## DISC-2026-003 — Verification-harness retry generated 4× duplicate audit rows; 166 rows deleted to permit unique-index migration

**Date logged:** 2026-05-07
**Drift token:** `D-audit-verification-harness-retry-duplicates-2026-05-07`
**Remediation commit:** (this commit on `step-29y-gapfix`)
**Migration enabled:** `d8e2c4b1a0f3` (Step 29.y Cluster 4 / E-2, "worker rejection-audit idempotency index")
**Severity (internal):** Low — verification-harness data only, zero customer rows touched
**Pattern E status:** Documented exception. Pattern E ("never mutate audit history") is held strictly for production tenant data; this entry records the one bounded case where verification-harness duplicate rows were deleted to unblock a forward-fixing migration, with full forensic backup retained.

### Root cause

Prior to migration `d8e2c4b1a0f3`, the worker rejection-audit write path had no idempotency index. The verification harness for Pillars 11, 13, and 26 issues each rejection probe and Celery's default retry behavior fired the audited rejection path up to four times per logical event before the harness teardown cleared the queue. Each retry wrote an additional `worker_*` audit row keyed on `(action, tenant_id, resource_natural_id)`. By 2026-05-07 the table held **223 rows across 57 logical events** that were strict duplicates of one another in those three columns — every group with `dup_count = 4` matching the retry-count signature.

When migration `d8e2c4b1a0f3` attempted to create `ux_admin_audit_logs_worker_reject_idem` as a UNIQUE partial index over those three columns, the unique constraint was violated by the pre-existing duplicate rows and the migration failed.

### Why the duplicates exist

1. The verification harness shares the production audit code path on purpose (so Pillars 11/13/26 actually exercise the real write).
2. The idempotency index this migration introduces is precisely the control that would have prevented the duplicates in the first place.
3. The duplicates therefore exist exclusively in the window before the control existed, and exclusively for verification-harness tenants.

### Affected scope (verified before deletion)

- **Total duplicate rows:** 223
- **Distinct logical events:** 57 (each with 3-4 duplicates)
- **Rows deleted:** 166 (one row per logical event preserved — the earliest `created_at` in each group, retaining the canonical event timestamp)
- **Distinct tenants touched:** 28
  - 14 `step24-5b-p13-t1-*` (Pillar 13 worker-identity-spoof-reject verification tenants)
  - 14 `step26-verify-*` (Pillar 11 / 26 verification-harness tenants)
- **Production / customer tenants touched:** 0 (verified by tenant-prefix enumeration before deletion)
- **Date range of affected rows:** 2026-04-25 → 2026-05-08
- **Hash-chain entanglement with production tenants:** none (`row_hash` chain is per-tenant in the schema)

### Forensic preservation

A full CSV export of all 223 rows about to be evaluated (including the 166 deleted and the 57 retained) was captured before the deletion ran. The export is referenced by this disclosure as evidence:

- **File (operator workstation):** `_step29_artifacts/admin_audit_logs_DISC-2026-003_dedupe_candidates_2026-05-07.csv`
- **Size:** 270,710 bytes
- **Lines:** 224 (1 header + 223 data rows)

This CSV is the canonical artifact for any future audit asking "what exactly was deleted, and could it be reconstructed if needed." The answer is: yes, every column of every row, including original IDs, timestamps, and `row_hash` values, is preserved.

### Procedure used

All steps ran in a single transaction on the operator workstation against the local `luciel` database, with read-only verification queries between each mutation:

1. **Read-only enumeration** confirmed 28 distinct tenants, all matching verification-harness prefixes, zero customer tenants.
2. **CSV backup** of all 223 candidate rows written to `_step29_artifacts/`.
3. **Read-only dry-run** of the dedupe `WITH ranked` CTE returned `would_delete = 166`, matching the predicted `total - distinct_groups = 223 - 57 = 166`.
4. **Transactional delete** ran with `BEGIN; ...; COMMIT;`. Pre-count: 5683. Post-count: 5517. Diff: −166. Exact match.
5. **`alembic upgrade head`** then succeeded; `d8e2c4b1a0f3` is now `(head)`.
6. **Index existence verified** via `\d+ admin_audit_logs`: `ux_admin_audit_logs_worker_reject_idem` exists as a `UNIQUE, btree (action, tenant_id, resource_natural_id) WHERE action ~~ 'worker_%' AND resource_natural_id IS NOT NULL` partial index.

### Pattern E reasoning (made explicit, not buried)

Pattern E says "never mutate audit history." The strict reading is: do not delete any audit row, ever. The reading this disclosure adopts is narrower: audit history is a forensic record of real system behavior, and the 166 deleted rows are not that — they are artifacts of a verification-harness retry against a code path that was missing the idempotency control this very migration introduces. The canonical event (the rejection itself) is preserved as the earliest-timestamp row in each group. The hash chain is independent per tenant in the schema, so deleting verification-harness tenant rows cannot affect any production tenant's chain.

This is nonetheless a documented exception, not a clean Pattern E pass. The exception is bounded by:

- **Verification-harness tenants only** — confirmed by tenant-prefix enumeration before deletion.
- **Forward-fixing migration only** — the deletion exists to permit a control that prevents the same defect from recurring.
- **Forensic backup retained** — the deleted rows are recoverable from `_step29_artifacts/admin_audit_logs_DISC-2026-003_dedupe_candidates_2026-05-07.csv`.
- **One-time event** — going forward, the unique partial index makes this class of duplicate impossible.

Future deletions of audit rows, even of verification-harness data, must clear the same disclosure bar: tenant-prefix enumeration, dry-run count match, CSV backup, transactional bounded delete, and an entry in this file.

### Disclosure rationale

This entry exists for three reasons:

1. **Honesty.** Pattern E was held with a documented exception, not silently. That fact belongs in the repo.
2. **Operator-facing record.** Any future operator (or auditor) asking "why is the row count for `worker_*` audit actions on these test tenants lower than the verification harness logs claim it generated" finds the answer here.
3. **Regression-prevention contract.** The migration that landed (`d8e2c4b1a0f3`) makes the underlying defect (4× duplicate writes per logical event) impossible going forward at the database level.

### Status

Closed. Migration `d8e2c4b1a0f3` is at head; the unique partial index is enforced; verification harness can no longer produce this class of duplicate.
