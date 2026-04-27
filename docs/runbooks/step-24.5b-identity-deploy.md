# Step 24.5b — Durable User Identity Deploy Runbook

**Target tag:** `step-24.5b-20260503`
**Pre-deploy gate:** local `python -m app.verification` → 14/14 green
**Post-deploy gate:** prod `python -m app.verification --mode=full` → 14/14 green
**Estimated wall-clock:** 60–90 minutes
**Built-on:** `step-27-completed-20260426` (commit `6f03e03`)

This runbook deploys Step 24.5b: the durable User identity layer that
resolves Q6 (role changes — promotions / demotions / departures) and
Q5's prerequisite (email-stable User identity for Step 38 bottom-up
tenant merge). Three commits ship as one release:

- `78716fe` feat(24.5b-schema): users + scope_assignments tables, agent.user_id FK (additive)
- `766427c` feat(24.5b-services): identity propagation through services + middleware + chat + memory + Q6 mandatory key rotation
- `314ac46` feat(24.5b-verification): backfill + Pillars 12/13/14 + 14/14 MODE=full gate

Two Alembic migrations land on prod:
- `8e2a1f5b9c4d → 3ad39f9e6b55` users + scope_assignments + agents.user_id FK
- `3ad39f9e6b55 → 4e989b9392c0` memory_items.actor_user_id

Both are additive and nullable on the new identity columns. Per
drift D11 + D20, neither column flips to NOT NULL in this release —
Step 28 hardens public routes first, then re-attempts the flip.

Reference: `docs/recaps/2026-04-27-post-step-24-5b-canonical.md` for
the full drift list (D1-D20) and Step 28 backlog formalization.

---

## Phase 0 — Pre-flight (5 min)

```powershell
# 0.1 Confirm prod ALB health + ACM cert valid
curl.exe -s https://api.vantagemind.ai/health
# Expected: {"status":"ok","service":"Luciel Backend"}

# 0.2 Confirm current ECS task-defs (post-Step-27c-final close)
aws ecs describe-services --cluster luciel-cluster `
  --services luciel-backend-service --region ca-central-1 `
  --query "services.taskDefinition" --output text
# Expected: arn ending in luciel-backend:15

aws ecs describe-services --cluster luciel-cluster `
  --services luciel-worker-service --region ca-central-1 `
  --query "services.taskDefinition" --output text
# Expected: arn ending in luciel-worker:4

# 0.3 Confirm prod Alembic head = 8e2a1f5b9c4d (Step 27 close head)
$overrides = @{
  containerOverrides = @(
    @{ name = "luciel-backend"; command = @("alembic", "current") }
  )
} | ConvertTo-Json -Depth 8 -Compress
[System.IO.File]::WriteAllText(
  (Join-Path (Get-Location) "alembic-current-overrides.json"),
  $overrides, (New-Object System.Text.UTF8Encoding $false)
)

$TASK_ARN = aws ecs run-task `
  --cluster luciel-cluster `
  --task-definition luciel-migrate:9 `
  --launch-type FARGATE `
  --network-configuration "awsvpcConfiguration={subnets=[subnet-0e54df62d1a4463bc,subnet-0e95d953fd553cbd1],securityGroups=[sg-0f2e317f987925601],assignPublicIp=ENABLED}" `
  --overrides file://alembic-current-overrides.json `
  --region ca-central-1 `
  --query "tasks.taskArn" --output text
Remove-Item alembic-current-overrides.json
Start-Sleep -Seconds 90

$TASK_ID = $TASK_ARN.Split('/')[-1]
aws logs get-log-events `
  --log-group-name "/ecs/luciel-backend" `
  --log-stream-name "migrate/luciel-backend/$TASK_ID" `
  --region ca-central-1 --start-from-head `
  --query "events[*].message" --output text
# Expected: line containing "8e2a1f5b9c4d (head)"

# 0.4 Confirm LUCIEL_PLATFORM_ADMIN_KEY resolves from SSM
aws ssm get-parameter `
  --name /luciel/production/platform-admin-key `
  --with-decryption --region ca-central-1 `
  --query "Parameter.Value" --output text | ForEach-Object { $_.Substring(0, 12) }
