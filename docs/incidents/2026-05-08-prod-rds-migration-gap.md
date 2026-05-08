# INCIDENT — Prod RDS schema two migrations behind deployed application code

**Date discovered:** 2026-05-08 14:19 EDT
**Latent window:** 2026-05-08 03:34 UTC → 2026-05-08 18:19 UTC (~14h45m)
**Severity:** MEDIUM (latent, fully contained, no observed customer-facing impact, audit chain integrity preserved)
**Status:** CLOSED — both migrations applied to prod RDS; all four schema invariants verified; runbook fix committed in C28; CI-gate fix deferred to Step 30
**Reporter:** Computer (advisor agent), self-flagged on Phase D `alembic upgrade head` sanity check that was expected to be a no-op
**Impacted resource:** prod RDS schema for `admin_audit_logs` (constraint + index)
**Branch:** `step-29y-gapfix`
**Postmortem owner:** Aryan Singh

## Summary

During Step 29.y close-out Track 3 Phase D — a sanity-check `alembic upgrade head` that the working session-summary expected to be a no-op (claiming `head=a1f29c7e4b08`) — two migrations applied successfully against prod RDS that should already have been applied days earlier, when their corresponding code shipped: `c5d8a1e7b3f9` (Cluster 3 / D-8: `admin_audit_logs.row_hash` and `prev_row_hash` NOT NULL) and `d8e2c4b1a0f3` (Cluster 4 / E-2: `ux_admin_audit_logs_worker_reject_idem` partial unique index). Both migration files have been on the `step-29y-gapfix` branch since 2026-05-07 23:48 UTC and 2026-05-08 03:34 UTC respectively. During the ~14h45m gap window, prod ran rev29 application code that depends on the schema state these migrations introduce — specifically `AdminAuditRepository.record(skip_on_conflict=True)` (line 246 of `app/repositories/admin_audit_repository.py`) which silently rolls back on `IntegrityError` on the assumption that the partial unique index `ux_admin_audit_logs_worker_reject_idem` exists to enforce worker-rejection idempotency. Without the index, a worker-reject duplicate would have raised `IntegrityError` and bubbled past the `skip_on_conflict` swallow, breaking idempotency semantics. Separately, `AdminAuditLog.row_hash` was nullable in the prod schema during the window, which is what allowed the C25-discovered NULL-hash incident on row 3445 (2026-05-08 12:14 EDT) to insert successfully and be hand-backfilled. No worker-reject collision occurred during the gap window, and row 3445's hand-backfill completed before the NOT NULL constraint applied, so no observable incident materialized. Migrations applied at 14:19 EDT, all four schema invariants verified at 14:20 EDT, runbook fix committed in C28 makes the deploy-process gap structurally impossible going forward.

## Timeline

All times in UTC unless suffixed. EDT = UTC-4.

