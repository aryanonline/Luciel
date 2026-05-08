# Luciel — Architecture (Dev + Prod)

**Scope:** Code layout, data model, request flow, async flow, dev environment, AWS topology, deployment flow, verification harness, audit chain.
**Out of scope:** Business value, pricing, roadmap. See `CANONICAL_RECAP.md`. Drifts and resolutions live in `DRIFTS.md`.

**Maintenance protocol:** Surgical edits only. When the topology, schema, or deployment shape changes, update the affected §-section in place. Log prior state in `DRIFTS.md` if the change closes a drift or supersedes a decision.

**Last updated:** 2026-05-08 (Step 29.y close-out)

---

## §1 Scope-Hierarchy Primitive

The platform is built around a single primitive: **every persisted row is scoped to a hierarchy** that is enforced at schema, query, and verification layers.

```
tenant
  └── luciel_instance        (per-tenant deployment instance)
        └── agent            (an AI persona configured for the tenant)
              └── actor      (a human or system using the agent)
                    └── memory_item / event / audit row
```

Enforcement layers:

1. **Schema:** `tenant_id` is `NOT NULL` on every domain table. FKs cascade soft-delete via Pattern E (deactivation), not hard delete.
2. **Query:** Every read and write filters by `tenant_id`. Verified by Pillar 13 (cross-tenant attack-test) and Pillar 11 (per-tenant memory writes).
3. **Verification:** 25-pillar harness asserts isolation, audit, and cascade behavior on every prod-shaped run.

---

## §2 Data Model

### §2.1 Live-aware tables (mutable, scoped, soft-deletable)

| Table | Purpose | Notes |
|-------|---------|-------|
| `tenants` | Tenant registry | `is_active` for soft-delete |
| `luciel_instances` | Per-tenant runtime instance config | FK to tenant |
| `agents` | AI personas configured for a tenant | FK to tenant + instance |
| `actors` | Humans or systems using agents | `permissions JSONB` (Step 30b migration to typed format) |
| `memory_items` | Tenant-scoped memory | `tenant_id NOT NULL`; per-agent FK; **no `domain` column today** (Step 31 Tier 1 gap) |
| `*_configs` | Config tables (auth, channels, integrations, etc.) | All config tables suffix `_configs` |

### §2.2 Append-only tables (no UPDATE, no DELETE)

| Table | Purpose | Notes |
|-------|---------|-------|
| `audit_events` | Three-channel audit row | Hash-chained (`prev_hash`, `current_hash`); no row deletion |
| `verification_runs` | 25-pillar harness results | Full run history; one row per pillar per run |

### §2.3 Memory item detail

`memory_items` is the core retrieval surface:

- `id` (UUID), `tenant_id NOT NULL`, `agent_id`, `actor_id`, `luciel_instance_id`
- `content` (text), `embedding` (pgvector), `metadata` (JSONB)
- `created_at`, `updated_at`, `is_active`
- Indexed on `(tenant_id, agent_id, created_at)` and on `embedding` (HNSW)
- Writes go through Celery worker (async); reads go through backend (sync)

### §2.4 Auth + Audit (three-channel)

Every privileged action writes to **three** channels:

1. **`audit_events` row** — append-only, scoped, queryable
2. **Hash chain** — `current_hash = sha256(prev_hash || row_payload)`; chain validated by Pillar 23
3. **CloudWatch log** — JSON-structured, retained per log-group policy

Audit grants are governed by Pillar 22; chain validity by Pillar 23.

### §2.5 Cascade discipline (Pattern E)

When a tenant is deactivated:

- Application code (not DB FK) walks the scope hierarchy and sets `is_active=false` on each child row.
- No DELETE. No row removal.
- Audit chain stays intact (no audit rows are touched).
- Re-activation is supported by re-flipping `is_active`; the relationship graph is preserved.

---

## §3 Operator Patterns

| Pattern | Meaning | Applied where |
|---------|---------|---------------|
| **E — deactivate, never delete** | All "removals" set `is_active=false` | All tables; cascade walker |
| **N — no-op safety** | Operations that find no work succeed silently | Migrations, cascade, cleanup jobs |
| **O — operator-tagged** | Operator-initiated changes carry an actor tag in audit | All prod-ops actions |
| **S — secrets in SSM** | No secrets in code, env files, or task defs | All credentials, DSNs, API keys |

---

## §4 Code Layout