# Expected: prefix like "luc_sk_..." truncated for safety. Captures
# 12 chars only -- do NOT paste the full key into chat or logs.

# 0.5 Take RDS snapshot (5-8 min, runs in background)
$STAMP = Get-Date -Format "yyyyMMdd-HHmm"
$SNAPSHOT_ID = "luciel-db-pre-step-24-5b-$STAMP"
aws rds create-db-snapshot `
  --db-instance-identifier luciel-db `
  --db-snapshot-identifier $SNAPSHOT_ID `
  --region ca-central-1 `
  --query "DBSnapshot.{id:DBSnapshotIdentifier,status:Status}"
Set-Content -Path step-24-5b-prod-snapshot-id.txt -Value $SNAPSHOT_ID
# Snapshot ID is the rollback boundary. Save it locally.
```

**Gate before Phase 1:** all 5 sub-checks above must return expected
output. If any drift, stop and diagnose before any DDL.

---

## Phase 1 — Alembic migration on prod RDS (5 min)

Two migrations run in sequence. Both are additive (new tables, new
nullable column on agents). No data movement at this phase.

```powershell
# 1.1 Run alembic upgrade head via ECS one-shot task
$migrateOverrides = @{
  containerOverrides = @(
    @{ name = "luciel-backend"; command = @("alembic", "upgrade", "head") }
  )
} | ConvertTo-Json -Depth 8 -Compress
[System.IO.File]::WriteAllText(
  (Join-Path (Get-Location) "alembic-upgrade-overrides.json"),
  $migrateOverrides, (New-Object System.Text.UTF8Encoding $false)
)

$TASK_ARN = aws ecs run-task `
  --cluster luciel-cluster `
  --task-definition luciel-migrate:9 `
  --launch-type FARGATE `
  --network-configuration "awsvpcConfiguration={subnets=[subnet-0e54df62d1a4463bc,subnet-0e95d953fd553cbd1],securityGroups=[sg-0f2e317f987925601],assignPublicIp=ENABLED}" `
  --overrides file://alembic-upgrade-overrides.json `
  --region ca-central-1 `
  --query "tasks.taskArn" --output text
Remove-Item alembic-upgrade-overrides.json
Start-Sleep -Seconds 120

$TASK_ID = $TASK_ARN.Split('/')[-1]
aws logs get-log-events `
  --log-group-name "/ecs/luciel-backend" `
  --log-stream-name "migrate/luciel-backend/$TASK_ID" `
  --region ca-central-1 --start-from-head `
  --query "events[*].message" --output text
# Expected: two "Running upgrade" lines:
#   8e2a1f5b9c4d -> 3ad39f9e6b55, add users scope_assignments and agent user_id fk
#   3ad39f9e6b55 -> 4e989b9392c0, add memory_items actor_user_id

# 1.2 Verify head advanced to 4e989b9392c0
# (Run the alembic-current task again, same shape as Phase 0.3)
# Expected: line containing "4e989b9392c0 (head)"
```

**Gate before Phase 2:** prod Alembic head must show `4e989b9392c0`.
If anything else, **stop** — alembic auto-rolled back inside the
transaction; investigate before retrying. RDS snapshot from Phase 0
is the recovery boundary.

---

## Phase 2 — Backfill via ECS one-shot task (10 min)

Per drift D6 + Option D: Service-layer User binding was deferred to a
one-shot backfill rather than threaded into onboarding/Agent creation.
Phase 2.1 runs `--dry-run` against prod RDS to surface the residual
NULL counts and any orphan classes BEFORE writing. Phase 2.2 runs
live only on operator confirmation of clean dry-run output.

