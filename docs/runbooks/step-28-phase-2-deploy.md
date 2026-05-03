# Step 28 Phase 2 — Operational Hardening Deploy Runbook

**Branch:** `step-28-hardening-impl`
**Working repo:** `Luciel-work` (the original `Luciel-original` is the
fallback if Phase 2 ever needs to be reverted wholesale).
**Pre-deploy gate:** `python -m app.verification` → **19/19 green** on
dev (Pillar 19 audit-log API mount included as of Commit 2).
**Post-deploy gate:** `python -m app.verification` → 19/19 green
against prod admin endpoints + smoke checks below.
**Total estimated wall-clock for the prod-touching commits:** 60–90
minutes for the worker DB role swap, 30–45 minutes each for alarms /
auto-scaling / health checks (Commits 4–7).

---

## 0 — Phase 2 commit map

Phase 2 is split across **9 commits**. Code-only commits (1–3, 8, 9)
ship via the normal CI/CD path with no AWS console steps. Prod-
touching commits (4–7) each ship as **code + IaC + this runbook
section** so we can execute them together at the laptop.

| # | Commit | SHA | Touches | Status |
|---|---|---|---|---|
| 1 | Pillar 15 consent route regression guard | (Phase 1 — `bd9446b`) | code | ✅ shipped |
| 2 | Audit-log API mount (`GET /api/v1/admin/audit-log`) | `75f6015` | code | ✅ shipped |
| 2b | Audit-log review fixes (H1/H2/H3, M1/M3, L1/L2) | `bfa2591` | code | ✅ shipped |
| 3 | Pillar 13 A3 sentinel-extractable fix | `56bdab8` | code (test) | ✅ shipped |
| 4 | Worker DB role swap → `luciel_worker` + admin password rotation | (pending, see §4) | RDS users + ECS task-def + SSM | ⏳ |
| 5 | CloudWatch alarms + SNS pipeline | (pending, see §5) | CloudWatch + SNS | ⏳ |
| 6 | ECS auto-scaling target tracking | (pending, see §6) | ECS + Application Auto Scaling | ⏳ |
| 7 | Container-level healthChecks (web + worker) | (pending, see §7) | ECS task-def | ⏳ |
| 8 | Batched retention deletes / anonymizes | `0d75dfe` | code | ✅ shipped |
| 9 | Phase 2 close — recap + this runbook | (this commit) | docs | 🔄 shipping now |

---

## 1 — Code-only commits (already deployable, no console steps)

These ship via the standard ECR push → task-def register → service
update pattern. No new AWS resources, no IAM changes.

### Commits 2 + 2b — Audit-log API mount

**What it does:** mounts `GET /api/v1/admin/audit-log` returning the
`admin_audit_logs` table with PII-redacted `diff` payloads via a
per-resource `_SAFE_DIFF_KEYS` allow-list. Disallowed keys collapse
to `"<redacted>"`. Cross-tenant `?tenant_id=` overrides are platform-
admin only and emit a `logger.warning` on use.

**Smoke test (post-deploy):**

```powershell
# Expect 200 + audit rows scoped to the caller's tenant.
$h = @{ "X-API-Key" = $env:LUCIEL_PLATFORM_ADMIN_KEY }
Invoke-RestMethod -Uri "https://api.vantagemind.ai/api/v1/admin/audit-log?limit=5" `
  -Headers $h | ConvertTo-Json -Depth 6