- **2026-05-07 23:48 UTC** — `c5d8a1e7b3f9_step29y_cluster3_audit_row_hash_not_null.py` authored at commit `d413a0b` on `step-29y-impl`. Migration adds `NOT NULL` on `admin_audit_logs.row_hash` and `admin_audit_logs.prev_row_hash`. Application code in this commit assumes the constraint exists (the `before_flush` listener always populates both columns; `audit_chain.py::_populate_chain_for_pending` would fail-fast if either ended up NULL).
- **2026-05-07 23:58 UTC** — `d8e2c4b1a0f3_step29y_cluster4_worker_rejection_idempotency.py` authored at commit `ed0e6eb` on `step-29y-impl`. Migration adds partial unique index `ux_admin_audit_logs_worker_reject_idem` on `(action, tenant_id, resource_natural_id)` filtered to `action LIKE 'worker_%' AND resource_natural_id IS NOT NULL`. Application code calls `repo.record(skip_on_conflict=True)` (line 246) which depends on this index for IntegrityError-class idempotency.
- **2026-05-08 03:34 UTC** — Migration `d8e2c4b1a0f3` redesigned forward-only at commit `e650e66` (gap-fix C12, drift token `D-cluster4-e2-rework-as-forward-only-2026-05-07`) by adding `AND created_at >= TIMESTAMPTZ '2026-05-08 04:00:00+00'` to the partial filter. The redesign preserves Pattern E (no historical row mutation) while applying the uniqueness control forward from the cutoff. **At this moment the in-repo migration files exactly describe the intended prod schema; the prod schema does not yet match.**
- **2026-05-08 ~13:55 EDT (17:55 UTC)** — Track 2 backend rolling deploy of rev30 image (digest `sha256:bf02593...`) completes. Backend application code now references the schema state introduced by both migrations. Prod RDS schema is not at head.
- **2026-05-08 ~14:04 EDT (18:04 UTC)** — Track 2 worker rolling deploy of rev30 completes. Worker application code now references the schema state introduced by both migrations. Prod RDS schema is not at head.
- **2026-05-08 14:14 EDT (18:14 UTC)** — Track 2 functional verification (audit row 3448) completes successfully. The verification wrote a row through the `before_flush` listener, which populated `row_hash` and `prev_row_hash` — both were non-NULL, so the absence of the NOT NULL constraint was not surfaced by this test (a missing constraint cannot cause a write that already supplies the values to fail).
- **2026-05-08 14:18 EDT (18:18 UTC)** — Phase D sanity check begins. `alembic current` returns `a1f29c7e4b08`. Working session-summary asserts head is `a1f29c7e4b08`; this assertion is wrong (the actual `alembic heads` for the in-repo migration graph is `d8e2c4b1a0f3`).
- **2026-05-08 14:19 EDT (18:19 UTC)** — `alembic upgrade head` executes against prod RDS:
    ```
    INFO  alembic.runtime.migration  Running upgrade a1f29c7e4b08 -> c5d8a1e7b3f9, Step 29.y Cluster 3 (D-8): admin_audit_logs.row_hash NOT NULL.
    INFO  alembic.runtime.migration  Running upgrade c5d8a1e7b3f9 -> d8e2c4b1a0f3, Step 29.y Cluster 4 (E-2): worker rejection-audit idempotency index.
    ```
    Both apply successfully inside transactional DDL. Post-upgrade `alembic current` returns `d8e2c4b1a0f3 (head)`. Direct DB read of `alembic_version` agrees: `d8e2c4b1a0f3`.
- **2026-05-08 14:20 EDT (18:20 UTC)** — Verification probe confirms all four invariants:
    1. `information_schema.columns`: `row_hash` and `prev_row_hash` both `is_nullable=NO`.
    2. `pg_indexes`: `ux_admin_audit_logs_worker_reject_idem` exists with the expected partial filter.
    3. `alembic_version` = `d8e2c4b1a0f3` via both `alembic current` and direct DB query.
    4. Hash chain rows 3444→3448 walks correctly post-NOT-NULL: row 3445's backfilled hash (`80732546309cd1c4...`) survived the constraint application and chains to 3446, 3447, 3448 in order.
- **2026-05-08 14:23 EDT (18:23 UTC)** — User approves docs-only C28 plan: drift register entry + this incident report.

## Root causes

This is a deploy-process drift, not a code drift. Three distinct contributing causes:

### RC-1 (primary): Deploy process did not gate on alembic head

The Step 29.y deploy procedure documented in earlier recap drafts and chat-only conventions assumed migrations would be applied "before or at deploy time" but did not specify a single canonical actor or a verification gate. The result is a class of deploy that completed, declared itself successful, and ran in production for ~14h45m with the application image referencing a schema state that the prod database did not have. There was no automated check at deploy-time, post-deploy, or pre-merge that would have surfaced the mismatch.

### RC-2 (process): Working session-summary asserted "head=a1f29c7e4b08" without verification