```powershell
# 2.1 Dry-run via ECS one-shot task
$dryRunOverrides = @{
  containerOverrides = @(
    @{
      name    = "luciel-backend"
      command = @("python", "-m", "scripts.backfill_user_id", "--dry-run", "--verbose")
    }
  )
} | ConvertTo-Json -Depth 8 -Compress
[System.IO.File]::WriteAllText(
  (Join-Path (Get-Location) "backfill-dryrun-overrides.json"),
  $dryRunOverrides, (New-Object System.Text.UTF8Encoding $false)
)

$TASK_ARN = aws ecs run-task `
  --cluster luciel-cluster `
  --task-definition luciel-migrate:9 `
  --launch-type FARGATE `
  --network-configuration "awsvpcConfiguration={subnets=[subnet-0e54df62d1a4463bc,subnet-0e95d953fd553cbd1],securityGroups=[sg-0f2e317f987925601],assignPublicIp=ENABLED}" `
  --overrides file://backfill-dryrun-overrides.json `
  --region ca-central-1 `
  --query "tasks.taskArn" --output text
Remove-Item backfill-dryrun-overrides.json
Start-Sleep -Seconds 120

$TASK_ID = $TASK_ARN.Split('/')[-1]
aws logs get-log-events `
  --log-group-name "/ecs/luciel-backend" `
  --log-stream-name "migrate/luciel-backend/$TASK_ID" `
  --region ca-central-1 --start-from-head `
  --query "events[*].message" --output text
# Expected: SUMMARY block with "DRY-RUN: no rows were written".
# Capture: Phase A "seen" + "backfilled" counts (= synthetic Users
# that will be created), and Phase B "orphans" classes (memory rows
# with no Agent binding to walk -- expected on prod since Step 27b
# memory rows pre-date Step 24.5b's column).
```

**Operator gate:** review the dry-run output. Phase B residuals
matching the pattern "orphans (no agent_id)" are expected and
acceptable per drift D11. If Phase A residuals are unexpectedly
high (>50) OR Phase B reports orphan classes other than no_agent_id,
stop and investigate. Phase A residuals on prod are the actual
agents that need backfill — typically a small count from existing
tenants.

```powershell
# 2.2 Live backfill (operator-confirmed clean dry-run)
$liveOverrides = @{
  containerOverrides = @(
    @{
      name    = "luciel-backend"
      command = @("python", "-m", "scripts.backfill_user_id", "--verbose")
    }
  )
} | ConvertTo-Json -Depth 8 -Compress
[System.IO.File]::WriteAllText(
  (Join-Path (Get-Location) "backfill-live-overrides.json"),
  $liveOverrides, (New-Object System.Text.UTF8Encoding $false)
)

$TASK_ARN = aws ecs run-task `
  --cluster luciel-cluster `
  --task-definition luciel-migrate:9 `
  --launch-type FARGATE `
  --network-configuration "awsvpcConfiguration={subnets=[subnet-0e54df62d1a4463bc,subnet-0e95d953fd553cbd1],securityGroups=[sg-0f2e317f987925601],assignPublicIp=ENABLED}" `
  --overrides file://backfill-live-overrides.json `
  --region ca-central-1 `
  --query "tasks.taskArn" --output text
Remove-Item backfill-live-overrides.json
Start-Sleep -Seconds 180

$TASK_ID = $TASK_ARN.Split('/')[-1]
aws logs get-log-events `
  --log-group-name "/ecs/luciel-backend" `
  --log-stream-name "migrate/luciel-backend/$TASK_ID" `
  --region ca-central-1 --start-from-head `
  --query "events[*].message" --output text
# Expected: SUMMARY block with "agents.user_id IS NULL: 0" in the
# residual section. Phase B residuals on memory_items.actor_user_id
# may remain >0 -- tolerated per drift D11.
```