```

If any row's `diff` payload contains a value that should have been
redacted but isn't, that is a P0 — file as a new drift entry and
patch `_SAFE_DIFF_KEYS` immediately.

### Commit 3 — Pillar 13 A3

**What it does:** rewrites the Pillar 13 A3 setup turn so the
sentinel content is wrapped in extractable user-fact shape (per
`EXTRACTION_PROMPT` rules). A3 lookup is keyed on
`MemoryItem.message_id` (deterministic FK) with 30 s polling and a
soft sentinel-in-content check. Brings `python -m app.verification`
from 17/18 to 19/19 (Pillar 19 audit-log mount also green).

**Smoke test:** running `python -m app.verification` in a dev shell
with `LUCIEL_PLATFORM_ADMIN_KEY` set is the test.

### Commit 8 — Batched retention

**What it does:** retention purges now run in chunks via
`_batched_delete` / `_batched_anonymize` using
`DELETE/UPDATE WHERE id IN (SELECT id ... FOR UPDATE SKIP LOCKED LIMIT n)`,
committing per batch. New `Settings` knobs (`retention_batch_size`,
`retention_batch_sleep_seconds`, `retention_max_batches_per_run`)
control bandwidth.

**Why it matters:** without this, a year-old tenant with millions of
`messages` rows would issue a single unbounded DELETE that holds row
locks across the tenant's entire history, fills the WAL, blocks
autovacuum, and lags read replicas — a real RDS outage class.

**Partial-failure semantics:** if a mid-run batch raises, prior
batches are durable, a `DeletionLog` row is written with
`reason="...| PARTIAL: <ExcType>: <msg>"`, and the original exception
re-raises. Strict PIPEDA improvement over pre-Commit-8 atomic-or-
nothing behavior.

**Tunable defaults (per `app/core/config.py`):**

| Env var | Default | Notes |
|---|---|---|
| `LUCIEL_RETENTION_BATCH_SIZE` | 1000 | Rows per chunk. Tuned for db.t3.medium warm cache. |
| `LUCIEL_RETENTION_BATCH_SLEEP_SECONDS` | 0.05 | Pause between batches; lets autovacuum + replication catch up. |
| `LUCIEL_RETENTION_MAX_BATCHES_PER_RUN` | 10000 | Defense-in-depth ceiling: 10M rows/call max. |

**Smoke test:** trigger a manual purge in dev against a category
with synthetic >batch-size rows and confirm `deletion_logs` shows
`rows_affected == sum(actual deleted)` and the run completes without
holding locks (check via `pg_locks` mid-run).

### Commit 9 — Phase 2 close

This commit. Updates `docs/CANONICAL_RECAP.md` and creates this
runbook. No code changes.

---

## 2 — Prod-touching commits — execution mode

The remaining four commits each touch live AWS infrastructure.
**Aryan executes these at his laptop with the agent guiding** —
the agent does **not** run AWS CLI against prod from its sandbox.

For every prod-touching commit:
1. Read the corresponding section below in full before opening a
   shell.
2. Run the **dry-run / recon** block first.
3. Run the **mutation** block.
4. Run the **verification** block.
5. If anything looks wrong, run the **rollback** block — do not
   improvise.

All AWS CLI examples assume `--region ca-central-1` and
account `729005488042`.

---

## 3 — Pre-flight ritual (every prod-touching session)

Identical to canonical recap §13.3:

```powershell
# Block 1 — AWS identity (expect 729005488042)
aws sts get-caller-identity --query Account --output text

# Block 2 — Git state (expect clean)
git status --short; git log -1 --oneline; git stash list

# Block 3 — Docker
docker info --format "{{.ServerVersion}} {{.OperatingSystem}}"

# Block 4 — Dev admin key (expect True / 50)
$env:LUCIEL_PLATFORM_ADMIN_KEY.StartsWith("luc_sk_"); $env:LUCIEL_PLATFORM_ADMIN_KEY.Length

