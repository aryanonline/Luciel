# ARC 12 WU14 — Infrastructure / Deploy Alignment Findings

**Scope.** Audit-only sweep reconciling Arc 12 application work (WU1–8,
EX1–4, all merged on `arc12/tool-registry-sibling-byo`) against the
repo's infrastructure-as-code surface (`cfn/`, `infra/`, `ops/`,
`scripts/`, `td-*.json` task-defs, `.env.example`). No live AWS
resources were provisioned and no deploys were run. Low-risk IaC
edits made in this branch are listed under "Changes made" below;
every topology decision is flagged for founder review, not enacted.

---

## 1. HEADLINE — §4.1 "Subprocess sandbox pool" divergence (FOUNDER REVIEW)

**Architecture v1 §4.1** lists "Subprocess sandbox pool" as part of the
Arc 12 AWS infrastructure surface. **§4.3** describes the cost model
as "a small Fargate task family that scales with BYO webhook traffic."

**Implementation (WU6, `app/tools/byo/sandbox.py`).** The BYO webhook
subprocess runs **IN-CONTAINER inside the existing backend Fargate
task**, spawned per invocation via:

    asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.tools.byo.subprocess_worker", ...
    )

One process per invocation, hard 30s SIGKILL boundary (`wait_for` →
`proc.kill()`), no shared state with the parent worker. **There is
NO separate Fargate task family.**

### Assessment

The in-container approach **satisfies the §3.3.5 envelope as
written**:

| §3.3.5 requirement                          | In-container WU6 implementation                                  | Status |
|---------------------------------------------|------------------------------------------------------------------|--------|
| Subprocess isolation                        | New OS process per call (`create_subprocess_exec`)               | ✅      |
| Hard 30s timeout, kill at boundary          | `wait_for(... 30s)` → `proc.kill()` (SIGKILL)                    | ✅      |
| No shared worker state                      | Stdin/stdout one-shot envelope, no shared memory or file handles | ✅      |
| Input/output schema validation              | Parent validates input pre-spawn, output post-collect            | ✅      |
| Per-endpoint circuit breaker (Redis)        | `CircuitBreaker` w/ `RedisBackend.from_settings()`               | ✅      |
| Restricted egress allowlist (admin-config)  | Parent + child both check `allowed_domains` (TOCTOU defence)     | ✅      |
| Audit row per invocation                    | `tool_execution_log` row carries the full envelope               | ✅      |
| Retry only on transport, never on schema    | `_attempt_single_dispatch` branches on `error_kind`              | ✅      |
| 1 MiB response body cap                     | `_MAX_RESPONSE_BYTES = 1 MiB` in `subprocess_worker.py`          | ✅      |

The security envelope the documents *mandate* is intact. The
divergence is purely **where the subprocess runs**: inside the
backend task vs. inside a sibling Fargate task family.

### Recommendation: **in-container is correct for v1**

Reasons:

1. **Fewer moving parts at v1 traffic levels.** BYO webhook is
   `requires_tier=("pro","enterprise")` AND requires per-instance
   tool authorization + an admin-registered endpoint row. At Arc 12
   ship time there is no measurable BYO call volume — provisioning a
   separate Fargate task family on day one is infrastructure built
   ahead of demand.
2. **The isolation property the docs require is process-level, not
   task-level.** §3.3.5 says "subprocess isolation" and "killed at
   the boundary." Both hold in the current implementation. A
   crashing or hung webhook child cannot corrupt the backend's
   event loop because the parent uses `asyncio.wait_for` →
   `proc.kill()` — the parent's loop is intact regardless of what
   the child does.
3. **No new IAM / network surface.** Egress is enforced in-app via
   the allowlist (see §3 below). The backend task's outbound
   networking already exists.
4. **Migration to a separate family stays cheap.** The split is
   localised in `app/tools/byo/sandbox.py::_spawn_and_collect`
   (the only call site of `create_subprocess_exec`). When BYO
   volume justifies a separate task family — or when an external
   threat-model review demands stronger isolation than a `kill()`-
   ed child — replacing the spawn with a Fargate `run-task`
   invocation is a contained change.

