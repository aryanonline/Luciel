# Step 29.y — Deferred Work Register

**Drift token:** `D-cluster-4b-deferral-undocumented-2026-05-07`
**Established by:** Step 29.y gap-fix Commit 6
**Branch of origin:** `step-29y-gapfix` off `step-29y-impl` HEAD `a98525a`

## Why this document exists

Step 29.y closed several findings in the application code, but a sub-cluster of fixes is **prod-side / IaC-side work** that cannot be expressed in code-only commits. Prior to this gap-fix, the deferral was recorded only in a test-file docstring (`tests/api/test_step29y_cluster4_worker_hardening.py:5-7`); the canonical recap was silent on it. That made the deferred items easy to lose between sessions.

This document elevates the deferral into a tracked register. Every item below has:
- The original finding token from `findings_phase1*.md`.
- Why it cannot be resolved in a code-only commit.
- The target step where it WILL be resolved.
- The runbook or change-control artifact (if any) that will own the rollout.

## Deferred — Cluster 4b (worker prod hardening)

Source-of-truth comment: `tests/api/test_step29y_cluster4_worker_hardening.py:5-7`.

### E-6 — CloudWatch alarms for the memory-extraction worker

**Why deferred:** Requires AWS CloudWatch resource creation and alarm-rule wiring. Touches IaC (Terraform / CDK) and the production AWS account. Out of scope for code-only sessions per Step 29.y working doctrine.

**Target step:** Step 30c (post-CORS, post-tag) — first prod hardening commit window after `step-29y-complete` is tagged.

**Rollout artifact:** A new runbook `docs/runbooks/step-30c-worker-cloudwatch-alarms.md` will be created as the first action of Step 30c. The alarms must include a metric filter on the `WORKER_AUDIT_WRITE_FAILED` log marker established by gap-fix C4 (`D-worker-audit-write-failure-not-alerted-2026-05-07`).

### E-12 — `luciel_worker` Postgres role grants

**Why deferred:** Requires a privileged DB connection that runs `GRANT` / `REVOKE` against production. Cannot be expressed as an Alembic migration without making the DSN itself privileged. Sequenced for a separate change-control window.

**Target step:** Step 30c.

**Rollout artifact:** `docs/runbooks/step-30c-luciel-worker-grants.md` (to be authored). The grants narrow the `luciel_worker` role to the minimum DML needed for memory extraction; `admin_audit_logs` writes use the existing `INSERT` grant plus `SELECT` only on the few rows the chain handler probes.

### E-13 — ECS task-definition role separation (`luciel_app` vs `luciel_worker`)

**Why deferred:** Requires updating ECS task definitions in production via `aws ecs register-task-definition` / Terraform plan. Not expressible as code-only.

**Target step:** Step 30c.

**Rollout artifact:** `docs/runbooks/step-30c-ecs-task-def-role-split.md` (to be authored). The split ensures the application container cannot enqueue worker-only side effects and vice versa.

## Deferred — Cluster 7 (unaccounted)

See gap-fix Commit 7 (`D-cluster-7-unaccounted-2026-05-07`). Cluster 7 has no commits, no tests, and no code citations on `step-29y-impl`. The original `findings_phase1*.md` documents that would have defined it are not in the repository (see [`docs/findings/README.md`](./findings/README.md)).

**Disposition:** investigated, no evidence on branch. If a future session recovers what Cluster 7 was meant to address, the recovery commit must reference `D-cluster-7-unaccounted-2026-05-07` and update both this document and the canonical recap.

## Deferred — Format migration (`actor_permissions` storage)

Established as a separate drift in this gap-fix because it falls naturally out of Commit 1's analysis: while Commit 1 changed the *write* format to JSON and added dual-format read, the historical column values on disk remain in the legacy comma form. Rewriting them to JSON requires recomputing `row_hash` for every historical row, which mutates audit history (Pattern E forensics red line) and must therefore happen in a coordinated prod-aligned migration window.

**Drift token:** `D-actor-permissions-storage-format-migration-step-30b-2026-05-07`
**Target step:** Step 30b.
**Rollout artifact:** A new Alembic migration plus a chain-rewrite runbook `docs/runbooks/step-30b-actor-permissions-format-migration.md` (to be authored). The migration must:
1. Take an `EXCLUSIVE` lock on `admin_audit_logs` (or use `pg_advisory_lock(hashtext('admin_audit_logs_chain'))` to coexist with the chain handler).
2. For every row whose `actor_permissions` is in legacy comma form, rewrite the column to JSON form **and** recompute `row_hash` using the same `canonical_row_hash` that the application uses.
3. Verify post-migration that Pillar 23 still passes against every row.

Until this is done, the dual-format read path established in Commit 1 is the production correctness contract. It must not be removed before the migration runs.

## Deferred — `sessions.api_key_id` foreign-key constraint (F-7)

See `docs/findings/phase1f.md`. The runtime guard in `app/api/v1/sessions.py` is the current containment. Schema-level FK is queued for Step 30b.

## Maintenance contract

Every future deferral MUST:
1. Receive a named drift token in the `D-<short-name>-<YYYY-MM-DD>` format.
2. Get an entry here with finding origin, deferral reason, target step, and the rollout artifact that will own it.
3. Be referenced from the canonical recap's deferral section.

Closing a deferral MUST update this document (move to a "Closed" section with the resolution commit hash) AND the canonical recap, in the same commit that closes it.