**Gate before Phase 3:** Phase A `agents.user_id IS NULL` MUST be 0.
If non-zero, the backfill skipped some agents (likely orphan keys
referencing missing Agent rows -- check the script's error log).
If memory_items residuals remain (drift D11), proceed -- Step 28
sweep handles those. The script's exit code will be 1 if any
residual remains; that exit code is purely informational here
(File 3.8 was rolled back per D20, no NOT NULL flip migration is
gated on it).

---

## Phase 3 — Image rebuild + service registration (15 min)

```powershell
# 3.1 Build new image carrying Commits 1+2+3 code
$SHA = git rev-parse --short HEAD
# Expected SHA: 314ac46 (Commit 3 of 4 close)
$TAG = "step24.5b-$SHA"
$ECR = "729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend"

# ECR login
aws ecr get-login-password --region ca-central-1 | `
  docker login --username AWS --password-stdin `
    729005488042.dkr.ecr.ca-central-1.amazonaws.com

# Build
docker build --platform linux/amd64 -t "luciel-backend:$TAG" .

# Push
docker tag "luciel-backend:$TAG" "$ECR`:$TAG"
docker push "$ECR`:$TAG"

# 3.2 Capture new digest
$NEW_DIGEST = aws ecr describe-images --repository-name luciel-backend `
  --image-ids "imageTag=$TAG" --region ca-central-1 `
  --query "imageDetails.imageDigest" --output text
$NEW_IMAGE = "$ECR@$NEW_DIGEST"
Write-Host "New image: $NEW_IMAGE"

# 3.3 Register luciel-backend:16 (web) pinned to new digest
aws ecs describe-task-definition --task-definition luciel-backend `
  --region ca-central-1 --query "taskDefinition" > tmpbackend-current.json

# Strip null fields, swap image
$rawJson = Get-Content tmpbackend-current.json -Raw
$rawJson | jq --arg img "$NEW_IMAGE" `
  '{family, taskRoleArn, executionRoleArn, networkMode, containerDefinitions, volumes, placementConstraints, requiresCompatibilities, cpu, memory, runtimePlatform} | .containerDefinitions |= map(if .name == "web" then .image = $img else . end)' `
  > tmpbackend-16.json

$BACKEND16_ARN = aws ecs register-task-definition `
  --cli-input-json file://tmpbackend-16.json --region ca-central-1 `
  --query "taskDefinition.taskDefinitionArn" --output text
Remove-Item tmpbackend-current.json, tmpbackend-16.json
Write-Host "Registered: $BACKEND16_ARN"
# Expected: arn ending in luciel-backend:16

# 3.4 Register luciel-worker:5 (worker) pinned to new digest
aws ecs describe-task-definition --task-definition luciel-worker `
  --region ca-central-1 --query "taskDefinition" > tmpworker-current.json

$rawJson = Get-Content tmpworker-current.json -Raw
$rawJson | jq --arg img "$NEW_IMAGE" `
  '{family, taskRoleArn, executionRoleArn, networkMode, containerDefinitions, volumes, placementConstraints, requiresCompatibilities, cpu, memory, runtimePlatform} | .containerDefinitions |= map(if .name == "worker" then .image = $img else . end)' `
  > tmpworker-5.json

$WORKER5_ARN = aws ecs register-task-definition `
  --cli-input-json file://tmpworker-5.json --region ca-central-1 `
  --query "taskDefinition.taskDefinitionArn" --output text
Remove-Item tmpworker-current.json, tmpworker-5.json
Write-Host "Registered: $WORKER5_ARN"
# Expected: arn ending in luciel-worker:5
```

**Gate before Phase 4:** both task-def ARNs must show `:16` and `:5`
respectively. Image digest matches across both registrations.

---

## Phase 4 — Service rollout (15 min)

Web first, worker second. Each waits for services-stable before the
next moves. ECS does the rolling deploy automatically.

```powershell
# 4.1 Roll web -> luciel-backend:16
aws ecs update-service `
  --cluster luciel-cluster `
  --service luciel-backend-service `
  --task-definition $BACKEND16_ARN `
  --region ca-central-1 > $null

aws ecs wait services-stable `
  --cluster luciel-cluster `
  --services luciel-backend-service `
  --region ca-central-1
Write-Host "web stable on luciel-backend:16"

# Smoke: ALB still answering?
curl.exe -s https://api.vantagemind.ai/health
# Expected: {"status":"ok","service":"Luciel Backend"}

# 4.2 Roll worker -> luciel-worker:5
aws ecs update-service `
  --cluster luciel-cluster `
  --service luciel-worker-service `
  --task-definition $WORKER5_ARN `
  --region ca-central-1 > $null

aws ecs wait services-stable `
  --cluster luciel-cluster `
  --services luciel-worker-service `
  --region ca-central-1
Write-Host "worker stable on luciel-worker:5"

# 4.3 Verify worker boot logs (no crash, gates 5+6 wired)
$LATEST_STREAM = aws logs describe-log-streams `
  --log-group-name "/ecs/luciel-worker" `
  --order-by LastEventTime --descending --max-items 1 `
  --region ca-central-1 `
  --query "logStreams.logStreamName" --output text

aws logs filter-log-events `
  --log-group-name "/ecs/luciel-worker" `
  --log-stream-names $LATEST_STREAM `
  --region ca-central-1 `
  --query "events[*].message" --output text | Tee-Object tmp/worker-boot.log
# Expected: "celery@... ready" line. NO "ClusterCrossSl
# Expected: "celery@... ready" line. NO "ClusterCrossSlot",
# "ListQueues", or "CreateQueue" errors (those would indicate
# Step 27c-final's broker-config regression returned, which it
# shouldn't because Commits 1+2+3 didn't touch broker config).

if (Select-String -Path tmp/worker-boot.log -Pattern "ClusterCrossSlot|ListQueues|CreateQueue" -Quiet) {
    Write-Host "ERROR: forbidden broker call detected in worker boot. ABORT."
    # Phase 4 rollback: see ROLLBACK CONTRACT section below.
    exit 1
}

---

## Phase 5 — Prod 14/14 gate via ECS exec (15 min)

The verification suite runs from inside the running web task so it
hits the same DB/SQS/secrets the production traffic does. Same
pattern as Step 27c-final's MODE=full gate.

```powershell
# 5.1 Pick a running web task to exec into
$TASK_ARN = aws ecs list-tasks `
  --cluster luciel-cluster `
  --service-name luciel-backend-service `
  --region ca-central-1 `
  --query "taskArns" --output text
Write-Host "Targeting task: $TASK_ARN"

# 5.2 Run the verification suite from inside the task
aws ecs execute-command `
  --cluster luciel-cluster `
  --task $TASK_ARN `
  --container web `
  --interactive `
  --command "python -m app.verification" `
  --region ca-central-1
# Expected: 14/14 pillars green, including:
#   Pillar 12 (identity stability under role change)
#   Pillar 13 (cross-tenant identity-spoof guard) -- this time in
#     MODE=full because prod has live SQS broker, so the malicious
#     payload ACTUALLY enqueues + worker consumes + Gate 6 fires +
#     ACTION_WORKER_IDENTITY_SPOOF_REJECT audit row lands
#   Pillar 14 (departure semantics, Q6 bounded cascade)

# 5.3 If anything below 14/14, capture the failing matrix and
# proceed to Phase 4 rollback (services back to luciel-backend:15 /
# luciel-worker:4). Phases 1+2 stay live (additive migrations are
# forward-compatible). DO NOT tag.
```

**Gate before Phase 6:** prod 14/14 MODE=full pillars green. The
critical new green is Pillar 13 in MODE=full — that's the test
proving the cross-tenant identity-spoof guard fires end-to-end on
real SQS + real worker, not just mode-degraded local. This is the
test a brokerage prospect's security review would actually run.

---

## Phase 6 — Release tag (1 min)

```powershell
$tagMsg = @"
step-24.5b-20260503  Durable User Identity Layer (Q6 + Q5 prerequisite)

Combined release of three commits:
  78716fe  feat(24.5b-schema): users + scope_assignments tables, agent.user_id FK (additive)
  766427c  feat(24.5b-services): identity propagation + Q6 mandatory key rotation
  314ac46  feat(24.5b-verification): backfill + Pillars 12/13/14 + 14/14 MODE=full gate

LANDED on prod
- users + scope_assignments tables (additive, both new identity
  columns nullable per drifts D11 + D20)
- Mandatory key rotation cascade on role change (Q6 hard rotation,
  no grace period)
- request.state.actor_user_id injected by middleware
- ChatService threads actor_user_id end-to-end through sync + async
  memory paths
- Worker defense-in-depth Gates 5 (User active) and 6 (cross-tenant
  identity-spoof guard) live and audit-emitting
- Pillars 12/13/14 in app.verification, MODE=full prod gate green
- POST /api/v1/users HTTP surface (4 routes: POST/GET/PATCH/DELETE)
- One-shot backfill script idempotent + audit-emitting

DEFERRED to Step 28
- agents.user_id NOT NULL flip (D20 -- public POST /admin/agents
  doesn't supply user_id; Step 28 hardens public routes first)
- memory_items.actor_user_id NOT NULL flip (D11 -- 10 historical
  orphan rows; Step 28 sweep first)
- ApiKeyService.deactivate_key audit emission retrofit (D5)
- consent.py prefix doubling fix (D16, API-breaking)
- Synthetic-email PATCH path (D14)
- admin_audit_log duplicate constants cleanup (D2)

VERIFIED on prod
- 14/14 pillars green via app.verification --mode=full
- Pillar 13 in MODE=full proves cross-tenant identity-spoof guard
  fires against real SQS-routed malicious payload
- Migration chain at head 4e989b9392c0
- agents.user_id IS NULL = 0 post-backfill

INFRASTRUCTURE STATE AT TAG
- Web luciel-backend:16, image step24.5b-314ac46
- Worker luciel-worker:5
- RDS Alembic head 4e989b9392c0
- RDS rollback snapshot luciel-db-pre-step-24-5b-<stamp>

ROLLBACK
- Web/worker rollback ceiling: luciel-backend:15 / luciel-worker:4
  (Step 27c-final close versions)
- 5-min recovery via update-service --force-new-deployment
- DB downgrade safe via alembic downgrade 8e2a1f5b9c4d (both Step
  24.5b migrations reverse cleanly)

Closes Q6 (role changes -- promotions / demotions / departures).
Satisfies Q5 prerequisite (email-stable User identity for Step 38
bottom-up tenant merge). Outreach posture upgraded: brokerage
prospects can now be told "an agent leaving REMAX Crossroads keeps
their identity at any other brokerage they hold scope at, and their
old keys stop working in the same transaction the role ends."
"@

$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText(
  (Join-Path (Get-Location) "step-24-5b-tagmsg.txt"),
  $tagMsg, $utf8NoBom
)

git tag -a step-24.5b-20260503 314ac46 -F step-24-5b-tagmsg.txt
Remove-Item step-24-5b-tagmsg.txt
git push origin step-24.5b-20260503

# Confirm tag on origin
git ls-remote --tags origin | Select-String "step-24.5b-20260503"
```

**Gate after Phase 6:** tag pushed, visible on origin. Step 24.5b
is officially closed.

---

## Rollback Contract per Phase

| Phase | What changes | Rollback procedure |
|---|---|---|
| 0 | RDS snapshot taken; nothing touched in app | None needed — snapshot is read-only |
| 1 | Two new tables (users + scope_assignments) + agents.user_id column added (nullable) + memory_items.actor_user_id column added (nullable) | `alembic downgrade 8e2a1f5b9c4d` reverses both migrations cleanly. Empty new tables, FK columns drop. |
| 2 | Backfill writes Users + populates agents.user_id | Forward-compatible. Synthetic-email Users created cannot be cleanly un-created without orphaning Agent.user_id FKs. Tolerable — Step 28 sweep handles. If full revert is required, restore RDS snapshot from Phase 0. |
| 3 | New ECS task-def revisions (luciel-backend:16, luciel-worker:5) registered | Old task-defs (luciel-backend:15, luciel-worker:4) remain ACTIVE in ECS. Roll back via `update-service --task-definition luciel-backend:15`. |
| 4 | Services rolled to :16 / :5 | `update-service --task-definition luciel-backend:15 --force-new-deployment` (web), same for worker:4. 5-minute recovery. RDS unchanged. |
| 5 | Read-only verification | Never touched — gate is read-only. |
| 6 | Release tag pushed | `git tag -d step-24.5b-20260503` + `git push --delete origin step-24.5b-20260503`. Tag is informational; deletion doesn't affect deployed code. |

**Web rollback ceiling:** `luciel-backend:15` (Step 27c-final close).
**Worker rollback ceiling:** `luciel-worker:4` (Step 27c-final close).
**RDS rollback boundary:** `luciel-db-pre-step-24-5b-<stamp>` from Phase 0.

If Phase 5 fails and we don't tag, Phase 1 (migrations) remains live
on prod. That's intentional — additive nullable columns are
forward-compatible with any rollback of services to :15 / :4. The
old code paths simply ignore the new columns.

---

## Step 28 backlog formalized at this release

Documented in the canonical recap for the next step's work plan:

- **D2** admin_audit_log.py duplicate ACTION_KNOWLEDGE_* constants and
  RESOURCE_KNOWLEDGE value override — one-line cleanup commit
- **D5** ApiKeyService.deactivate_key audit-emission retrofit — accept
  audit_ctx, emit per-key audit row in same txn
- **D10** Migration emission convention recorded — body-diff only,
  never full-file templates (going-forward, not blocking)
- **D11** memory_items.actor_user_id NOT NULL flip — runs after
  orphan-memory sweep handles the 10 historical NULL rows
- **D14** UserUpdate.email PATCH path for synthetic emails (.luciel.local)
  — currently 422s through public API; rare but needs handling
- **D16** consent.py prefix doubling (`/api/v1/api/v1/consent/...`)
  — API-breaking fix needs its own deploy plan
- **D18** Local LLM-based memory extractor produces 0 rows on test
  message shapes — extractor prompt tuning + fallback path
- **D20** agents.user_id NOT NULL flip — runs after public POST
  /admin/agents requires user_id explicitly OR auto-creates a
  synthetic User binding; coordinated with the route-hardening
  pass on the admin layer

Plus the standing CloudWatch alarms / auto-scale / IAM splits /
dedicated worker SG items already on the Step 28 backlog from Step
26b and Step 27c-final closures.

---

## Security reminder before any external demo

Drift D1 outstanding: local platform-admin key `luc_sk_HY_RKmywB7x...`
was exposed in chat 2026-04-26 13:00 EDT. Rotation deferred to
end-of-Step-24.5b business arc, but it is a **HARD GATE before any
external demo** (GTA brokerage outreach, prospect call, recorded
walkthrough, anything that could put credentials adjacent to a real
audience).

Rotation procedure:

```powershell
# Rotate via the Step 27a SSM-direct mint pattern
python -m scripts.mint_platform_admin_ssm `
  --display-name "Local Platform Admin (post-Step-24.5b, $(Get-Date -Format yyyy-MM-dd))" `
  --created-by "aryan@step24.5b-rotation" `
  --region ca-central-1
# Retrieve raw key from SSM, save to password manager, delete SSM parameter.
# Then deactivate the leaked key id (look it up by prefix in api_keys).
```

This rotation is local-dev only — does not touch prod platform-admin.
Prod's platform-admin key lives at SSM
`/luciel/production/platform-admin-key` and is read fresh on every
verification suite run. It was minted at Step 27c-final close and
has had zero exposure events since.

---

## Wall-clock summary

| Phase | Wall-clock | Notes |
|---|---|---|
| 0 — pre-flight | 5 min | RDS snapshot runs in background |
| 1 — migration | 5 min | Two migrations, transactional DDL |
| 2 — backfill | 10 min | Dry-run + live, ECS one-shot tasks |
| 3 — image rebuild + register | 15 min | Most time in docker push |
| 4 — service rollout | 15 min | ECS rolling deploy, web then worker |
| 5 — prod 14/14 gate | 15 min | ECS execute-command into running task |
| 6 — tag | 1 min | git tag + push |
| **Total** | **66 min** | First-time deploy of Step 24.5b layer |

This matches the 60–90 minute estimate at the header. Subsequent
Step 24.5b-related deploys (e.g. Step 28's NOT NULL flip and route
hardening) reuse this runbook's Phase 0 / 1 / 4 / 5 / 6 shape; only
Phase 2 (backfill) is a Step 24.5b-specific phase that won't repeat.