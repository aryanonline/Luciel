# Step 30c — Action-classification gate prod deploy

**Scope:** Roll the action-classification gate (closing tag
`step-30c-action-classification-complete` on `main` HEAD `99c6eb5`)
into the running ECS backend and worker services. No DB migration. No
new environment variables (the two new `Settings` knobs ship with
production-correct defaults). No infrastructure change. Rolling deploy,
zero expected downtime.

**Pre-deploy state (verified 2026-05-11):**

- Code on `main` HEAD `84339a3` (doc-truthing consistency commit; the
  code-complete commit is `b216300` and the closing tag is on `99c6eb5`).
- ECR repo: `729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend`
- Current pinned image digest in `td-backend-rev34.json` and
  `td-worker-rev14.json`:
  `sha256:bf0259387cbf65c9b83dea35339f908754f34c81e9a0a8de564f9790ccdcfc36`
- Cluster: `luciel-cluster`. Services: `luciel-backend-service`,
  `luciel-worker-service`. Region: `ca-central-1`.

**Default-safety claim (why this is a low-risk rollout):**

The fail-closed wrapper is the production default
(`action_classifier='static'`, `action_classifier_fail_closed=True`,
both already baked into `app/core/config.py`, not env-overridable in
the task definitions). Worst-case behavioral change on rollout: a tool
that ships without a `declared_tier` declaration gets routed to
`APPROVAL_REQUIRED` instead of executing. The three shipped tools all
have explicit tier declarations
(`save_memory`/`get_session_summary` = ROUTINE,
`escalate_to_human` = NOTIFY_AND_PROCEED), so no in-traffic tool
suddenly starts asking for approval.

---

## Step 0 — Sanity check from operator workstation

```powershell
# Confirm you are on the right commit
git log --oneline -1
# expect: 84339a3 ... doc-truthing follow-up ...

# Confirm the closing tag is reachable
git show --no-patch step-30c-action-classification-complete | head -3

# Confirm the AWS profile (per operator-patterns.md Hazard 2)
aws configure list-profiles
# expect exactly: default
```

## Step 1 — Build the image locally and tag with the closing-commit SHA

```powershell
$IMAGE_TAG = "step-30c-84339a3"
$ECR_REPO  = "729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend"

docker build -t "luciel-backend:$IMAGE_TAG" .
docker tag    "luciel-backend:$IMAGE_TAG" "$ECR_REPO`:$IMAGE_TAG"
```

## Step 2 — Auth to ECR and push

```powershell
aws ecr get-login-password --region ca-central-1 `
  | docker login --username AWS --password-stdin $ECR_REPO

docker push "$ECR_REPO`:$IMAGE_TAG"
```

Capture the pushed image digest — the task-def update pins by digest,
not by tag, because tag-pinning is fragile:

```powershell
$NEW_DIGEST = (aws ecr describe-images `
  --repository-name luciel-backend `
  --image-ids imageTag=$IMAGE_TAG `
  --region ca-central-1 `
  --query 'imageDetails[0].imageDigest' --output text)

Write-Host "New digest: $NEW_DIGEST"
# expect a sha256:... string
```

## Step 3 — Update both task-def manifests in the repo

Find-and-replace the old digest with the new one in both
`td-backend-rev34.json` and `td-worker-rev14.json` (keep the `family`
and everything else unchanged). The diff should be exactly two lines.

```powershell
$OLD_DIGEST = "sha256:bf0259387cbf65c9b83dea35339f908754f34c81e9a0a8de564f9790ccdcfc36"

(Get-Content td-backend-rev34.json) -replace [regex]::Escape($OLD_DIGEST), $NEW_DIGEST `
  | Set-Content td-backend-rev34.json

(Get-Content td-worker-rev14.json)  -replace [regex]::Escape($OLD_DIGEST), $NEW_DIGEST `
  | Set-Content td-worker-rev14.json