### What this divergence costs

* **Documentation drift.** Architecture §4.1 / §4.3 will mislead a
  reader who counts task families from the doc and finds one
  family short on AWS.
* **Cost-model description is wrong.** §4.3's "small Fargate task
  family that scales with BYO webhook traffic" doesn't exist; the
  BYO cost lives inside the backend task's CPU/memory budget.

### Founder choices

* **Option A (recommended).** Keep in-container; amend Architecture
  §4.1 to read "subprocess sandbox (in-container at v1; separate
  Fargate task family deferred until traffic justifies it)" and
  amend §4.3 to remove the small-Fargate-family line.
* **Option B.** Build a separate `luciel-byo-sandbox` Fargate task
  family, an SQS dispatch queue, a per-invocation `run-task` IAM
  policy, and rewrite `_spawn_and_collect` to enqueue rather than
  spawn. Significantly more infra and a hot-path latency tax
  (`run-task` cold-start ≈ seconds) for v1 traffic levels.

**Pending founder ruling. Do not build B unilaterally.**

### Infra implications of staying in-container

* **Task memory/CPU.** Current backend task: `cpu: 512, memory: 1024`
  (`td-backend-rev78.json`). The BYO subprocess uses a fresh Python
  interpreter (~40–60 MiB resident) plus the 1 MiB response cap. At
  expected v1 concurrency this fits comfortably; the task already
  carries the chat-path LLM and DB connection-pool footprint
  (largest residents). **No memory bump warranted at v1.** Revisit
  if a BYO-heavy admin lands on Enterprise with multiple
  concurrently-active widgets.
* **Outbound networking.** The egress allowlist is enforced
  IN-PROCESS (parent + child both check the resolved hostname
  against the admin-registered `allowed_domains` array). The ECS
  task's security group does **not** restrict egress today — there
  is no VPC-level network-ACL belt-and-suspenders. **Defence is
  application-layer only.** For v1 this matches §3.3.5 as written
  ("restricted egress allowlist by domain registered at tool config
  time"), but a future hardening pass (or a customer security
  review) may want to add a security-group egress rule scoped to a
  per-instance NAT/proxy. **Flag, no change.**

---

## 2. ENV / SSM / CONFIG reconciliation

Enumerated every NEW setting Arc 12 reads. Methodology: `grep -rn
"settings\."` across `app/tools/byo/`, `app/tools/sibling_dispatch.py`,
`app/tools/`, and inspected `app/core/config.py` for additions.

### New settings — exhaustive list

| Symbol                                | Where                                          | Type                | Status |
|---------------------------------------|------------------------------------------------|---------------------|--------|
| `SIBLING_FAN_OUT_BUDGET = 12`         | `app/tools/sibling_dispatch.py:89`             | module constant     | ✅ Constant (correct per WU5 spec) |
| `BYO_HARD_TIMEOUT_SECONDS = 30`       | `app/tools/byo/sandbox.py:89`                  | module constant     | ✅ Constant (§3.3.5 fixed) |
| `_CHILD_REQUEST_TIMEOUT_SECONDS = 25` | `app/tools/byo/sandbox.py:93`                  | module constant     | ✅ Constant (internal) |
| `DEFAULT_RETRY_COUNT = 2`             | `app/tools/byo/sandbox.py:95`                  | module constant     | ✅ Constant (§3.3.5 fixed) |
| `DEFAULT_BACKOFF_INITIAL_SECONDS = 0.5` / `_MAX_SECONDS = 5.0` | `app/tools/byo/sandbox.py:96-97` | module constants    | ✅ Constants (§3.3.5 fixed) |
| `FAILURE_THRESHOLD = 5`               | `app/tools/byo/circuit_breaker.py:88`          | module constant     | ✅ Constant (§3.3.5 fixed) |
| `FAILURE_WINDOW_SECONDS = 60`         | `app/tools/byo/circuit_breaker.py:89`          | module constant     | ✅ Constant (§3.3.5 fixed) |
| `OPEN_DURATION_SECONDS = 60`          | `app/tools/byo/circuit_breaker.py:90`          | module constant     | ✅ Constant (§3.3.5 fixed) |
| `_STATE_TTL_SECONDS = 24*60*60`       | `app/tools/byo/circuit_breaker.py:97`          | module constant     | ✅ Constant (cache hygiene) |
| `_MAX_RESPONSE_BYTES = 1 MiB`         | `app/tools/byo/subprocess_worker.py:61`        | module constant     | ✅ Constant (envelope-internal) |
| `settings.redis_url` (REUSE)          | `app/tools/byo/circuit_breaker.py:179`         | existing SSM-wired  | ✅ Already provisioned (SSM `/luciel/production/REDIS_URL`) |

