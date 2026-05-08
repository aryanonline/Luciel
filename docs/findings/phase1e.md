# Phase 1e — Worker hardening

Reconstructed from code citations and resolution commits on `step-29y-impl`. See [`README.md`](./README.md) for methodology.

## E-2 — Worker rejection idempotency (SQS ack-late race)

### Code citations
- `alembic/versions/d8e2c4b1a0f3_step29y_cluster4_worker_rejection_idempotency.py:10` — "findings_phase1e.md E-2 documents an ack-late race on SQS"
- `app/repositories/admin_audit_repository.py:244` — "See findings_phase1e.md E-2 and the migration d8e2c4b1a0f3 docstring."

### Resolution commits (on `step-29y-impl`)
- `ed0e6eb` — Step 29.y Cluster 4a (E-2): partial unique index for worker rejection idempotency
- `056f1d8` — Step 29.y Cluster 4a (E-2): record() honours skip_on_conflict for worker rejection idempotency
- `865690a` — Step 29.y Cluster 4a: behavioral + AST tests for E-2/E-3/E-5

### Reconstructed summary

A worker killed between writing its rejection audit row and acknowledging the SQS message leaves the message visible after the SQS visibility timeout. On redelivery the worker would re-write the same audit row (resource_natural_id is deterministic on session/message ids), creating duplicate rejection-class rows.

The Cluster 4a fix:
- Adds a partial unique index `ux_admin_audit_logs_worker_reject_idem` on `(action, tenant_id, resource_natural_id)` covering only worker-rejection-class actions (migration `d8e2c4b1a0f3`).
- Extends `AdminAuditRepository.record()` with `skip_on_conflict=True` for worker callers — a concurrent INSERT that hits the index raises `IntegrityError`, which is the correct idempotency outcome; the repository swallows it and returns `None`.

## E-3 — Celery `autoretry_for=(Exception,)` masked `Reject`

### Code citations
- `app/worker/tasks/memory_extraction.py:67` — "See findings_phase1e.md E-3"
- `tests/api/test_step29y_cluster4_worker_hardening.py:4` — "E-3 ... fixes documented in findings_phase1e.md"

### Resolution commits (on `step-29y-impl`)
- `9098a5d` — Step 29.y Cluster 4a (E-3): narrow autoretry_for, explicit retry/reject paths
- `6bda31a` — Step 29.y Cluster 4a (E-3): register ACTION_WORKER_PERMANENT_FAILURE

### Reconstructed summary

In some Celery 5.x versions, a task decorated with `autoretry_for=(Exception,)` caught `Reject` itself, producing duplicate rejection audit rows on every retry. The Cluster 4a fix narrows `autoretry_for` to `()`, dispatches retries manually via `self.retry()` only for the `_TRANSIENT_EXC` tuple, and routes permanent exceptions through `_reject_with_audit` to DLQ.

## E-5 — `apply_async` missing trace_id / tenant_id headers

### Code citations
- `tests/api/test_step29y_cluster4_worker_hardening.py:4` — "E-5 fixes documented in findings_phase1e.md"

### Resolution commits (on `step-29y-impl`)
- `0deb0e4` — Step 29.y Cluster 4a (E-5): apply_async with trace_id / tenant_id headers

### Reconstructed summary

Worker enqueues lacked trace_id and tenant_id message headers, breaking cross-process forensic correlation. Cluster 4a propagates both as Celery message headers so every downstream log line, audit row, and DLQ message can be joined by trace.

## E-6, E-12, E-13 — DEFERRED to Step 30c

These three findings target prod-side hardening (CloudWatch alarms, the `luciel_worker` Postgres role grants, and ECS task-definition variables) and cannot be resolved in code-only sessions. The deferral is documented as a named drift in [`docs/STEP_29Y_DEFERRED.md`](../STEP_29Y_DEFERRED.md) (gap-fix Commit 6, `D-cluster-4b-deferral-undocumented-2026-05-07`).

Test docstring `tests/api/test_step29y_cluster4_worker_hardening.py:5-7` records the original deferral; the gap-fix elevates that comment into a tracked drift register entry.