git diff td-backend-rev34.json td-worker-rev14.json
# expect exactly 2 changed lines, one per file
```

## Step 4 — Register the new task definitions

```powershell
$BACKEND_TD = (aws ecs register-task-definition `
  --cli-input-json file://td-backend-rev34.json `
  --region ca-central-1 `
  --query 'taskDefinition.taskDefinitionArn' --output text)

$WORKER_TD = (aws ecs register-task-definition `
  --cli-input-json file://td-worker-rev14.json `
  --region ca-central-1 `
  --query 'taskDefinition.taskDefinitionArn' --output text)

Write-Host "Backend TD: $BACKEND_TD"
Write-Host "Worker  TD: $WORKER_TD"
```

Both ARNs will be one revision higher than the previous; record those
revision numbers — the post-deploy doc-truthing wants them.

## Step 5 — Roll the services forward

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

## Step 6 — Watch the rollouts converge

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
auto-rolls-back; if you see ACTIVE deployments lingering longer than a
couple of minutes, capture CloudWatch logs and stop here.

## Step 7 — Observe enforcement in CloudWatch

The gate stamps `tier`, `tier_reason`, and `classifier` on every
`ToolResult.metadata`. The simplest first-witness is the broker emitting
a structured log line that includes the tier. Logs Insights query
against `/ecs/luciel-backend`:

```
fields @timestamp, @message
| filter @message like /tier_reason/
| sort @timestamp desc
| limit 50
```

Within a few minutes of real traffic you should see lines with
`tier_reason='declared_tier'` (the normal success path) on every
`save_memory` / `get_session_summary` / `escalate_to_human` call. A
single `tier_reason='unknown_tool'` or `tier_reason='tier_undeclared'`
in production is a real audit event and worth investigating; it should
not happen with the three shipped tools but it is the signal the gate
exists to surface.

## Step 8 — Post-deploy doc-truthing

In the same commit:

1. Append to `DRIFTS.md` §5
   `~~D-confirmation-gate-not-enforced-2026-05-09~~`:
   > **Prod observed-clean:** First production enforcement observed
   > YYYY-MM-DD on `luciel-backend-service` task-def revision N and
   > `luciel-worker-service` task-def revision M, both pinned to ECR
   > digest `<NEW_DIGEST>` built from `main` HEAD `84339a3` (closing
   > tag target `99c6eb5`). First `tier_reason='declared_tier'` line
   > in `/ecs/luciel-backend` at HH:MM:SS UTC. No unexpected
   > `unknown_tool` or `tier_undeclared` codes observed in the first
   > <duration> of post-deploy traffic.
2. Add a one-line note to `CANONICAL_RECAP.md` §12 row 30c after the
   existing closeout: > "Production enforcement first observed on
   <date> via ECS rev N (backend) / rev M (worker)."
3. Commit message:
   `Step 30c prod deploy: action-classification gate observed clean on luciel-backend-service rev N`

## Rollback

If anything goes sideways during Step 6:

```powershell
aws ecs update-service `
  --cluster luciel-cluster --service luciel-backend-service `
  --task-definition luciel-backend:34 `
  --region ca-central-1

aws ecs update-service `
  --cluster luciel-cluster --service luciel-worker-service `
  --task-definition luciel-worker:14 `
  --region ca-central-1
```

The previous revisions stay pinned to the pre-deploy digest and roll
back in place. ECS circuit breaker should beat us to it for an
obvious failure mode, but having the explicit rollback recorded here
removes a decision point at 2am.

## Cross-references

- Closing tag: `step-30c-action-classification-complete` on `99c6eb5`
- DRIFTS entry being prod-truthed: `D-confirmation-gate-not-enforced-2026-05-09`
- Service-name precedent: `docs/runbooks/operator-patterns.md` §Hazard 1
- Last prior backend deploy of comparable shape: `step-24.5b-identity-deploy.md`
