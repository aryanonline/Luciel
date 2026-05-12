# Step 24.5c + Step 31 — Production deploy (schema-first, then code)

**Scope:** Close `D-step-24-5c-and-31-schema-and-code-undeployed-to-prod-2026-05-12` by
landing both pieces of currently-undeployed work onto production in the
canonical schema-first ordering:

1. The Alembic migration `3dbbc70d0105` (Step 24.5c — `conversations`
   table, `identity_claim_type` enum, `identity_claims` table, nullable
   `sessions.conversation_id` FK, two partial indexes filtered on
   `active=true`) advances production RDS from presumed revision
   `a7c1f4e92b85` to head `3dbbc70d0105`.
2. The application image built from `main` HEAD `7828a42` rolls onto
   `luciel-backend-service` and `luciel-worker-service` as new task
   definition revisions `:40` and `:20`, carrying the Step 24.5c code
   path (`SessionService.create_session_with_identity`,
   `CrossSessionRetriever`, `IdentityResolver`, adapter claims wiring,
   `app/api/v1/chat_widget.py` widget audit log emissions) and the
   Step 31 read surface (`DashboardService`, `app/api/v1/dashboard.py`
   three GET endpoints).

**Pre-deploy state (presumed, verified at Step 1):**

- Code on `main` HEAD: `7828a42` (drift entry commit). The
  code-complete commit closing Step 31's read surface is `fa876ce`
  (closing tag `step-31-dashboards-validation-gate-complete`); the
  drift entry sits one commit forward at `7828a42` and is docs-only,
  so either commit is a valid deploy target — we use `7828a42` so the
  deployed image and the audit trail share a SHA.
- Last production-deployed application image: `main` HEAD `84339a3`,
  ECR digest
  `sha256:f0bf303272fb0801eefc4cf0d20d2ddb624f2a5f60c8e845cbe422869739f863`,
  pinned in `td-backend-rev39.json` and `td-worker-rev19.json` (rolled
  2026-05-11 via Step 30c).
- Presumed production RDS Alembic revision: `a7c1f4e92b85`. This is
  the revision the Step 30c-deployed image (`84339a3`) was built
  against — Step 30c did not touch `alembic/versions/`, so prod RDS
  has not advanced since.
- ECR repo:
  `729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend`
- Cluster: `luciel-cluster`. Services: `luciel-backend-service`,
  `luciel-worker-service`. Region: `ca-central-1`.

**Default-safety claim (why schema-first is mandatory, not a courtesy):**

