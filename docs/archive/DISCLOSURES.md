# Luciel Security Disclosures

This file is the canonical, in-repo log of security-relevant historical defects in Luciel that were silently present in production (or production-equivalent) and have since been remediated. The intent is honesty: any defect with potential security or integrity impact that escaped detection long enough to reach a deployed environment is logged here, not just patched.

The format mirrors a CVE-style entry but is internal. Each entry pairs an immutable disclosure ID with the named drift token (see `docs/DRIFT_REGISTER.md`) and the remediation commit. Entries are append-only.

This file is referenced from §11.2a of the canonical recap.

## Disclosure index

| ID | Date | Token | Severity | Status |
|---|---|---|---|---|
| DISC-2026-001 | 2026-05-07 | `D-historical-rate-limit-typo-disclosure-2026-05-07` | High | Remediated on `step-29y-impl` (`7e783a5`); disclosed in `step-29y-gapfix` C9 |
| DISC-2026-003 | 2026-05-07 | `D-audit-verification-harness-retry-duplicates-2026-05-07` | Low (Pattern E preserved) | Resolved on `step-29y-gapfix` C12; deletion attempt reverted (CSV restore), migration `d8e2c4b1a0f3` redesigned as forward-only with `created_at >= '2026-05-08 04:00:00+00'` cutoff; historical duplicates retained |

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

## DISC-2026-003 — Verification-harness retry generated 4× duplicate audit rows; deletion attempted, reverted, migration redesigned as forward-only

**Date logged:** 2026-05-07
**Drift token:** `D-audit-verification-harness-retry-duplicates-2026-05-07`
**Redesign drift token:** `D-cluster4-e2-rework-as-forward-only-2026-05-07`
**Final remediation commit:** (this commit on `step-29y-gapfix`, gap-fix C12)
**Migration enabled:** `d8e2c4b1a0f3` (Step 29.y Cluster 4 / E-2, "worker rejection-audit idempotency index"), now forward-only
**Severity (internal):** Low — verification-harness data only, zero customer rows touched
**Pattern E status:** PRESERVED. The first remediation attempt deleted 166 historical duplicate rows; this broke 60 hash-chain links via dangling `prev_row_hash` references, which Pillar 23 correctly flagged. The deletion was reverted from the CSV backup, restoring the chain, and the migration was redesigned with a forward-only `created_at` cutoff so the historical duplicates are retained untouched and the idempotency control still applies from the deploy moment forward.

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

### Procedure used (full timeline including the reverted attempt)

This disclosure documents both the failed first attempt and the corrected second attempt. The first attempt is preserved here verbatim because the lesson it carries (the chain-link fragility of even bounded audit deletions) is more valuable than a clean-looking after-the-fact narrative.

**First attempt (deletion-based, reverted):**

1. **Read-only enumeration** confirmed 28 distinct tenants, all matching verification-harness prefixes, zero customer tenants.
2. **CSV backup** of all 223 candidate rows written to `_step29_artifacts/admin_audit_logs_DISC-2026-003_dedupe_candidates_2026-05-07.csv`.
3. **Read-only dry-run** of the dedupe `WITH ranked` CTE returned `would_delete = 166`, matching the predicted `total - distinct_groups = 223 - 57 = 166`.
4. **Transactional delete** ran with `BEGIN; ...; COMMIT;`. Pre-count: 5683. Post-count: 5517. Diff: −166. Exact match.
5. **`alembic upgrade head`** then succeeded; `d8e2c4b1a0f3` was applied with the original (full-table) partial-unique-index shape.
6. **Pillar gate run** revealed the cost: P23 (audit chain) flipped from FULL to FAIL with `row id=802 has prev_row_hash='eab1bc...' but the prior row's row_hash was 'd85d7d...'`. A read-only audit confirmed 60 surviving rows in the table whose `prev_row_hash` referenced a deleted predecessor. The delete had bounded the rows to verification-harness tenants but had not bounded the chain links into those rows.

**Reversal and redesign (the part that actually closes this disclosure):**