# Block 5 — Verification (expect 19/19)
python -m app.verification
```

If Block 5 isn't 19/19 green, **diagnose, do not deploy**.

---

## 4 — Commit 4 — Worker DB role swap + admin password rotation

**Goal:** worker process stops authenticating to RDS as
`luciel_admin` (superuser) and starts using `luciel_worker` (least-
privilege role created in Phase 1, drift `D-worker-role`). Then
rotate the `luciel_admin` password (drift
`D-prod-superuser-password-leaked-to-terminal-2026-05-03`).

**Why it matters:** Phase 1 enforces audit-log append-only by API.
This commit enforces it by **DB grant**, so even if a worker bug
attempts `UPDATE admin_audit_logs ...` it fails at Postgres.

**Code/IaC artifacts to land in this commit (Aryan to author or
review on the branch before deploy):**

- `scripts/mint_worker_db_password_ssm.py` — already present (Phase 1
  Commit 8a). Re-verify.
- New runbook block in `docs/runbooks/step-28-commit-8-luciel-worker-sg.md`
  (already present) — re-confirm SG ingress instructions.
- Task-def update for `luciel-worker` to read `WORKER_DATABASE_URL`
  from SSM `/luciel/production/WORKER_DATABASE_URL` instead of
  `DATABASE_URL`.
- `app/core/config.py` already exposes a way for the worker to read
  a different SSM key — verify before deploy.

### 4.1 Recon (read-only, safe)

```powershell
# Confirm luciel_worker role exists and has the expected grants.
# Run via Pattern N one-shot (luciel-migrate:N) — do NOT add temporary
# IAM ingress to RDS for psql from a laptop.
aws ecs run-task --cluster luciel-cluster `
  --task-definition luciel-migrate:N `
  --launch-type FARGATE `
  --network-configuration "awsvpcConfiguration={subnets=[<private-subnet-id>],securityGroups=[<migrate-sg-id>],assignPublicIp=DISABLED}" `
  --overrides '{
    "containerOverrides":[{
      "name":"luciel-migrate",
      "command":["python","-c","from app.core.config import settings; from sqlalchemy import create_engine, text; e=create_engine(settings.database_url); c=e.connect(); print(c.execute(text(\"SELECT rolname, rolsuper, rolcanlogin FROM pg_roles WHERE rolname IN (''luciel_admin'',''luciel_worker'')\")).fetchall())"]
    }]
  }' --region ca-central-1
```

Expected: two rows. `luciel_admin` super=t login=t,
`luciel_worker` super=f login=t.

### 4.2 Mint new worker password and store in SSM

```powershell
# From the working repo, dry-run first.
python scripts/mint_worker_db_password_ssm.py --dry-run

# Real run — writes /luciel/production/WORKER_DATABASE_URL as SecureString
# AND issues ALTER USER luciel_worker WITH PASSWORD '...' on RDS.
python scripts/mint_worker_db_password_ssm.py
```

The mint script self-rotates: it generates a 32-char password, runs
`ALTER USER luciel_worker WITH PASSWORD ...`, builds the SQLAlchemy
URL, and writes the SSM SecureString. It does **not** print the
password anywhere (Pattern E).

### 4.3 Update worker task-def to read WORKER_DATABASE_URL

```powershell
# Pull current task-def
aws ecs describe-task-definition --task-definition luciel-worker `
  --region ca-central-1 --query "taskDefinition" `
  > taskdef-worker-current.json

# Edit taskdef-worker-current.json:
#  - In containerDefinitions[0].secrets, replace the entry where
#    name == "DATABASE_URL" so its valueFrom points to
#    arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/WORKER_DATABASE_URL
#    (keep the env var name DATABASE_URL — Celery code reads that).
#  - Strip read-only fields (taskDefinitionArn, revision, status,
#    requiresAttributes, compatibilities, registeredAt, registeredBy).

aws ecs register-task-definition `
  --cli-input-json file://taskdef-worker-new.json `
  --region ca-central-1 `
  --query "taskDefinition.taskDefinitionArn" --output text
```

### 4.4 Roll the worker service onto the new task-def

```powershell
aws ecs update-service `
  --cluster luciel-cluster `
  --service luciel-worker-service `
  --task-definition luciel-worker:<new-revision> `
  --force-new-deployment `
  --region ca-central-1 `
  --query "service.deployments[0].rolloutState" --output text
```

Watch deployment to `COMPLETED`:

```powershell
aws ecs describe-services --cluster luciel-cluster `
  --services luciel-worker-service --region ca-central-1 `
  --query "services[0].deployments[*].[status,rolloutState,runningCount,desiredCount]" `
  --output table
```

### 4.5 Verify worker is now connecting as luciel_worker

```powershell
# CloudWatch logs for the new task — expect normal Celery boot, no
# permission errors.
aws logs tail /ecs/luciel-worker --since 5m --region ca-central-1