The Step 24.5c migration is **additive only** — one Postgres enum
created, two tables created, one nullable column added on `sessions`,
two partial indexes added. No destructive operations, no column type
changes, no NOT NULL backfills. Existing `sessions` rows are
preserved with `conversation_id IS NULL`, which is the Step 24.5c
design contract per ARCHITECTURE §3.2.11 (the cross-session retriever
treats `conversation_id IS NULL` as "single-channel session, no
conversation continuity claimed"). Inside Alembic's PostgreSQL
transactional DDL, any failed step rolls back to the pre-migration
state cleanly.

**Why schema *must* go first, not in parallel or after:** the new
application code at `7828a42` executes
`SessionService.create_session_with_identity()` on **every** widget
chat turn (the route wiring landed in PR #30, `5723b8e`). That call
inserts into `identity_claims` and `conversations`. If the new image
rolls before the migration applies, the first widget chat turn after
rollout fails with
`psycopg.errors.UndefinedTable: relation "identity_claims" does not
exist` — the exact failure the local harness caught on 2026-05-12
pre-migration. The migration must complete and be verified before
ECS task-def update.

**Why this isn't a maintenance window:** because the migration is
additive only and the old code path doesn't touch the new tables,
the period **between migration-complete and task-def update**
operates correctly on both code paths — old tasks (still serving
traffic) keep using the pre-24.5c path which doesn't reference the
new tables, new tasks (rolling in) use the post-24.5c path which now
has tables to write into. No write-side or read-side conflict
between code versions exists. ECS rolling deploy with circuit
breaker remains the rollout mechanism; zero expected downtime.

---

## Step 0 — Sanity check from operator workstation

```powershell
cd C:\Users\aryan\Projects\Business\Luciel

# Confirm you are on the right commit
git checkout main
git pull --ff-only origin main
git log --oneline -1
# expect: 7828a42 post-step-31 doc-truthing: open prod-deploy gap drift (#35)

# Confirm the existing closing tags are reachable
git show --no-patch step-31-dashboards-validation-gate-complete | head -3
# expect: tag pointing at fa876ce

# Confirm AWS profile (per operator-patterns.md Hazard 2)
aws configure list-profiles
# expect exactly: default
```

## Step 1 — Verify presumed production state

Three independent checks. All three must match presumption before
any forward action. If any does not match, **stop and revise the
runbook** before continuing.

These checks exist because the Step 24.5c/31 deploy is the first
event after which the new code path and new schema are exercised by
real customer traffic via the production embed key. Verifying the
architectural footing **before** that event is the architectural
discipline §6 commits to — production-observed evidence, not
design-document assertion.

### Step 1a — Verify production ECS task-def revisions

```powershell
aws ecs describe-services `
  --cluster luciel-cluster `
  --services luciel-backend-service luciel-worker-service `
  --region ca-central-1 `
  --query 'services[].{name:serviceName,taskDef:taskDefinition,desired:desiredCount,running:runningCount}' `
  --output table
```

Expect `luciel-backend-service` pinned to `luciel-backend:39` and
`luciel-worker-service` pinned to `luciel-worker:19`, both with
`running == desired`.

### Step 1b — Verify production RDS Alembic revision

The simplest first-witness: run `alembic current` from a one-shot
ECS task that already has `DATABASE_URL` injected via SSM. We don't
introduce a new shell into the VPC; we reuse the existing
`luciel-backend` task family with an override command.

```powershell
$RUN_OUT = aws ecs run-task `
  --cluster luciel-cluster `
  --task-definition luciel-backend:39 `
  --launch-type FARGATE `
  --network-configuration "awsvpcConfiguration={subnets=[<private-subnet-ids>],securityGroups=[<sg-id>],assignPublicIp=DISABLED}" `
  --overrides '{\"containerOverrides\":[{\"name\":\"luciel-backend\",\"command\":[\"alembic\",\"current\"]}]}' `
  --region ca-central-1 `
  --query 'tasks[0].taskArn' --output text
Write-Host "Diagnostic task: $RUN_OUT"
```

Then wait for `STOPPED` and pull the log line out of
`/ecs/luciel-backend`:

```powershell
aws ecs wait tasks-stopped --cluster luciel-cluster --tasks $RUN_OUT --region ca-central-1

# Logs Insights query against /ecs/luciel-backend
# fields @timestamp, @message
# | filter @message like /head/ or @message like /(head)/
# | sort @timestamp desc
# | limit 5
```

Expect a line ending `a7c1f4e92b85 (head)` — the presumed
production revision. If the line says `3dbbc70d0105` instead, the
migration has already been applied (someone ran it out-of-band) and
Step 2 below should be skipped; jump straight to Step 3. If it says
any other revision, stop and reconcile.

> Subnet and SG ids: the same ones used by the running
> `luciel-backend-service`. Pull them from the service description
> output of Step 1a's `aws ecs describe-services` if you need them
> in the override invocation.

### Step 1c — Verify in-region resilience configuration

This step confirms the §3.4 architectural commitment
*"A single-availability-zone failure is recoverable — Database has
hot standby in a second availability zone; application and worker
tiers run across multiple availability zones"* is **actually
configured** in production, not just designed for. This is the
same honesty discipline that opened
`D-step-24-5c-and-31-schema-and-code-undeployed-to-prod-2026-05-12`
— design says it is true; production must observe it as true
before the deploy proceeds.

Multi-AZ is **single-region** by construction: both AZs live in
`ca-central-1` (Montreal). The §3.2 data-residency commitment
("customer data lives only in Canada") is unchanged — Multi-AZ
means we run in two of Montreal's data centers instead of one of
them, not that we leave Canada. This subsection's title is
"in-region resilience" not "Multi-AZ" so the residency framing is
unambiguous in the audit trail.

#### Step 1c.0 — RDS Multi-AZ check

```powershell
aws rds describe-db-instances `
  --region ca-central-1 `
  --query 'DBInstances[?starts_with(DBInstanceIdentifier, `luciel`)==`true`].{id:DBInstanceIdentifier,az:AvailabilityZone,multiAz:MultiAZ,status:DBInstanceStatus}' `
  --output table
```

Expect `multiAz=True`. Record the observed value verbatim in the
runbook execution log.

#### Step 1c.1 — ECS AZ-spread check

```powershell
aws ecs list-tasks `
  --cluster luciel-cluster `
  --service-name luciel-backend-service `
  --region ca-central-1 `
  --query 'taskArns' --output text `
  | ForEach-Object {
      aws ecs describe-tasks --cluster luciel-cluster --tasks $_ `
        --region ca-central-1 `
        --query 'tasks[].{taskArn:taskArn,az:availabilityZone}' `
        --output table
    }
```

Expect at least two distinct `availabilityZone` values across
running tasks (e.g. `ca-central-1a` + `ca-central-1b`). Repeat for
`luciel-worker-service`. Record observed values.

#### Step 1c — Branch logic

**Scenario A (RDS Multi-AZ=True and ECS spans ≥2 AZs):** Record
the observations. `D-prod-multi-az-rds-unverified-2026-05-09`
becomes a side-benefit close at Step 5 doc-truthing (move §3 → §5
with strikethrough per §6 doctrine). Continue to Step 2.

**Scenario B (RDS Multi-AZ=False, or ECS pinned to a single AZ):**
**Pause the Step 24.5c/31 deploy.** Architecture commitment §3.4
is not currently enforced; flipping it without first establishing
the enforcement would carry the same shape of dishonesty this
runbook exists to resolve. Run Step 1c.2 (Multi-AZ flip) below,
then resume Step 2.

*Why pause-and-flip, not parallel-flip:* `aws rds modify-db-instance
--multi-az --apply-immediately` and the Step 2 Alembic migration
both act on the same RDS primary. They are technically independent
— the Multi-AZ flip is AWS control-plane, the migration is DDL on
the data plane — but **observationally entangled** during the ~15-30
minute standby provisioning window. A migration latency spike, an
unexpected reboot triggered by the modify-db-instance apply, or the
§3.5 "database primary failure" recovery path firing mid-migration
would all be near-indistinguishable in the audit trail if we ran
them in parallel. Pause-and-flip costs ~30 minutes of operator
wall-clock once; parallel-flip risks blurring causal attribution
on a load-bearing deploy. The wall-clock cost is the architecturally
honest choice.

#### Step 1c.2 — Multi-AZ flip sub-runbook (conditional on Scenario B)

This sub-runbook is its own discrete piece of operational scope.
It has its own observed-clean stanza, its own drift close, and its
own rollback. Execute only if Step 1c.0 returned `multiAz=False`
for the production RDS instance.

**Default-safety claim:** `aws rds modify-db-instance --multi-az`
is a synchronous-replication enablement. AWS provisions the
standby in a separate AZ within the same `ca-central-1` region
(typical ~15-30 minutes). The primary remains writable throughout;
the application sees zero downtime. The only application-visible
side effect is a one-time storage I/O briefly elevated during
standby seed.

```powershell
# Flip Multi-AZ on the production RDS instance.
# Replace <db-instance-id> with the value observed at Step 1c.0.
aws rds modify-db-instance `
  --db-instance-identifier <db-instance-id> `
  --multi-az `
  --apply-immediately `
  --region ca-central-1 `
  --query 'DBInstance.{id:DBInstanceIdentifier,status:DBInstanceStatus,multiAzPending:PendingModifiedValues.MultiAZ}' `
  --output table
```

Then poll status until the standby is provisioned and `MultiAZ`
reflects `True` on the live instance (not `PendingModifiedValues`):

```powershell
aws rds wait db-instance-available `
  --db-instance-identifier <db-instance-id> `
  --region ca-central-1

# Re-verify
aws rds describe-db-instances `
  --db-instance-identifier <db-instance-id> `
  --region ca-central-1 `
  --query 'DBInstances[0].{id:DBInstanceIdentifier,az:AvailabilityZone,multiAz:MultiAZ,status:DBInstanceStatus,storage:AllocatedStorage}' `
  --output table
```

Expect `multiAz=True`. If ECS was the single-AZ surface (the
rarer case for Fargate, which scheduler-spreads by default), check
the service's `placementStrategy` and confirm it includes a `spread`
strategy across `availabilityZone`; if not, update the service with
`--placement-strategy 'field=attribute:ecs.availability-zone,type=spread'`
before proceeding.

**Observed-clean for Step 1c.2:** one full RDS health-check cycle
passes with `multiAz=True`, one full ECS service describe shows
tasks spanning ≥2 AZs, no `ERROR` or connection-drop spikes in
`/ecs/luciel-backend` during the flip window.

**Doc-truthing for Step 1c.2** (lands in the same Step 5
doc-truthing commit, not its own commit): close
`D-prod-multi-az-rds-unverified-2026-05-09` §3 → §5 with
strikethrough heading per §6 doctrine; one-line resolution noting
the flip timestamp, the observed `multiAz=True` value, and the
two AZ identifiers now spanned.

**Rollback for Step 1c.2:** Multi-AZ deactivation is
`aws rds modify-db-instance --no-multi-az --apply-immediately`,
but should not be needed for an additive enablement that completes
the AWS-managed provisioning cycle cleanly. If a rollback is
required for cost reasons, document the decision explicitly — do
not silent-rollback an architectural commitment.

## Step 2 — Apply Alembic migration on production RDS

Same one-shot pattern. Re-use the currently-deployed image (revision
`:39`, which is at code `84339a3`) — but note: the `:39` image
**does not contain** the `3dbbc70d0105` migration file in its
`alembic/versions/` directory, because that migration was introduced
by commit `1e761a6` *after* `84339a3`. So this step must run the
migration from an image that contains the new migration file.

The cleanest path is to build the new image first (which we need
anyway for Step 3), push it under a Step-24.5c/31 ECR tag, register
the new task-definition revisions (Step 3 below), and then use the
**newly registered** task-def revision `:40` to run `alembic
upgrade head` as a one-shot — but **without updating the service**.
This keeps the running production tasks on `:39` until the schema
is in place.

So Step 2 and Step 3 interleave: Steps 3.0–3.4 below (build, push,
register) happen first, *then* we come back to Step 2's migration
invocation against the newly-registered `:40` revision, *then*
Step 3.5 updates the running services.

> If this feels awkward, the alternative is to build a separate
> "migration runner" image. We don't, because (1) the new image is
> what we're about to deploy anyway, (2) running migrations from
> the new image proves the image's `alembic/versions/` tree is
> intact, and (3) the task-def revision `:40` defines exactly the
> environment (DATABASE_URL via SSM) the migration needs without
> us re-specifying credentials.

### Step 2 (revisited, after Step 3.0–3.4) — invoke the migration

```powershell
$MIGRATE_TASK = aws ecs run-task `
  --cluster luciel-cluster `
  --task-definition luciel-backend:40 `
  --launch-type FARGATE `
  --network-configuration "awsvpcConfiguration={subnets=[<private-subnet-ids>],securityGroups=[<sg-id>],assignPublicIp=DISABLED}" `
  --overrides '{\"containerOverrides\":[{\"name\":\"luciel-backend\",\"command\":[\"alembic\",\"upgrade\",\"head\"]}]}' `
  --region ca-central-1 `
  --query 'tasks[0].taskArn' --output text
Write-Host "Migration task: $MIGRATE_TASK"

aws ecs wait tasks-stopped --cluster luciel-cluster --tasks $MIGRATE_TASK --region ca-central-1
```

The migration writes these lines to `/ecs/luciel-backend` (verify
via Logs Insights):

```
INFO  [alembic.runtime.migration] Running upgrade a7c1f4e92b85 -> 3dbbc70d0105, step 24.5c: conversations + identity_claims
```

with no `ERROR` / `Traceback` / `CRITICAL` lines following. Then
re-run the Step 1b probe (this time expect `3dbbc70d0105 (head)`)
to independently confirm the upgrade landed.

### Step 2 acceptance criteria

- `alembic current` against prod RDS returns `3dbbc70d0105 (head)`.
- Optional schema-level verification via a scoped `psql` from a
  bastion or another one-shot:
  - `\dt identity_claims` returns 1 row.
  - `\dt conversations` returns 1 row.
  - `\d sessions` shows `conversation_id` column, type `UUID`,
    nullable, FK to `conversations(id)`.
  - `\di+ idx_identity_claims_active_unique` returns 1 row, partial
    index `WHERE active = true`.
  - `\di+ idx_identity_claims_active_lookup` returns 1 row, partial
    index `WHERE active = true`.

**Rollback at this step:** Alembic's PostgreSQL transactional DDL
rolls back automatically on any in-step failure. If the migration
completes but later steps fail and we need to revert schema, the
downgrade target is `a7c1f4e92b85`:

```powershell
# Only if rolling back schema (not normally required)
$DOWNGRADE = aws ecs run-task `
  --cluster luciel-cluster --task-definition luciel-backend:40 `
  --launch-type FARGATE `
  --network-configuration "awsvpcConfiguration={...}" `
  --overrides '{\"containerOverrides\":[{\"name\":\"luciel-backend\",\"command\":[\"alembic\",\"downgrade\",\"a7c1f4e92b85\"]}]}' `
  --region ca-central-1
```

Downgrade is safe because the new tables and column have no rows
yet (no new code has been served traffic between migration and
task-def update).

## Step 3 — Build and roll the new application image

### Step 3.0 — Build the image locally

**Platform pin (load-bearing).** The application tier (§3.2.3) is
"identical, stateless across requests, replaceable." That contract
requires every image in the rolling deploy to be ABI-compatible
with every other — i.e. all tasks run the same CPU architecture.
The registered `td-backend-rev39.json` task-def declares
`runtimePlatform.cpuArchitecture` — confirm it and pin the local
build to match. Building on an operator's host architecture (e.g.
Apple Silicon `arm64`) and pushing to an `amd64` task family is a
classic post-deploy failure mode that the §3.5 "Configuration error
in a deploy" recovery would catch as an elevated error rate — but
catching it pre-push is cheaper.

```powershell
# Confirm the production task arch from the existing manifest.
# Expect linux/amd64 unless the manifest says otherwise.
Get-Content td-backend-rev39.json `
  | Select-String -Pattern 'cpuArchitecture|operatingSystemFamily'
# expect: "cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"

$IMAGE_TAG = "step-24-5c-31-7828a42"
$ECR_REPO  = "729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend"

# Pin the build platform to match the task-def runtimePlatform.
docker buildx build --platform linux/amd64 `
  -t "luciel-backend:$IMAGE_TAG" `
  --load .

docker tag "luciel-backend:$IMAGE_TAG" "$ECR_REPO`:$IMAGE_TAG"

# Sanity-check the built image's reported architecture.
docker image inspect "luciel-backend:$IMAGE_TAG" `
  --format '{{.Architecture}} / {{.Os}}'
# expect: amd64 / linux
```

### Step 3.1 — Auth to ECR and push

```powershell
aws ecr get-login-password --region ca-central-1 `
  | docker login --username AWS --password-stdin $ECR_REPO

docker push "$ECR_REPO`:$IMAGE_TAG"
```

Capture the pushed image digest (task-def update pins by digest):

```powershell
$NEW_DIGEST = (aws ecr describe-images `
  --repository-name luciel-backend `
  --image-ids imageTag=$IMAGE_TAG `
  --region ca-central-1 `
  --query 'imageDetails[0].imageDigest' --output text)

Write-Host "New digest: $NEW_DIGEST"
# expect a sha256:... string, distinct from the Step 30c digest f0bf3032...
```

### Step 3.2 — Rename and update both task-def manifests in the repo

Per Step 30c precedent, the manifests roll their filename revision
in lockstep with the registered task-def revision. Rename
`td-backend-rev39.json` → `td-backend-rev40.json` and
`td-worker-rev19.json` → `td-worker-rev20.json`, then update the
digest inside each.

```powershell
git mv td-backend-rev39.json td-backend-rev40.json
git mv td-worker-rev19.json  td-worker-rev20.json

$OLD_DIGEST = "sha256:f0bf303272fb0801eefc4cf0d20d2ddb624f2a5f60c8e845cbe422869739f863"

(Get-Content td-backend-rev40.json) -replace [regex]::Escape($OLD_DIGEST), $NEW_DIGEST `
  | Set-Content td-backend-rev40.json

(Get-Content td-worker-rev20.json)  -replace [regex]::Escape($OLD_DIGEST), $NEW_DIGEST `
  | Set-Content td-worker-rev20.json

git diff --stat
# expect 2 renames + 2 single-line edits
```

### Step 3.3 — Register the new task definitions

```powershell
$BACKEND_TD = (aws ecs register-task-definition `
  --cli-input-json file://td-backend-rev40.json `
  --region ca-central-1 `
  --query 'taskDefinition.taskDefinitionArn' --output text)

$WORKER_TD = (aws ecs register-task-definition `
  --cli-input-json file://td-worker-rev20.json `
  --region ca-central-1 `
  --query 'taskDefinition.taskDefinitionArn' --output text)

Write-Host "Backend TD: $BACKEND_TD"
Write-Host "Worker  TD: $WORKER_TD"
# expect ARNs ending :40 (backend) and :20 (worker)
```

### Step 3.4 — Pause and execute Step 2 (Alembic migration)

Now that `:40` exists as a registered task-def, jump back up to
**Step 2 (revisited)** and run the `alembic upgrade head` one-shot.
Confirm Step 2's acceptance criteria. Then return here.

### Step 3.5 — Roll the services forward

```powershell
aws ecs update-service `
  --cluster luciel-cluster `
  --service luciel-backend-service `
  --task-definition $BACKEND_TD `
  --region ca-central-1 `
  --query 'service.{taskDefinition:taskDefinition,desiredCount:desiredCount}' `
  --output table

aws ecs update-service `
  --cluster luciel-cluster `
  --service luciel-worker-service `
  --task-definition $WORKER_TD `
  --region ca-central-1 `
  --query 'service.{taskDefinition:taskDefinition,desiredCount:desiredCount}' `
  --output table
```

### Step 3.6 — Watch the rollouts converge

```powershell
# Backend
aws ecs describe-services `
  --cluster luciel-cluster --services luciel-backend-service `
  --region ca-central-1 `
  --query 'services[0].deployments[].{status:status,taskDef:taskDefinition,desired:desiredCount,running:runningCount,pending:pendingCount}' `
  --output table

# Worker
aws ecs describe-services `
  --cluster luciel-cluster --services luciel-worker-service `
  --region ca-central-1 `
  --query 'services[0].deployments[].{status:status,taskDef:taskDefinition,desired:desiredCount,running:runningCount,pending:pendingCount}' `
  --output table
```

Re-run every 30s until each service has exactly one `PRIMARY`
deployment, no `ACTIVE` deployments draining, and `running ==
desired`.

**On circuit-breaker firing:** ECS's deployment circuit breaker is
the §3.5 *"Configuration error in a deploy"* recovery mechanism
doing its job — observe the auto-rollback to `:39` / `:19`
complete, do **not** manually intervene unless the circuit breaker
itself misbehaves. Once rollback steady-state is reached, capture
the CloudWatch logs that triggered the breaker, open a drift entry
for the failure mode, and treat the deploy as paused (not failed —
paused) pending root-cause. The architecture's failure model
working as designed is a *successful* outcome of the deploy
attempt, not a stop signal.

## Step 4 — Observed-clean verification (five-pillar shape)

The Step 31 row in CANONICAL_RECAP §12 commits to a five-pillar
pre-launch validation gate as the visible promise — isolation,
customer journey, memory quality, operations, compliance. The
harness pins those pillars at the local-Postgres level (run stamp
`20260512-144847-068362`, 40/40 claims green). This deploy's
first-prod-observation owes the same shape, scoped to what is
actually demonstrable in production without seeding multi-tenant
fixtures into the live RDS.

Each pillar's prod probe is the **production-observed evidence**
for the architectural commitment it carries. Steps 4a-4b verify
the deploy-mechanical clean-startup contract (§3.2.3 / §3.2.4);
Steps 4c-4g verify the five pillars 1:1.

### Step 4a — Backend startup log review

Logs Insights against `/ecs/luciel-backend`:

```
fields @timestamp, @message
| filter @message like /Application startup complete|Started server process|GET \/health/
| sort @timestamp asc
| limit 50
```

Expect on each new backend task:
- `Started server process [1]`
- `Application startup complete.`
- Steady `GET /health → 200 OK`

### Step 4b — Worker startup log review

Logs Insights against `/ecs/luciel-worker`:

```
fields @timestamp, @message
| filter @message like /celery@|ready\.|Warm shutdown/
| sort @timestamp asc
| limit 50
```

Expect on each new worker task `celery@... ready.` and on each old
worker task `Warm shutdown` completing cleanly.

Across the full post-deploy window: **zero** `ERROR`, `Traceback`,
or `CRITICAL` hits in either log group.

#### Step 4b.1 — SQS broker verification (§3.2.4)

The ARCHITECTURE §3.6 production diagram labels the queue node
`Redis/SQS` and the §3.2.4 prose pins it down: **SQS in production,
Redis in development.** §3.2.4 also makes the architectural claim
load-bearing — "the queue is what makes worker failures invisible
to the customer" depends on SQS actually being the active broker.
A worker that started cleanly (Step 4b) but is silently pointed at
the wrong broker or a wrong queue ARN would pass Step 4b and still
leave §3.2.4 false in production. This sub-step verifies the broker
identity directly.

List the production worker queue and confirm it is reachable in
`ca-central-1`:

```powershell
aws sqs list-queues --region ca-central-1 `
  --profile default
# expect: at least one queue URL ending in the documented
# worker-queue name; capture the URL into $WORKER_QUEUE_URL

aws sqs get-queue-attributes `
  --queue-url $WORKER_QUEUE_URL `
  --attribute-names QueueArn ApproximateNumberOfMessages `
      ApproximateNumberOfMessagesNotVisible CreatedTimestamp `
  --region ca-central-1 --profile default
# expect: QueueArn returns; ApproximateNumberOfMessages is a
# concrete integer (queue is reachable, not 404).
```

Grep `/ecs/luciel-worker` for the kombu/SQS poller startup line that
proves the worker process actually attached to SQS and not a
fallback Redis broker:

```
fields @timestamp, @message
| filter @message like /kombu.transport.SQS|sqs:\/\/|broker_url.*sqs/
| sort @timestamp asc
| limit 20
```

Expect at least one match per new worker task showing the SQS
transport in the broker URL. If the matches are zero, or any line
shows `redis://` as the broker URL in production logs, the deploy
has regressed §3.2.4 even if every other pillar is green — pause,
open a drift token, do not advance to Step 4c.

### Step 4c — Customer journey pillar (§3.3 steps 4 + 9)

The customer-journey pillar is the architectural claim that a
widget turn flows end-to-end through scope check (§3.3 step 4),
session resolution against the new identity primitives, audit
emission (§3.3 step 9), and lands the documented row writes.

The staging embed key `luc_sk_xwij1...` (id 698 on
`domain_configs.id=374`, `tenant=luciel-staging-widget-test`,
`domain=cloudfront-staging`) is the customer-shaped surface to
exercise. Hit `POST /api/v1/chat/widget` for one full turn — either
through the staging widget at
`https://d1t84i96t71fsi.cloudfront.net/staging-widget-test.html`
or via direct API call carrying the embed key in the documented
header.

Expected log emissions on `/ecs/luciel-backend` for that one turn
(the Step 31 sub-branch 1 widget audit log emissions, landed in
`5723b8e`):

```
fields @timestamp, @message
| filter @message like /widget_chat_turn_received|widget_chat_session_resolved|widget_chat_turn_completed/
| sort @timestamp asc
| limit 20
```

All three lines should appear, in order, with consistent
correlation id.

Database-level verification (scoped read from a one-shot or
bastion `psql`, scope bound to tenant
`luciel-staging-widget-test`):

```sql
SELECT count(*) FROM identity_claims WHERE active = true;
-- expect ≥ 1 (the adapter claim from the turn above)

SELECT count(*) FROM conversations;
-- expect ≥ 1 (the conversation created during the turn above)

SELECT id, conversation_id, channel
  FROM sessions
  WHERE conversation_id IS NOT NULL
  ORDER BY created_at DESC LIMIT 1;
-- expect 1 row, conversation_id populated.
```

### Step 4d — Isolation pillar (§4.7 three-layer scope enforcement)

The isolation pillar is the architectural claim that the caller's
resolved scope is the **upper bound** of what a method can read,
not a hint. The production-observable probe is a negative-case
read: confirm that an admin key scoped to a synthetic *probe*
tenant cannot reach the staging tenant's data.

**One-time probe-tenant seed** (deactivated immediately after this
step — Pattern E):

```sql
-- Seed a synthetic probe tenant with no data.
INSERT INTO tenants (id, slug, display_name, active, created_at)
  VALUES (gen_random_uuid(), 'prod-probe-isolation', 'Prod probe — isolation', true, NOW())
  RETURNING id;
```

Mint a tenant-admin key against the probe tenant via
`scripts/mint_embed_key.py` or the admin endpoint, then exercise
`GET /api/v1/dashboard/tenant` with that key:

- Expect: 200 OK with zero turns, zero conversations, zero
  identity claims (because the probe tenant has none).
- Crucially: confirm the response body does **not** contain any
  identifier from the `luciel-staging-widget-test` tenant.

Then attempt to call `GET /api/v1/dashboard/domain/{staging-domain-id}`
with the probe-tenant admin key:

- Expect: 403 (scope policy refusal) — not 200 with empty results,
  not 404. The §4.7 promise is that the boundary itself is
  enforced, not that empty results happen to come back.

Deactivate the probe tenant immediately (Pattern E):

```sql
UPDATE tenants SET active = false
  WHERE slug = 'prod-probe-isolation';
UPDATE api_keys SET is_active = false
  WHERE tenant_id = (SELECT id FROM tenants WHERE slug = 'prod-probe-isolation');
```

The deactivated probe tenant remains in the audit chain per §3.2.7;
it is not deleted.

### Step 4e — Memory quality pillar (§3.3 step 5 cross-session retriever)

The memory quality pillar is the **load-bearing Q8 demonstration**
— the architectural claim that, given a `(User, conversation_id)`
pair, the `CrossSessionRetriever` surfaces sibling-session messages
within the same scope. This is the read-side of Step 24.5c. The
write-side was verified at Step 4c; this step verifies the read-side.

Fire **two turns** against `POST /api/v1/chat/widget` carrying the
same embed key, but with the second turn declaring a different
channel identifier than the first (the adapter-asserted claim from
§3.2.11 — the production embed key today asserts via `channel='web'`;
for the second turn, use the programmatic-API embed-key equivalent
or — if not yet provisioned — capture this gap as a follow-up drift
and exercise the cross-session retriever via two `channel='web'`
turns under the same identity claim):

```
fields @timestamp, @message
| filter @message like /cross_session_retrieval|conversation_id/
| sort @timestamp asc
| limit 20
```

**Expected:** the second turn's `widget_chat_session_resolved`
log line carries the **same** `conversation_id` as the first turn,
and the cross-session retriever log line (or trace span) emits
at least 1 sibling-session passage with provenance pointing at the
first turn's session id.

If the second turn resolves to a different `conversation_id`, the
cross-channel identity resolver is not behaving as designed and the
deploy should be rolled back before further verification.

*Truth-in-labeling note for the Q8 claim.* This probe verifies the
**within-channel** half of Recap §11 Q8 "picks up where the
conversation left off" — two widget turns under the same identity
claim, same `conversation_id`, sibling-session retrieval with
correct provenance. The **cross-channel** half (widget Monday →
phone Wednesday recognised as same `User`) is structurally supported
by §3.2.11's three primitives (`conversations`, `identity_claims`,
`CrossSessionRetriever`) and proven at the orthogonal-channel level
by Step 24.5c CLAIM 6 in `tests/e2e/step_24_5c_live_e2e.py`, but is
not reachable end-to-end in prod until the voice/SMS/email channel
adapters land at 📋 Step 34a. The Step 5 doc-truthing stanza should
record this scope explicitly so the close does not over-claim what
prod has observed.

### Step 4f — Operations pillar (§3.2.10 alarm declaration)

The operations pillar deliberately splits **declared** (asserted by
harness at AST level) from **live `OK`-state** (carved out as
`[PROD-PHASE-2B]`). This split must be preserved verbatim in the
closing stanza so the next reader does not infer the live half was
verified.

Record the alarm IDs declared in `cfn/luciel-prod-alarms.yaml`:

- `luciel-worker-no-heartbeat`
- `luciel-worker-unhealthy-task-count`
- `luciel-worker-error-log-rate`
- `luciel-rds-connection-count`
- `luciel-rds-cpu`
- `luciel-rds-free-storage`
- `luciel-ssm-access-failure`

```powershell
aws cloudwatch describe-alarms `
  --region ca-central-1 `
  --alarm-name-prefix luciel- `
  --query 'MetricAlarms[].{name:AlarmName,state:StateValue,reason:StateReason}' `
  --output table
```

Record which alarms exist in CloudWatch and which are still
undeployed. **Do not** wait for `OK` state on every alarm before
continuing — that is the `[PROD-PHASE-2B]` carve-out per
`D-prod-alarms-deployed-unverified-2026-05-09`. Record the observed
state verbatim in the closing stanza.

### Step 4g — Compliance pillar (§3.2.7 three-channel audit)

The compliance pillar is the architectural claim that the
`admin_audit_logs` hash chain is intact across the deploy. The
deploy itself writes audit rows (task-def registration, service
update, embed-key probe for Step 4d). Verify the chain advances
and verifies cleanly:

```sql
-- Confirm the hash chain advanced across the deploy window.
SELECT count(*) FROM admin_audit_logs
  WHERE created_at >= '<deploy-start UTC>';
-- expect ≥ several rows (probe-tenant create, key mint,
-- deactivation, etc.)

-- Re-run the hash chain verification listener; expect zero
-- chain-break rows.
-- (Pillar 23 verification, per Step 31 compliance pillar)
```

Confirm `deletion_logs` is queryable (no schema regression):

```sql
SELECT count(*) FROM deletion_logs;
-- expect: query returns cleanly (count may be 0; the table
-- must exist).
```

The retention-purge-worker absence (per
`D-retention-purge-worker-missing-2026-05-09`) is an acknowledged
carve-out, preserved verbatim in the closing stanza — not silenced
by this gate.

#### Step 4g.1 — CloudTrail third-channel verification (§3.2.7)

§3.2.7 names **three** independent audit channels — database
(`admin_audit_logs` hash chain), application log stream
(CloudWatch Logs), and AWS CloudTrail — and the rationale only
holds with all three: "if they disagree, that disagreement is
itself the signal." Step 4g verified channel one; Steps 4a/4b/4c
verified channel two implicitly by reading CloudWatch. This
sub-step verifies channel three — that CloudTrail actually captured
the deploy events this runbook just executed (`RegisterTaskDefinition`
for revisions :40 / :20 and `UpdateService` on both services).
Without this check, two of three channels are observed and the
§3.2.7 three-channel claim is unverified for this deploy window.

```powershell
# Capture the deploy window start/end UTC into shell vars first
# (deploy-start = Step 3.3 first register-task-definition call;
#  deploy-end   = Step 3.6 rollouts converged).

aws cloudtrail lookup-events `
  --lookup-attributes AttributeKey=EventName,AttributeValue=RegisterTaskDefinition `
  --start-time $DEPLOY_START_UTC --end-time $DEPLOY_END_UTC `
  --region ca-central-1 --profile default
# expect: at least two events (one for luciel-backend:40, one for
# luciel-worker:20), each carrying the operator's IAM principal.

aws cloudtrail lookup-events `
  --lookup-attributes AttributeKey=EventName,AttributeValue=UpdateService `
  --start-time $DEPLOY_START_UTC --end-time $DEPLOY_END_UTC `
  --region ca-central-1 --profile default
# expect: at least two events (luciel-backend-service,
# luciel-worker-service), each referencing the new task-def ARN.
```

The disagreement test (§3.2.7 rationale): the count of
`RegisterTaskDefinition` + `UpdateService` events in CloudTrail
must be **consistent** with the count of audit rows written to
`admin_audit_logs` by the deploy and with the corresponding
`UpdateService` log lines in CloudWatch. A material disagreement
between the three is the §3.2.7 signal — pause, do not close the
drift, open an incident token.

### Step 4h — Capture run stamp

Capture the UTC timestamp of the first `widget_chat_turn_completed`
line (Step 4c) as the **observed-clean stamp** (format
`YYYYMMDD-HHMMSS-NNNNNN` to match the local harness stamp
convention from Step 31 sub-branch 5).

## Step 5 — Doc-truthing close

All edits land in one commit on a fresh branch off the deploy
commit, then PR, squash-merge.

1. **CANONICAL_RECAP §12 Step 24.5c row** — append:

   > **Prod observed-clean:** First production execution of
   > `SessionService.create_session_with_identity()` against the
   > new `identity_claims` and `conversations` tables observed
   > YYYY-MM-DD on `luciel-backend-service` task-def revision 40
   > and `luciel-worker-service` task-def revision 20, both pinned
   > to ECR digest `<NEW_DIGEST>` built from `main` HEAD `7828a42`
   > on `linux/amd64`. Migration `3dbbc70d0105` applied against
   > production RDS at HH:MM:SS UTC; first
   > `widget_chat_turn_completed` line on `/ecs/luciel-backend` at
   > HH:MM:SS UTC. Cross-session retriever observed surfacing
   > sibling-session passages on the second-turn probe at
   > HH:MM:SS UTC (Step 4e), confirming the §3.3 step 5 design
   > claim end-to-end against live production for the
   > **within-channel** half of §11 Q8; the cross-channel half
   > remains structurally supported by §3.2.11's three primitives
   > and proven at the orthogonal-channel level by Step 24.5c
   > CLAIM 6, with end-to-end prod reachability bound to 📋 Step 34a
   > channel adapters. Production worker broker observed as **SQS**
   > in `ca-central-1` (Step 4b.1) per §3.2.4. No `ERROR` /
   > `Traceback` / `CRITICAL` codes observed in the post-deploy
   > log window. Runbook of record:
   > `docs/runbooks/step-24-5c-and-31-prod-deploy.md`.

2. **CANONICAL_RECAP §12 Step 31 row** — append:

   > **Prod observed-clean (five-pillar shape):** Step 31 widget
   > audit log emissions (`widget_chat_turn_received`,
   > `widget_chat_session_resolved`, `widget_chat_turn_completed`)
   > first observed in `/ecs/luciel-backend` on YYYY-MM-DD via
   > ECS rev 40 (backend) / rev 20 (worker). Five-pillar
   > production observation: **isolation** verified via
   > probe-tenant 403 on cross-tenant dashboard read (Step 4d,
   > probe tenant deactivated per Pattern E in the same window);
   > **customer journey** verified via Step 4c widget-turn probe;
   > **memory quality** verified via Step 4e cross-session
   > retriever probe; **operations** observed at declaration
   > level — live `OK`-state remains `[PROD-PHASE-2B]` per
   > `D-prod-alarms-deployed-unverified-2026-05-09`;
   > **compliance** verified via `admin_audit_logs` hash chain
   > advance across the deploy window plus CloudTrail
   > `RegisterTaskDefinition` and `UpdateService` events captured
   > for both services (Step 4g.1, §3.2.7 three-channel audit
   > observed end-to-end for this deploy), with
   > retention-purge-worker absence preserved as the explicit
   > carve-out per
   > `D-retention-purge-worker-missing-2026-05-09`. The
   > `DashboardService` HTTP surface
   > (`/api/v1/dashboard/{tenant,domain/{id},agent/{id}}`) is
   > live under the same image; embed-key denial of dashboard
   > reads enforced as designed. Runbook of record:
   > `docs/runbooks/step-24-5c-and-31-prod-deploy.md`.

3. **CANONICAL_RECAP §12 Step 24.5c row parenthetical pointer**
   — flip from "**⚠ Prod-deploy gap** (see
   `D-step-24-5c-and-31-schema-and-code-undeployed-to-prod-2026-05-12`)"
   to "(Prod observed clean YYYY-MM-DD; see resolved drift
   `~~D-step-24-5c-and-31-schema-and-code-undeployed-to-prod-2026-05-12~~`)".

