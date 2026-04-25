# Step 27b — Security & Invariant Contract

**Tag:** `step-27-20260429` (target)
**Scope:** Async memory extraction via SQS/Celery on a new `luciel-worker` ECS service.
**Reference commit:** TBD (see commit message at landing).

This document is the immutable contract for the Step 27b async memory extraction
worker. It is the reference for every assertion in Pillar 11, the body of the
27b commit message, the artifact a brokerage security reviewer reads first, and
the checklist for Step 24.5b (which must preserve every row in the invariant
table).

---

## 1. Architecture summary

Web process enqueues; worker process executes. Memory extraction moves off the
chat request path. Target chat p95 latency reduction: 2-4s → <500ms on
memory-heavy turns.

- **Broker:** Redis (SSM `/luciel/production/REDIS_URL`, existing ElastiCache).
  Local dev: `redis://localhost:6379/0`.
- **Result backend:** disabled (`task_ignore_result=True`). No user content in Redis.
- **Queues:** `luciel-memory-tasks` (main), `luciel-memory-dlq` (dead-letter).
- **Deployment:** new ECS Fargate service `luciel-worker`, same image as web,
  entrypoint `celery -A app.worker.celery_app worker --loglevel=info --concurrency=2`.
- **Region:** ca-central-1 (PIPEDA data residency).
- **Sizing:** 0.25 vCPU, **1024 MB** memory.

## 2. Invariant mapping

| # | Invariant | How 27b preserves it |
|---|---|---|
| 1 | Domain-agnostic | Queue names, task names, log formats all product-level. No vertical strings. No `app.domain` imports. |
| 2 | One-way DI | Worker has its own entrypoint. No `request.state` in worker code. |
| 3 | Soft delete | Worker never deletes. Memory writes only. |
| 4 | Audit before commit | Every worker-written `memory_items` row paired with `admin_audit_logs` row in same transaction. Action: `memory_extracted`. |
| 5 | Scope arithmetic only | Worker scope = validated `tenant_id` from payload. No platform_admin bypass in worker. |
| 6 | Read/write rule symmetry | When `luciel_instance_id` set, both message-window read and memory write gated through scope policy at task entry. |
| 7 | No backfill on additive migrations | Migration adds nullable columns + partial unique index. No UPDATE on existing rows. |
| 8 | Defense in depth | Four pre-flight gates: payload shape, key active, tenant match, instance active. Any fail → DLQ + audit row. |
| 9 | Deliberate creation | Worker writes only on explicit enqueue from ChatService. |
| 10 | Chat-key blast radius | Worker inherits enqueuing chat key's binding. |
| 11 | Identity immutability | Worker never updates tenant_id / domain_id / agent_id / luciel_instance_id. |
| 12 | Hand-written migrations | Migration `8e2a1f5b9c4d` hand-written, verified against fresh DB. |
| 13 | Mandatory tenant predicates | Composite unique `(tenant_id, message_id)` on `memory_items`. |

## 3. Business-rule mapping

| Rule | Location | Preserved |
|---|---|---|
| Option B onboarding | `OnboardingService` | Untouched |
| Consent-gated memory (Step 22) | `ChatService` BEFORE `enqueue_extraction` | Preserved |
| Retention (Step 21) | `RetentionPolicy` category=memory | Applies to worker-written rows |
| Audit trail (Step 24.5) | `AdminAuditRepository.record(...)` inside worker task | New; closes pre-existing gap |
| Scope enforcement (Step 24) | Gates 3 & 4 inside worker task | New; worker-side enforcement |
| Pricing | Not a billable unit | Unchanged |
| Domain-agnostic-by-config | No vertical branches in 27b files | Preserved |

## 4. Data-leakage contract

- **In-transit:** Task payload contains only opaque IDs (`session_id`, `user_id`,
  `tenant_id`, `message_id`, `agent_id`, `luciel_instance_id`, `actor_key_prefix`,
  `trace_id`). **No user-generated content.** Worker re-reads messages from
  Postgres inside the task.
- **Broker dwell:** Task sits in Redis ≤30s. Result backend disabled.
- **Logs:** Celery log format omits `%(task_args)s` / `%(task_kwargs)s`.
  Worker logs emit only opaque IDs + `type(exc).__name__`.
- **At-rest in DB:** Memory content lands in `memory_items.content`, same as
  today. Audit row stores SHA256 hash of content, not content itself.
- **Cross-region:** Broker, Worker ECS, RDS — all ca-central-1.
- **IAM least-privilege (worker task role):** SQS receive/delete/send on the
  two worker queues only; SSM GetParameters on `/luciel/production/WORKER_*` only;
  LLM API egress. **No** ALB ingress, RDS admin, or cross-account CloudWatch.
- **DB least-privilege (Postgres role `luciel_worker`, Step 28 follow-up):**
  SELECT/INSERT on `memory_items`, `admin_audit_logs`;
  SELECT on `messages, sessions, users, api_keys, tenants, agents, luciel_instances`;
  **No** access to retention/deletion/consent/knowledge tables.

## 5. Failure modes

| Failure | Action | DLQ? | Retry? | Audit row |
|---|---|---|---|---|
| Malformed payload | Immediate reject | Yes | No | `worker_malformed_payload` |
| API key revoked mid-flight | Immediate reject | Yes | No | `worker_key_revoked` |
| session.tenant_id ≠ payload.tenant_id | Immediate reject | Yes | No | `worker_cross_tenant_reject` |
| LucielInstance deactivated mid-flight | Immediate reject | Yes | No | `worker_instance_deactivated` |
| LLM/embedding transient | Retry with backoff | After 3 | 3× (2s/4s/8s, jittered) | Only on final fail |
| DB transient | Retry with backoff | After 3 | 3× | Only on final fail |
| Duplicate message_id | No-op idempotent success | No | No | No (intentional) |

## 6. Pillar 11 assertion spec

Two execution modes:

- **Mode FULL** (worker + broker reachable): runs all 10 sub-assertions
  (F1–F10). Used in prod gate.
- **Mode DEGRADED** (infra unreachable): runs 4 contract-level checks
  (D1–D4). Both modes return PASS. Mode reported in detail string.

F1–F10 cover: enqueue latency, idempotent upsert, cross-tenant rejection,
malformed-payload rejection, audit-row content hygiene, actor_key_prefix
linkage, queue-depth endpoint, trace_id propagation, worker scope guardrail,
instance-liveness gate.

See `app/verification/tests/pillar_11_async_memory.py` for the executable
contract.

## 7. Rollback contract

- **Feature flag** `MEMORY_EXTRACTION_ASYNC=false` → reverts to sync path
  without redeploy.
- **Worker scale to zero:** `aws ecs update-service --service
  luciel-worker-service --desired-count 0`.
- **SQS queues retained** (cheap, enables re-enable).
- **Web task-def rollback:** `luciel-backend:8` → `:7` via standard
  `update-service --force-new-deployment`. 15-min recovery.
- **Migration rollback:** `8e2a1f5b9c4d` is additive; `git revert` safe;
  `alembic downgrade -1` safe.

## 8. Fail-open behavior

If `luciel-worker-service` is down while `MEMORY_EXTRACTION_ASYNC=true`,
`enqueue_extraction` failures are caught and logged at WARNING level.
Chat requests succeed; memory writes pause until worker recovers.
Queue-depth alarm (Step 28) surfaces the gap.