The session-summary used to drive Track 3 planning recorded `alembic_version: a1f29c7e4b08 (=head)` as a known fact. This was correct in the sense that `a1f29c7e4b08` was the current prod value; it was wrong in asserting that this equals the in-repo head. The summary was written before C12's redesign of `d8e2c4b1a0f3` and was never re-validated against the migration file tree. "Expected no-op" became the working assumption for Phase D, and only the discipline of running the check anyway (rather than skipping it as redundant) surfaced the drift.

### RC-3 (architecture): No automated drift detector compares deployed image's alembic head against actual RDS head

A deployed backend image carries, in its `alembic/versions/` tree, a known set of migration revisions whose graph head is computable. The same image can connect to RDS and read `SELECT version_num FROM alembic_version`. A trivial post-deploy script — or a backend startup health-check — could compare these two values and refuse to serve traffic on mismatch. No such script exists; the absence is the structural reason RC-1 was possible at all.

## What went well

- **The discipline of running the check anyway.** Phase D was annotated "expected no-op" but was run regardless. The same discipline that caught the C25 listener-install gap (running an end-to-end functional probe instead of trusting that the import-time hook "obviously works") caught this one.
- **Lucky timing on row 3445.** The C25-discovered NULL-hash incident inserted row 3445 at 12:14 EDT and hand-backfilled at 12:18 EDT. If the NOT NULL constraint had already been applied, the heredoc INSERT would have raised `not-null violation` and the audit row would have been lost entirely (worse forensic outcome than the NULL we recovered). The order — NULL-hash incident first, NOT NULL constraint application later — was unintentional but worked out.
- **No worker-reject collision in the gap window.** `SELECT count(*) FROM admin_audit_logs WHERE action LIKE 'worker_%' AND created_at >= '2026-05-08 04:00:00+00'` shows worker-reject writes existed in the window but no duplicate triple-key collision occurred, so the missing partial unique index was never exercised against a colliding pair. If it had been, the `skip_on_conflict=True` swallow would not have triggered (no IntegrityError to swallow without the index), and the duplicate row would have been written, breaking worker idempotency. Customer-impact-wise nothing happened, but it was not by design.
- **Phase D ran inside the same ops container we'd already validated** (rev30 image, post-deploy, audit listener confirmed working), so applying migrations from this container could not introduce a separate version mismatch.

## What didn't go well

- **The gap window was ~14h45m before discovery.** That is a long time to be running with this class of drift. Detection happened only because we chose to run a sanity check; without that choice, the next discovery vector would have been (a) a real worker-reject collision causing data corruption, (b) a real NULL-hash audit write that subsequent NOT-NULL-aware code would reject, or (c) the next routine close-out. Of those, (a) and (b) are the bad outcomes; only (c) is benign.
- **The session-summary repeated the wrong "head" claim multiple times** without anyone re-validating it against the migration file tree. A summary-of-state document is a working aid, not an authority — but it was treated as authority for Track 3 planning. This is the same class of drift as the canonical recap omitting Step 29.x/29.y (token `D-canonical-recap-v3.4-omits-step-29x-29y-2026-05-07`).
- **The Track 2 functional verification (audit row 3448) was insensitive to this drift.** Writing through the listener populates both columns, so the missing NOT NULL constraint cannot cause a write with both values supplied to fail. A more adversarial verification probe would have attempted a NULL-hash insert and asserted it fails; that would have surfaced the drift at the moment Track 2 declared itself complete, not during Phase D minutes later.

## Recovery

Already complete as of 14:20 EDT.

1. `alembic upgrade head` applied both migrations in transactional DDL. Atomic per migration.
2. Constraint and index both verified in `information_schema` and `pg_indexes`.
3. Hash chain integrity preserved end-to-end: row 3444's hash chains to 3445's backfilled hash, which chains to 3446 (meta-audit), 3447 (id=3 deactivation), 3448 (C25 verification). All `prev_row_hash` columns match the preceding row's `row_hash`.
4. No data was modified in either migration: NOT NULL was applied successfully because all existing rows already had non-NULL values (row 3445 had been backfilled before this point); the partial unique index was applied successfully because no duplicate triple-key existed in the qualifying row set.
5. No customer-facing alert was raised during the window. No support tickets reference behavior consistent with this drift.