4. **CANONICAL_RECAP §12 Step 31 row parenthetical pointer** —
   same flip as item 3.

5. **DRIFTS.md §3 → §5 move** of
   `D-step-24-5c-and-31-schema-and-code-undeployed-to-prod-2026-05-12`,
   verbatim per §6 doctrine, with:
   - heading wrapped in `~~strikethrough~~`
   - `**Status:** RESOLVED on YYYY-MM-DD`
   - `**Closing commit:** <doc-truthing commit SHA on main>`
   - `**Closing tags (re-cut forward):**
     step-24-5c-cross-channel-identity-complete,
     step-31-dashboards-validation-gate-complete`
   - One-line resolution paragraph: "Production RDS advanced from
     `a7c1f4e92b85` to `3dbbc70d0105` via Alembic one-shot on
     newly-registered `luciel-backend:40` at HH:MM:SS UTC;
     application image built from `7828a42` rolled onto
     `luciel-backend-service` and `luciel-worker-service` as task-
     def revisions 40 and 20 at HH:MM:SS UTC; staging-embed-key
     widget chat turn observed clean end-to-end; log groups
     `/ecs/luciel-backend` and `/ecs/luciel-worker` clean for the
     observation window."

6. **Re-cut the two closing tags forward** onto the doc-truthing
   commit so the tags continue to read "code + docs + prod all
   agree here":

   ```powershell
   git tag -fa step-24-5c-cross-channel-identity-complete <doc-truthing SHA> -m "..."
   git tag -fa step-31-dashboards-validation-gate-complete <doc-truthing SHA> -m "..."
   git push --force-with-lease origin step-24-5c-cross-channel-identity-complete
   git push --force-with-lease origin step-31-dashboards-validation-gate-complete
   ```

