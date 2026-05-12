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

Two independent checks. Both must match presumption before any
forward action. If either does not match, **stop and revise the
runbook** before continuing.

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

```powershell
$IMAGE_TAG = "step-24-5c-31-7828a42"
$ECR_REPO  = "729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend"

docker build -t "luciel-backend:$IMAGE_TAG" .
docker tag    "luciel-backend:$IMAGE_TAG" "$ECR_REPO`:$IMAGE_TAG"
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
desired`. Rolling deploy with circuit breaker means a bad image
auto-rolls back; if `ACTIVE` lingers more than a couple of minutes
or the circuit breaker fires, capture CloudWatch logs and **stop**.

## Step 4 — Observed-clean verification

### Step 4a — Log review on backend and worker

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

### Step 4b — Widget chat E2E against the staging embed key

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

### Step 4c — Database-level verification (scoped read)

From a one-shot or bastion `psql`, with the scope bound to
tenant `luciel-staging-widget-test`:

```sql
SELECT count(*) FROM identity_claims WHERE active = true;
-- expect ≥ 1 (the adapter claim from the Step 4b turn)

SELECT count(*) FROM conversations;
-- expect ≥ 1 (the conversation created during Step 4b turn)

SELECT id, conversation_id, channel
  FROM sessions
  WHERE conversation_id IS NOT NULL
  ORDER BY created_at DESC LIMIT 1;
-- expect 1 row, the session created during Step 4b, with
-- conversation_id populated.
```

### Step 4d — Capture run stamp

Capture the UTC timestamp of the first `widget_chat_turn_completed`
line as the **observed-clean stamp** (format
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
   > to ECR digest `<NEW_DIGEST>` built from `main` HEAD `7828a42`.
   > Migration `3dbbc70d0105` applied against production RDS at
   > HH:MM:SS UTC; first `widget_chat_turn_completed` line on
   > `/ecs/luciel-backend` at HH:MM:SS UTC. No `ERROR` /
   > `Traceback` / `CRITICAL` codes observed in the post-deploy
   > log window. Runbook of record:
   > `docs/runbooks/step-24-5c-and-31-prod-deploy.md`.

2. **CANONICAL_RECAP §12 Step 31 row** — append:

   > **Prod observed-clean:** Step 31 widget audit log emissions
   > (`widget_chat_turn_received`, `widget_chat_session_resolved`,
   > `widget_chat_turn_completed`) first observed in
   > `/ecs/luciel-backend` on YYYY-MM-DD via ECS rev 40
   > (backend) / rev 20 (worker). The `DashboardService` HTTP
   > surface (`/admin/dashboards/{tenant,domain,agent}`) is live
   > under the same image; embed-key denial of dashboard reads
   > is enforced as designed (per the Step 31 sub-branch 3
   > contract test). Runbook of record:
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

- DRIFTS entry being closed:
  `D-step-24-5c-and-31-schema-and-code-undeployed-to-prod-2026-05-12`
- Closing tags (re-cut forward):
  `step-24-5c-cross-channel-identity-complete`,
  `step-31-dashboards-validation-gate-complete`
- Migration of record:
  `alembic/versions/3dbbc70d0105_step24_5c_conversations_and_identity_claims.py`
- Last prior deploy precedent:
  `docs/runbooks/step-30c-action-classification-deploy.md`
- Service-name and Hazard precedent:
  `docs/runbooks/operator-patterns.md`
- ARCHITECTURE design surfaces this deploy makes live:
  §3.2.11 Identity & conversation continuity (line 268),
  §3.2.12 Hierarchical dashboards & validation gate (line 292)
- Staging E2E precedent for widget surface exercise:
  `docs/runbooks/STEP_30B_STAGING_E2E.md`