### Verdict

* **No new env vars or SSM parameters are required for Arc 12.**
  Every Arc 12 knob the documents fix as a constant is a constant in
  code; every operational dependency (Redis) is already in the
  task-def `secrets:` block via the SSM parameter the rate-limit
  middleware and Celery broker already consume.
* **`SIBLING_FAN_OUT_BUDGET` and the cycle-detection state are
  runtime-internal — confirmed.** They do NOT appear in entitlements,
  task-def env, SSM, or any API/UI surface. WU5 spec compliance
  verified.
* **Redis circuit-breaker confirmation.** `CircuitBreaker.from_settings()`
  in `bring_your_own_webhook_tool.py:79` reads `settings.redis_url`
  via `RedisBackend.from_settings()`. The same URL is the rate-limit
  storage URI (`app/middleware/rate_limit.py:147`) and the Celery
  broker fallback. **One Redis, three consumers, zero new resources.**

### `.env.example` update (made)

Pre-WU14 `.env.example` carried stale fields from before the Arc 12
domain/agent excision (`DEFAULT_DOMAIN_ID=core`, `DEFAULT_TENANT_ID=internal`,
`LLM_PROVIDER=stub`, `APP_NAME`, `ENVIRONMENT`, `LOG_LEVEL`) — none of
which exist in `app/core/config.py`. Rewrote `.env.example` to:
1. only list fields the live `Settings` actually reads;
2. add an explicit Arc 12 note that `REDIS_URL` is reused by the BYO
   circuit breaker — so a local dev who turns BYO on knows the
   dependency.

This is a documentation-only file; production reads from SSM via the
task-def `secrets:` block, not from `.env`.

---

## 3. TASK-DEF / CFN consistency

### Backend task-def — latest is `td-backend-rev78.json`

* **Image:** `luciel-backend@sha256:b4c145eb…214c4dff` (Arc-11 era;
  next deploy after Arc 12 image build will bump to a new sha).
* **CPU / memory:** `512 / 1024`. **Not changed for Arc 12.**
  Sufficient for the in-container BYO subprocess (see §1).
* **Env:** carries the operational basics already
  (`AWS_REGION=ca-central-1`, `MEMORY_EXTRACTION_ASYNC=true`,
  marketing URLs, SES transport selector). **No Arc 12 additions
  needed.**
* **Secrets (SSM):** `ANTHROPIC_API_KEY`, `DATABASE_URL`,
  `MAGIC_LINK_SECRET`, `OPENAI_API_KEY`, **`REDIS_URL`**, all
  Stripe Price IDs, `JWT_SIGNING_KEYS_JSON / _ACTIVE_KID / _GRACE_KID`.
  The `REDIS_URL` line is the load-bearing wire for the BYO
  circuit breaker. **Confirmed present.**

### Worker task-def — latest is `td-worker-rev34-arc11.json`

* **Image:** `luciel-backend:main-8789601` (Arc-11 build, shared
  image with the backend).