7. **Commit message:**
   `Step 24.5c + Step 31 prod deploy: observed clean on luciel-backend-service rev 40 / luciel-worker-service rev 20, RDS at 3dbbc70d0105`

## Rollback

If Step 2 (migration) fails: Alembic's transactional DDL
auto-rolls-back. No code rollout has happened. Investigate
migration log, fix, retry.

If Step 3.5 (service update) fails or Step 3.6 shows circuit
breaker firing:

```powershell
aws ecs update-service `
  --cluster luciel-cluster --service luciel-backend-service `
  --task-definition luciel-backend:39 `
  --region ca-central-1

aws ecs update-service `
  --cluster luciel-cluster --service luciel-worker-service `
  --task-definition luciel-worker:19 `
  --region ca-central-1
```

`:39` and `:19` stay pinned to the Step 30c digest. They roll back
in place. The applied migration **stays in place** — it is
additive only and the old code paths do not reference the new
tables, so rolling code back while schema remains forward is a
valid intermediate state (this is the inverse of the failure mode
the schema-first ordering exists to prevent). Schema downgrade
should not be required for an application-layer rollout failure;
only consider it if the rollback investigation surfaces a
schema-correctness problem, which is not the expected failure mode
for an additive-only migration.

## Cross-references

- DRIFTS entry being closed (primary):
  `D-step-24-5c-and-31-schema-and-code-undeployed-to-prod-2026-05-12`