# RDS recon: who's connected?
# Use Pattern N to run:
#   SELECT usename, application_name, count(*) FROM pg_stat_activity
#   GROUP BY 1,2;
# Expect to see luciel_worker rows from the worker tasks.
```

### 4.6 Smoke a write that exercises the role

Trigger a memory-extraction Celery task in prod (pick a low-traffic
luciel_instance) and confirm:
- The task succeeds (worker can INSERT into `memory_items`).
- An `admin_audit_logs` row is written with the worker's
  AuditContext (Pattern: worker key prefix preserved).
- A `psql -U luciel_worker -c "DELETE FROM admin_audit_logs"`
  attempted via Pattern N **fails with permission denied** (the
  whole point of the swap).

### 4.7 Rotate luciel_admin password (drift D-prod-superuser-password)

```powershell
# Generate a new password locally (do NOT echo it).
$newAdminPwd = -join ((33..126) | Get-Random -Count 32 | %{[char]$_})

# Apply via Pattern N one-shot. The mint pattern for luciel_worker
# applies here verbatim — write a temporary script that does
# ALTER USER luciel_admin WITH PASSWORD :pwd from inside the ECS task
# and writes the new SQLAlchemy URL to /luciel/production/DATABASE_URL.
# Reuse mint_worker_db_password_ssm.py as a template; clone it as
# scripts/rotate_admin_db_password_ssm.py for this commit.

python scripts/rotate_admin_db_password_ssm.py --dry-run
python scripts/rotate_admin_db_password_ssm.py
```

Then roll the **web** service to pick up the new `DATABASE_URL`:

```powershell
aws ecs update-service --cluster luciel-cluster `
  --service luciel-backend-service `
  --force-new-deployment --region ca-central-1
```

### 4.8 Rollback (Commit 4)

If anything regresses:

```powershell
# Roll worker service back to the prior task-def revision.
aws ecs update-service --cluster luciel-cluster `
  --service luciel-worker-service `
  --task-definition luciel-worker:<previous-revision> `
  --force-new-deployment --region ca-central-1

# luciel_admin password rotation is NOT rollback-safe (old password
# is destroyed). If the new password is corrupted, mint another via
# the same script — do not try to recover the old one.
```

---

## 5 — Commit 5 — CloudWatch alarms + SNS pipeline

**Goal:** alarm before users notice. Five alarms, one SNS topic
(`luciel-prod-alerts`), email subscription to Aryan.

**Alarms to land:**

| Alarm | Metric | Threshold | Evaluation |
|---|---|---|---|
| `luciel-sqs-backlog-high` | `ApproximateNumberOfMessagesVisible` on `luciel-memory-tasks` | > 50 | 2 of 2 datapoints, 5-min period |
| `luciel-sqs-dlq-nonzero` | `ApproximateNumberOfMessagesVisible` on `luciel-memory-dlq` | > 0 | 1 of 1, 1-min period |
| `luciel-rds-conn-high` | `DatabaseConnections` on `luciel-prod-db` | > 80% of `max_connections` (compute once, hardcode threshold) | 2 of 2, 5-min |
| `luciel-ecs-cpu-high` | `CPUUtilization` per service | > 80% | 2 of 2, 5-min |
| `luciel-alb-5xx-high` | `HTTPCode_Target_5XX_Count` on backend TG | > 1% of request count | 2 of 2, 5-min |

**Code/IaC artifacts to land:**
- `infra/cloudwatch/alarms.yaml` (CloudFormation) — single template
  declaring the SNS topic + 5 alarms with parameters for queue ARNs,
  RDS instance id, ECS service names, ALB target group.
- New runbook section here describing the deploy and the snooze
  protocol (an alarm fires → you snooze for 30 min while debugging,
  not forever).

### 5.1 Recon

```powershell
# Confirm queue ARNs
aws sqs list-queues --queue-name-prefix luciel-memory --region ca-central-1

# Confirm RDS max_connections
aws rds describe-db-parameters `
  --db-parameter-group-name <luciel-prod-db's pg> `
  --query "Parameters[?ParameterName=='max_connections'].ParameterValue" `
  --output text --region ca-central-1
```

### 5.2 Deploy