* **CPU / memory:** `512 / 1024`. Worker does NOT run BYO subprocess
  spawns (BYO dispatch is on the chat-request path, which lives in
  the backend container). No worker-side Arc 12 work was added.
  **No change needed.**
* **Secrets:** carries `REDIS_URL` (for rate-limit storage parity
  and any future Celery work that consumes Redis directly), shared
  Stripe / JWT secrets.

### CFN templates (`cfn/*.yaml`)

Inspected: `luciel-prod-alarms.yaml`, `luciel-prod-worker-autoscaling.yaml`,
`luciel-sandbox-agent-policy.yaml`, `luciel-verify-task-role.yaml`,
`luciel-widget-cdn.yaml`, `knowledge-bucket.yaml`. **No Arc 12 changes
required.** None of the existing CFN stacks own the topology surface
that the §4.1 divergence would touch (i.e. there is no
`luciel-byo-sandbox` stack today, and per the Option A
recommendation, none should be created).

### IAM (`iam/`, `infra/iam/`)

The ops-role policy at
`infra/iam/luciel-ecs-prod-ops-role-permission-policy.json:25` already
includes SSM `GetParameter` access for `/luciel/production/REDIS_URL`.
**No Arc 12 IAM additions.** The backend task role
(`luciel-ecs-web-role`) already has the SSM read for `REDIS_URL` via
the existing `secrets:` declaration on the task-def.

---

## 4. ALEMBIC single-head + EX4 reseal deploy-path note

### Single head — confirmed

Programmatic scan of `alembic/versions/*.py` revealed a SINGLE head:

    arc12_ex4_reseal_audit_chain_drop_agent_domain

(`down_revision: arc12_ex3_drop_scope_assignment_domain`; no successor.)

Arc 12 added 14 migrations, all chained in WU order on top of the
Arc-11 closeout head:

* `arc12_ex2_rls_drop_agent_domain_refs.py`
* `arc12_ex3_drop_*` (8 files, EX3 surface)
* `arc12_wu2_instance_tool_authorizations.py`
* `arc12_wu4_sibling_call_grants.py`
* `arc12_wu6_byo_webhook_and_tool_execution_log.py`
* `arc12_ex4_reseal_audit_chain_drop_agent_domain.py` (current head)

### Deploy-path covered by `alembic upgrade head` — confirmed

The existing deploy scripts (e.g. `scripts/deploy_30a4.ps1` lines
241+) run the migration as an ECS exec into the freshly-deployed
task: `aws ecs execute-command ... --command 'alembic upgrade head'`.
Because Arc 12's migrations chain off the prior head and terminate
in a single new head, the standard `upgrade head` invocation will
apply all 14 Arc 12 migrations in dependency order without script
changes.

### ⚠ EX4 reseal — long-running migration deploy note (FOUNDER REVIEW)

`alembic/versions/arc12_ex4_reseal_audit_chain_drop_agent_domain.py`
performs a row-by-row UPDATE of `admin_audit_logs` inside a SINGLE
transaction holding `pg_advisory_xact_lock(hashtext('admin_audit_logs_chain'))`:

    rows = bind.execute("SELECT ... FROM admin_audit_logs ORDER BY id ASC").mappings().all()
    for r in rows:
        new_hash = _canonical_hash_v2(row_dict, prev_hash)
        bind.execute("UPDATE admin_audit_logs SET row_hash = :rh, prev_row_hash = :ph WHERE id = :id", ...)

For every historical audit row this performs:
1. one SHA-256 of the canonical content;
2. one row UPDATE.

The whole walk is xact-scoped — autocommit per row is **not** an
option here, because the advisory lock and the recomputed-hash
chain MUST be one atomic operation. While the lock is held, every
concurrent runtime audit insert (`_before_flush_handler` in
`app/repositories/audit_chain.py`) blocks.

**Implications for the deploy run-task:**

* On a small `admin_audit_logs` table (≤ a few hundred thousand
  rows): completes in seconds–minutes. The standard `aws ecs
  execute-command ... 'alembic upgrade head'` window is fine.