- DRIFTS entry potentially closed as side-benefit (Scenario A) or
  closed by Step 1c.2 sub-runbook (Scenario B):
  `D-prod-multi-az-rds-unverified-2026-05-09`
- DRIFTS entries carried forward as preserved carve-outs in the
  closing stanzas (not closed by this deploy):
  `D-prod-alarms-deployed-unverified-2026-05-09`,
  `D-retention-purge-worker-missing-2026-05-09`
- Closing tags (re-cut forward):
  `step-24-5c-cross-channel-identity-complete`,
  `step-31-dashboards-validation-gate-complete`
- Migration of record:
  `alembic/versions/3dbbc70d0105_step24_5c_conversations_and_identity_claims.py`
- Last prior deploy precedent:
  `docs/runbooks/step-30c-action-classification-deploy.md`
- Service-name and Hazard precedent:
  `docs/runbooks/operator-patterns.md`
- ARCHITECTURE design surfaces this deploy makes live (write-side
  and read-side):
  §3.2.11 Identity & conversation continuity (line 268),
  §3.2.12 Hierarchical dashboards & validation gate (line 292),
  §3.3 step 5 (memory retrieval — cross-session retriever),
  §3.4 row "single-AZ failure is recoverable" (verified at Step 1c)
- Staging E2E precedent for widget surface exercise:
  `docs/runbooks/STEP_30B_STAGING_E2E.md`