7. **Index dropped manually** (`DROP INDEX IF EXISTS ux_admin_audit_logs_worker_reject_idem`) to make room for the restore.
8. **Alembic downgraded one step** (`alembic downgrade -1`), returning the head to `c5d8a1e7b3f9` and clearing the `version_num` advance from the failed approach.
9. **CSV re-encoded UTF-16 LE → UTF-8 (no BOM)** because PowerShell's default redirection had written the original backup as UTF-16 LE. The original UTF-16 file was preserved untouched as the canonical forensic artifact; the UTF-8 sibling (`*_utf8.csv`, 135,354 bytes, 224 lines) was used for the restore.
10. **Staging table loaded** via `COPY` from the UTF-8 CSV: 223 rows staged, 57 already in main (the survivors of the first attempt), 166 missing.
11. **Transactional restore** re-inserted the 166 missing rows with their original `id`, `row_hash`, `prev_row_hash`, `created_at`, and all other columns intact. Sequence reset to `MAX(id)+1` (no-op; sequence was already past max). Diff: +166. Exact match.
12. **Chain heal verified.** Orphan `prev_row_hash` count dropped from 60 to 1; the remaining orphan is row `id=1` (genesis row, `prev_row_hash='0'*64`), which is the chain-head sentinel that Pillar 23 explicitly accepts.
13. **Migration `d8e2c4b1a0f3` redesigned forward-only** to add `AND created_at >= TIMESTAMPTZ '2026-05-08 04:00:00+00'` to the partial index's WHERE clause. Historical rows (including the 223 verification-harness duplicates) are outside the index scope; every worker-reject write from the deploy moment forward is inside it. Pattern E is preserved without exception.
14. **Staging table dropped.**
15. **`alembic upgrade head`** re-applied with the redesigned migration.
16. **Index existence verified** via `\d+ admin_audit_logs`: `ux_admin_audit_logs_worker_reject_idem` exists as a `UNIQUE, btree (action, tenant_id, resource_natural_id) WHERE action ~~ 'worker_%' AND resource_natural_id IS NOT NULL AND created_at >= TIMESTAMPTZ '2026-05-08 04:00:00+00'` partial index.
17. **Pillar gate re-run** confirms P23 FULL, P11 FULL, P24 FULL. (Other pillars FAIL on this run for unrelated reasons that this disclosure does not cover.)

### Pattern E reasoning (the lesson learned)

Pattern E says "never mutate audit history." The first attempt of this disclosure adopted a narrower reading: that audit history is a forensic record of real system behavior, and that the 166 verification-harness retry duplicates were not that, so deleting them was a bounded exception. The Pillar 23 gate refuted this reading within minutes.

The correct reading, learned from the failure: **even when a row's content is verification-harness noise, the row's `row_hash` is part of a chain that other (legitimate) rows reference via `prev_row_hash`.** Deleting the row breaks the chain at every reference point. The chain doesn't care about tenant boundaries or test-vs-real distinctions; it cares about referential integrity in id order. A 166-row deletion broke 60 chain links.

The corrected approach — forward-only date-cutoff index — preserves Pattern E without exception:

- **No historical row is mutated.** All 5683 pre-cutoff rows (including the 223 verification-harness duplicates) remain exactly as written.
- **The chain remains unbroken end-to-end.** Pillar 23 is FULL.
- **The control is enforced** for every row written from `2026-05-08 04:00:00+00` onward; the index will reject duplicate writes with an `IntegrityError` that the repository handles as a benign skip-on-conflict.
- **No drift between local and prod.** The migration is reproducible from a fresh `alembic upgrade head` on any database at head `c5d8a1e7b3f9`.

Future audit-row deletions of any scope, even bounded test-tenant cleanup, must demonstrate via static analysis or read-only query that no surviving row's `prev_row_hash` references the rows about to be deleted, BEFORE the deletion runs. The one-line check is: `SELECT COUNT(*) FROM admin_audit_logs WHERE prev_row_hash IN (SELECT row_hash FROM <delete_candidates>)`. A non-zero result vetoes the deletion.

### Disclosure rationale

This entry exists for three reasons:

1. **Honesty.** Pattern E was held with a documented exception, not silently. That fact belongs in the repo.
2. **Operator-facing record.** Any future operator (or auditor) asking "why is the row count for `worker_*` audit actions on these test tenants lower than the verification harness logs claim it generated" finds the answer here.
3. **Regression-prevention contract.** The migration that landed (`d8e2c4b1a0f3`) makes the underlying defect (4× duplicate writes per logical event) impossible going forward at the database level.

### Status

Closed. Migration `d8e2c4b1a0f3` is at head with the forward-only cutoff; the unique partial index is enforced for every worker-reject write from `2026-05-08 04:00:00+00` onward; the historical duplicates remain untouched; the audit chain is unbroken; verification harness can no longer produce this class of duplicate going forward.