```powershell
aws cloudformation deploy `
  --template-file infra/cloudwatch/alarms.yaml `
  --stack-name luciel-prod-alarms `
  --parameter-overrides `
    AlertEmail=aryans.www@gmail.com `
    SqsMainQueueName=luciel-memory-tasks `
    SqsDlqQueueName=luciel-memory-dlq `
    RdsInstanceId=luciel-prod-db `
    EcsClusterName=luciel-cluster `
    EcsBackendService=luciel-backend-service `
    EcsWorkerService=luciel-worker-service `
    AlbTargetGroupFullName=<full TG name> `
    RdsConnectionThreshold=<computed 80%> `
  --region ca-central-1
```

Confirm the SNS subscription email and click the AWS confirmation
link.

### 5.3 Verify each alarm fires when expected

For each alarm, manually breach in dev once (e.g. enqueue 60 SQS
messages with no consumer; expect SNS email within 6 minutes). Do
**not** synthetic-breach in prod — use dev/staging, then trust the
template parity.

### 5.4 Rollback (Commit 5)

```powershell
aws cloudformation delete-stack `
  --stack-name luciel-prod-alarms --region ca-central-1
```

Pure-additive commit; rollback is clean.

---

## 6 — Commit 6 — ECS auto-scaling target tracking

**Goal:** web service scales on CPU, worker service scales on SQS
queue depth.

**Targets:**

| Service | Metric | Target | Min | Max |
|---|---|---|---|---|
| `luciel-backend-service` | `ECSServiceAverageCPUUtilization` | 50% | 1 | 4 |
| `luciel-worker-service` | Custom: `ApproximateNumberOfMessagesVisible` / running task count, target = 10 messages/task | 1 | 4 |

**Code/IaC artifacts:**
- `infra/autoscaling/web-cpu.yaml` — `AWS::ApplicationAutoScaling::ScalableTarget`
  + `AWS::ApplicationAutoScaling::ScalingPolicy` (TargetTrackingScaling).
- `infra/autoscaling/worker-queue.yaml` — same pattern, custom
  CloudWatch metric math expression as the scaling metric.

### 6.1 Deploy

```powershell
aws cloudformation deploy `
  --template-file infra/autoscaling/web-cpu.yaml `
  --stack-name luciel-prod-autoscale-web `
  --parameter-overrides ClusterName=luciel-cluster ServiceName=luciel-backend-service `
  --region ca-central-1 --capabilities CAPABILITY_IAM

aws cloudformation deploy `
  --template-file infra/autoscaling/worker-queue.yaml `
  --stack-name luciel-prod-autoscale-worker `
  --parameter-overrides ClusterName=luciel-cluster ServiceName=luciel-worker-service QueueName=luciel-memory-tasks `
  --region ca-central-1 --capabilities CAPABILITY_IAM
```

### 6.2 Verify

```powershell
aws application-autoscaling describe-scalable-targets `
  --service-namespace ecs --region ca-central-1 `
  --query "ScalableTargets[?ResourceId=='service/luciel-cluster/luciel-backend-service']"

aws application-autoscaling describe-scaling-policies `
  --service-namespace ecs --region ca-central-1
```

Then load-test dev (k6 or hey) and confirm the scale-out alarm fires
+ desired count rises.

### 6.3 Rollback (Commit 6)

Delete both stacks. ECS reverts to the static `desiredCount` set on
the service, which is what we ran on through Phase 1.

---

## 7 — Commit 7 — Container-level health checks

**Goal:** ECS replaces a sick task before the ALB target group
notices. Belt-and-suspenders against the existing ALB target health
check.

**Probes:**

| Container | Command | Interval | Timeout | Retries | StartPeriod |
|---|---|---|---|---|---|
| Backend (web) | `curl -fsS http://localhost:8000/health || exit 1` | 30 s | 5 s | 3 | 30 s |
| Worker | `celery -A app.worker.celery_app inspect ping -d celery@$(hostname) \|\| exit 1` | 60 s | 10 s | 3 | 60 s |

**Code/IaC artifacts:**
- Updated task-def JSON for `luciel-backend` and `luciel-worker`,
  adding `containerDefinitions[0].healthCheck`.