## Lessons & action items

- **L1: alembic upgrade head against prod RDS is mandatory after every backend image deploy, immediately after rolling deploy completes, before declaring deploy successful — even when the change set appears code-only.** This makes the ordering invariant impossible to skip, and the check is cheap (idempotent no-op when at head, sub-second). Lands today via runbook update in C28 and `docs/runbooks/PROD_ACCESS.md` to be authored in F.2 of the close-out.
- **L2: Add an automated startup-time health-check that compares the image's `alembic heads` against `SELECT version_num FROM alembic_version`, and either refuses to serve traffic or emits a CloudWatch alarm on mismatch.** This is RC-3's structural fix. Deferred to Step 30 (requires implementation work + alarm wiring). Tracked as new drift token `D-no-alembic-head-vs-rds-startup-check-2026-05-08`.
- **L3: Verification probes used to declare a deploy successful must include at least one adversarial assertion** (a write that should fail, asserting that it does fail). The Track 2 audit row 3448 probe was a happy-path proof; the missing-NOT-NULL drift was invisible to it. Step 30 verification harness review should add adversarial cases for each schema invariant the application depends on. Tracked as deferred under existing meta backlog.
- **L4: Working session-summaries are not authority.** Plans derived from them must re-validate any factual claim against the actual repo or actual prod state before relying on it for irreversible decisions. The "expected no-op" annotation should always be treated as a hypothesis to test, never as a permission to skip. (Process note, no token; this is the same lesson as RC-2.)

## References

- **Drift tokens (this incident):** `D-prod-rds-two-migrations-behind-deployed-code-2026-05-08`, `D-no-alembic-head-vs-rds-startup-check-2026-05-08` (deferred Step 30).
- **Drift tokens (related, prior closed):** `D-cluster4-e2-rework-as-forward-only-2026-05-07` (C12), `D-audit-verification-harness-retry-duplicates-2026-05-07` (C11+C12), `D-audit-row-3445-hash-backfilled-2026-05-08` (C25), `D-audit-chain-listener-only-in-app-main-2026-05-08` (C25).
- **Migration files:** `alembic/versions/c5d8a1e7b3f9_step29y_cluster3_audit_row_hash_not_null.py`, `alembic/versions/d8e2c4b1a0f3_step29y_cluster4_worker_rejection_idempotency.py`.
- **Migration commits:** `d413a0b` (Cluster 3 author), `ed0e6eb` (Cluster 4a author), `e650e66` (Cluster 4 forward-only redesign / C12).
- **Application code that depended on the schema:** `app/repositories/admin_audit_repository.py:246` (`skip_on_conflict=True` swallow), `app/repositories/audit_chain.py::_populate_chain_for_pending` (NOT NULL guarantee on flush).
- **DB rows referenced:** `admin_audit_logs.id` 3444, 3445, 3446, 3447, 3448.
- **Verification SQL** (reproducible):
    - Constraint check: `SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name = 'admin_audit_logs' AND column_name IN ('row_hash', 'prev_row_hash');`
    - Index check: `SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'admin_audit_logs' AND indexname LIKE '%worker_reject%';`
    - Head check: `SELECT version_num FROM alembic_version;` plus `alembic current` from `/app`.
    - Chain walk: `SELECT id, row_hash, prev_row_hash FROM admin_audit_logs WHERE id BETWEEN 3444 AND 3448 ORDER BY id;`
- **Ops task used for verification and recovery:** `c91d44f735204207b777100ffc958192` at `10.0.11.199`, td `luciel-prod-ops:3` running rev30 image.