```
Luciel/
├── pyproject.toml                # build + deps (no setup.py)
├── alembic.ini
├── alembic/
│   └── versions/                 # migrations; latest head must match prod RDS
├── app/
│   ├── main.py                   # FastAPI entrypoint; audit chain listener bound here
│   ├── settings.py               # Pydantic settings (REDIS_URL, DSN, etc., centralized)
│   ├── db/
│   │   ├── models/               # SQLAlchemy models per scoping primitive
│   │   └── session.py            # psycopg v3 driver
│   ├── api/                      # FastAPI routers
│   ├── worker/
│   │   ├── celery_app.py         # Celery app instance
│   │   └── tasks/                # Async memory writes, embeddings, verification dispatch
│   ├── auth/                     # Tenant + agent + actor identity
│   ├── audit/                    # Three-channel audit, hash chain
│   └── verification/
│       ├── runner.py             # Verification harness orchestrator
│       └── tests/
│           ├── pillar_01_*.py
│           ├── ...
│           └── pillar_25_*.py    # 25 pillars total
├── docs/
│   ├── CANONICAL_RECAP.md        # business
│   ├── ARCHITECTURE.md           # this file
│   ├── DRIFTS.md                 # open + resolved
│   ├── runbooks/
│   ├── incidents/
│   ├── verification-reports/
│   └── archive/                  # superseded docs (Step 29.y close-out)
└── infra/
    ├── ecs/                      # task definitions per service
    ├── cfn/                      # CloudFormation: autoscaling, alarms
    └── ssm/                      # SSM parameter naming convention
```

---

## §5 Request Flow (Sync — Backend)

```
client → ALB (api.vantagemind.ai) → ECS service luciel-backend (FastAPI)
   → auth middleware (tenant + agent + actor resolution)
   → router handler
   → DB read (psycopg v3, luciel_worker role for runtime DML)
   → optional Celery enqueue (memory write, embedding, audit)
   → response
   ↓
audit_events row + CloudWatch log line emitted on every privileged op
```

Key invariants:

- Every DB query filters by `tenant_id` resolved from auth context.
- Auth resolution failure returns 401 before any DB read.
- Backend is **stateless**; no in-process queue, no in-process cache that crosses requests.
- Backend currently **runs without autoscaling** (Step 30 — deliberate, ALB-fronted steady-state).

---

## §6 Async Flow (Worker)

```
backend → Celery enqueue (Redis broker) → ECS service luciel-worker
   → Celery task picks up
   → DB write (memory_item, audit, embedding)
   → CloudWatch log
   ↓
DLQ on repeated failure (alarm binding pending — Step 31 Tier 4)
```

Worker autoscaling: **LIVE** since 2026-05-05.

- CFN stack: `luciel-prod-worker-autoscaling`
- Capacity: 1–4
- Target: CPU 60%
- Cooldowns: scale-out 60s, scale-in 300s

---

## §7 Verification Harness (25 Pillars)

Pillars cover scope isolation, auth, audit, cascade, async correctness, performance, and infrastructure. Each pillar emits a row to `verification_runs` per run.

| Pillar | Focus | Notable detail |
|--------|-------|----------------|
| P1–P10 | Identity, schema, FK integrity, soft-delete | Foundation |
| P11 | Async memory write end-to-end | F1 warm-up patch (C32c) ensures steady-state latency, not cold-start, is measured |
| P13 | Cross-tenant attack | A5 attempts a write under tenant A as tenant B; must fail |
| P14 | Verify task on prod-shaped TD | Caught stale-image bug in C31 |
| P22 | Audit grants | Validates `audit_events` insert permissions |
| P23 | Audit hash chain | `prev_hash || payload → current_hash` continuity |
| P24–P25 | Operational invariants | DLQ, autoscaling presence |

Latest run: F.1.b verify task `de3abaabe5534c06a21df87f2f479fc1` — **25/25 FULL** on `luciel-verify:20` (rev32 digest `sha256:22b2a029d4e7b363fb0b71ea91aa2972dd5459dbb767f952eef8e923be280adb`), exitCode 0.

Runner entrypoint: `app/verification/runner.py`. Pillar tests: `app/verification/tests/pillar_*.py`.

---

## §8 Dev Environment

- **OS:** Windows 11; primary shell PowerShell from `C:\Users\aryan\Projects\Business\Luciel`
- **Python:** virtualenv `.venv`, Python 3.x with `pyproject.toml` deps (`pip install -e .`)
- **Local DB:** Docker PostgreSQL with pgvector extension; matches prod schema via Alembic
- **Local Redis:** Docker Redis; broker for Celery worker
- **IDE:** VS Code with Python + Docker extensions
- **Linting/formatting:** project-pinned via `pyproject.toml`
- **Git:** feature branches, detailed commit messages prefixed by step (e.g., `Step 29.y gap-fix C33a: ...`)
- **Push remote:** `https://git-agent-proxy.perplexity.ai/aryanonline/Luciel.git` with `api_credentials=["github"]`

Dev workflow:

1. Create or check out feature branch (e.g., `step-29y-gapfix`)
2. Implement change + alembic migration if schema touches
3. Run pillar tests locally for any pillar the change touches
4. Commit with `Step <N>.<x> <Cn>: <message>`
5. Push; deploy to prod follows the runbook for the relevant step
6. Verify via `luciel-verify:<N>` task on prod-shaped TD

---

## §9 Production AWS Topology

- **Account:** `729005488042`
- **Region:** `ca-central-1` (Canada Central)
- **Domain:** `api.vantagemind.ai`

### §9.1 Network