* On a large table (millions of rows): the per-row Python round-trip
  + SHA-256 + UPDATE will run for several minutes to potentially
  tens of minutes. The runtime audit path BLOCKS for the entire
  duration (no new audit row can commit). `execute-command` will
  NOT time out (it's a long-lived SSM session), but the operator
  must:
  1. **Run during a maintenance window** so the blocked audit
     writers don't queue up against live traffic.
  2. **Confirm `pg_stat_activity` shows the UPDATE progressing**
     rather than blocked on something else.
  3. **NOT cancel the run-task mid-walk** — partial reseal leaves
     the chain in a mixed state (recomputed prefix + v1-hash
     suffix) that Pillar 23's verifier will flag as broken.

**Recommendation.** Before the production deploy:
* Query `SELECT count(*) FROM admin_audit_logs` on prod RDS to size
  the walk. If ≤ ~500k rows, run inside the normal deploy window.
  If larger, schedule explicitly and notify the founder.
* Consider a one-time read-replica disconnect during the walk if
  replication lag is a concern (advisory lock + bulk UPDATE will
  generate proportionally large WAL).

**No code change recommended.** The reseal logic is correct as
written; this is purely a deploy-ops staging note. The migration's
own docstring (lines 79–92) already documents the irreversibility;
the long-running risk surfaces here for the first time in deploy
context.

---

## 5. Changes made on this branch

| File              | Change                                                                                       |
|-------------------|----------------------------------------------------------------------------------------------|
| `.env.example`    | Removed stale Arc-11-and-earlier fields; aligned to the live `Settings`; added Arc 12 Redis note. |
| `arc12_specs/WU14_INFRA_FINDINGS.md` | This document — the WU14 deliverable for founder review.                    |

**Nothing else.** No CFN edits, no task-def edits, no app-code edits,
no IAM edits. Every other reconciliation item was either already
aligned (`REDIS_URL` already wired; single Alembic head already in
order; no new env vars to thread) or is a topology decision deferred
to the founder (the §4.1 divergence, the EX4 maintenance-window
question).

---

## 6. Summary for founder

1. **§4.1 Architecture vs. WU6 implementation diverges on the BYO
   subprocess sandbox topology.** Architecture documents a "small
   Fargate task family that scales with BYO webhook traffic"; WU6
   ships subprocess-isolation IN-CONTAINER inside the backend task.
   The security envelope §3.3.5 mandates is met either way.
   **Recommended: keep in-container at v1, amend Architecture §4.1
   / §4.3 to match.** Pending founder ruling.
2. **No new env vars / SSM params required.** Every Arc 12 setting
   is a runtime constant; the only operational dependency
   (Redis-backed circuit-breaker state) reuses
   `settings.redis_url`, already SSM-wired for both task-defs.
   `SIBLING_FAN_OUT_BUDGET = 12` and cycle-detection state are
   runtime-internal constants — NOT admin-configurable, NOT in
   entitlements, NOT in any UI/API surface. Confirmed.
3. **Task-def memory/CPU bump NOT warranted for the in-container
   subprocess.** Current `512 / 1024` (`td-backend-rev78`) is
   sufficient for v1 BYO concurrency. Flag the outbound networking
   note (egress is enforced application-layer only — no VPC
   security-group restriction). No change.
4. **Alembic single head confirmed:**
   `arc12_ex4_reseal_audit_chain_drop_agent_domain`. The standard
   `alembic upgrade head` deploy command applies all 14 Arc 12
   migrations in order. No deploy-script changes required.
5. **EX4 reseal needs a maintenance-window deploy note.** Single
   xact, advisory-lock-held, row-by-row UPDATE of every
   `admin_audit_logs` row. Run-task does NOT time out (long-lived
   SSM session), but live audit writes block for the duration.
   Recommendation: size the table before the deploy; schedule
   explicitly if it's large. No code change.
6. **`.env.example` cleaned of pre-Arc-12 stale fields** as the only
   low-risk in-scope IaC edit.