- Image must contain `curl` (backend already does — confirm at
  Dockerfile review). Worker image already has Celery CLI.

### 7.1 Deploy

```powershell
# Pull current task-defs
aws ecs describe-task-definition --task-definition luciel-backend `
  --region ca-central-1 --query "taskDefinition" > td-backend.json
aws ecs describe-task-definition --task-definition luciel-worker `
  --region ca-central-1 --query "taskDefinition" > td-worker.json

# Edit each: add the healthCheck block per the table above.
# Strip read-only fields. Register new revisions.
aws ecs register-task-definition --cli-input-json file://td-backend-new.json `
  --region ca-central-1 --query "taskDefinition.revision" --output text
aws ecs register-task-definition --cli-input-json file://td-worker-new.json `
  --region ca-central-1 --query "taskDefinition.revision" --output text

# Roll services
aws ecs update-service --cluster luciel-cluster --service luciel-backend-service `
  --task-definition luciel-backend:<new> --force-new-deployment --region ca-central-1
aws ecs update-service --cluster luciel-cluster --service luciel-worker-service `
  --task-definition luciel-worker:<new> --force-new-deployment --region ca-central-1
```

### 7.2 Verify

```powershell
# Tasks should reach healthStatus: HEALTHY
aws ecs list-tasks --cluster luciel-cluster --service-name luciel-backend-service `
  --region ca-central-1 --query "taskArns[]" --output text |
  ForEach-Object {
    aws ecs describe-tasks --cluster luciel-cluster --tasks $_ `
      --region ca-central-1 `
      --query "tasks[0].[lastStatus,healthStatus]" --output text
  }
```

Also confirm an intentionally broken container (kill the process
inside) gets killed and replaced by ECS.

### 7.3 Rollback (Commit 7)

Roll services back to the previous task-def revision. Health checks
are removed; behavior reverts to ALB-target-group-only (Phase 1
state).

---

## 8 — Post-Phase-2 verification gate

After Commits 4, 5, 6, 7 are all live in prod:

1. `python -m app.verification` against prod admin endpoints — 19/19
   green.
2. `aws cloudwatch describe-alarms --alarm-names <all 5>` — all in
   `OK` state.
3. `aws application-autoscaling describe-scalable-targets ...` —
   both targets registered.
4. `aws ecs describe-services ...` — both services running with
   healthCheck-enabled task-def revisions.
5. `pg_stat_activity` recon — every connection from worker tasks is
   `usename = 'luciel_worker'`. Zero worker connections as
   `luciel_admin`.
6. Manual end-to-end test: send a chat through a luciel_instance,
   confirm:
   - `admin_audit_logs` row written with worker AuditContext.
   - `memory_items` extraction completes.
   - No CloudWatch alarms fired during the run.
7. Update `docs/CANONICAL_RECAP.md` Section 3 with Phase 2 commits +
   prod state, tag the merge commit `step-28-phase-2-complete`.

---

## 9 — Phase-2 close exit criteria

**Phase 2 is complete when ALL of the following hold:**

- [x] Pillar 13 19/19 green on dev (Commit 3 + Commit 2 audit-log
      mount)
- [x] Audit-log API mounted, reviewed, and PII-redacted (Commits 2 +
      2b)
- [x] Retention purges batched (Commit 8)
- [ ] Worker connects to RDS as `luciel_worker`, NOT `luciel_admin`
      (Commit 4)
- [ ] `luciel_admin` password rotated (Commit 4)
- [ ] 5 CloudWatch alarms armed, SNS subscription confirmed
      (Commit 5)
- [ ] ECS auto-scaling live for backend + worker (Commit 6)
- [ ] Container healthChecks live for backend + worker (Commit 7)
- [ ] Prod `python -m app.verification` 19/19 green
- [ ] `docs/CANONICAL_RECAP.md` updated, tag
      `step-28-phase-2-complete` pushed

When all boxes check, Phase 2 closes and the canonical recap moves
on to Phase 3 (hygiene) or Step 30b (chat widget — REMAX trial
unblock), whichever the user prioritizes.