- VPC with public + private subnets across 2 AZs
- Worker network: subnets `subnet-0e54df62d1a4463bc`, `subnet-0e95d953fd553cbd1`; SG `sg-0f2e317f987925601`; `assignPublicIp` ENABLED
- ALB in public subnets fronting `luciel-backend` service

### §9.2 Compute (ECS)

- **Cluster:** `luciel-cluster`
- **Services:**
  - `luciel-backend` — FastAPI behind ALB; **no autoscaling** (Step 30)
  - `luciel-worker` — Celery worker; **autoscaling LIVE** (1–4, CPU 60%) since 2026-05-05
  - `luciel-prod-ops` — operator runner for SSM/IAM tasks (TD `luciel-prod-ops:3`)
  - `luciel-verify` — verification harness runner (TD `luciel-verify:20`)

Active task definitions (as of Step 29.y):
- `luciel-backend:34`
- `luciel-worker:14`
- `luciel-prod-ops:3`
- `luciel-verify:20`

ECR rev32 image: `sha256:22b2a029d4e7b363fb0b71ea91aa2972dd5459dbb767f952eef8e923be280adb`

### §9.3 Data

- **RDS PostgreSQL** in `ca-central-1`, multi-AZ, in private subnets
  - Roles: `luciel_admin` (DDL/migrations), `luciel_worker` (runtime DML)
  - pgvector extension installed
  - **No alembic-head-vs-RDS startup check** (open drift — Step 30 carry-forward)
  - **No cross-region replication** (Step 38 cluster 4b carry-forward)
- **Redis** (Celery broker) — `REDIS_URL` centralized in `app/settings.py` (closed C19/C30)
- **SSM Parameter Store** — all secrets; rotation supported (closed C24)

### §9.4 Identity / IAM

- `luciel-prod-ops` role — operator actions; SSM read; **cannot list/remove SSM tags** (open drift)
- `luciel-worker` role — runtime DML, Celery, CloudWatch logs
- `luciel-backend` role — runtime read + Celery enqueue + CloudWatch logs
- Audit chain listener bound in `app/main.py` (closed C25)

### §9.5 Observability

- CloudWatch log groups per service
- CloudWatch metrics for ECS CPU/memory + RDS + Redis
- **DLQ + 5xx + worker-failure alarms not yet bound** (Step 31 Tier 4)

---

## §10 Deployment Flow

1. Build image locally or via CI from feature branch
2. Push to ECR
3. Register new task definition revision pointing to new image digest (digest, not tag — see C31 closure)
4. Update ECS service `desiredCount` or use service deployment to roll
5. Run verification harness (`luciel-verify:<N>`) on the new image **before** declaring deploy complete
6. Tag git ref (`step-<N>-complete`) only after 25/25 FULL on prod-shaped TD

Migrations:

- Alembic migrations run from `luciel-prod-ops` task with `luciel_admin` role
- Migration must apply cleanly forward; no destructive migrations (Pattern E)
- DISC-2026-003 lesson: forward-only with `created_at >= '2026-05-08 04:00:00+00'` watermark; never delete audit duplicates retroactively

---

## §11 Audit Chain Detail (Pillar 22 + Pillar 23)

**Pillar 22 — Grants:** asserts the `luciel_worker` role can `INSERT` into `audit_events` and `verification_runs`, and **cannot** `DELETE` or `UPDATE` either. The role grants are minimal and explicit.

**Pillar 23 — Hash chain:** for each new `audit_events` row:

```
current_hash = sha256(prev_hash || canonical_json(row_payload))
```

`prev_hash` is the most recent existing row's `current_hash`. P23 walks the chain and asserts continuity; any break is a critical failure.

Chain repair history:

- 2026-05-08: 60 chain links broken by attempted delete of 223 verification audit duplicates; restored from CSV; redesigned forward-only with watermark. See `DRIFTS.md` DISC-2026-003.

---

## §12 Service Boundaries

| Concern | Where it lives |
|---------|----------------|
| Auth resolution | `app/auth/` middleware, runs before any router |
| Tenant scoping | Schema (NOT NULL) + query filters + P13 attack-test |
| Memory writes | Always async via Celery worker |
| Memory reads | Sync via backend |
| Audit emission | Three-channel; emitter in `app/audit/` |
| Secrets | SSM only; loaded via `app/settings.py` at startup |
| Migrations | `alembic/versions/` only; applied via prod-ops task |
| Verification | `app/verification/` only; runs in luciel-verify TD on prod-shaped image |

---

## §13 Known Architectural Gaps (forward references — full detail in DRIFTS.md)

- No alembic-head-vs-RDS startup check (Step 30 carry-forward)
- `luciel-prod-ops` cannot list/remove SSM tags
- Backend service has no autoscaling (deliberate; revisit Step 30)
- Actor permissions stored as untyped JSONB (migration Step 30b)
- `domain` is not a column on `memory_items` (Step 31 Tier 1 product-intent gap)
- Cross-region replication absent (Step 38 carry-forward)
- DLQ + 5xx + worker-failure alarms not yet bound (Step 31 Tier 4)
- Tenant data-deletion request flow not yet documented (Step 31 Tier 5)
