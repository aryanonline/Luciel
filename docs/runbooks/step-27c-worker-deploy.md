# Step 27b — `luciel-worker` ECS Service Deploy Runbook

**Target tag:** `step-27-20260429`
**Pre-deploy gate:** local `python -m app.verification` → 11/11 green
**Post-deploy gate:** prod `python -m app.verification` → 11/11 green
**Estimated wall-clock:** 60–90 minutes for first-time worker provisioning

This runbook deploys the new `luciel-worker` ECS service. The web stack
update (`luciel-backend:8` carrying 27a + 27b code) follows the
established ECR push → task-def register → service update pattern from
26b. This document focuses on the **net-new worker stack**.

Reference: `docs/runbooks/step-27b-security-contract.md`.

---

## Phase 0 — Pre-flight (5 min)

```powershell
# 0.1 Confirm broker URL is provisioned
aws ssm get-parameter --name /luciel/production/REDIS_URL --with-decryption `
  --region ca-central-1 --query "Parameter.Value" --output text

# 0.2 Confirm latest web image digest
aws ecr describe-images --repository-name luciel-backend `
  --image-ids imageTag=latest --region ca-central-1 `
  --query "imageDetails.imageDigest" --output text

# 0.3 Confirm no luciel-worker resources already exist (idempotency check)
aws ecs describe-services --cluster luciel-cluster `
  --services luciel-worker-service --region ca-central-1 `
  --query "services.status" 2>$null
# Expected: empty / MISSING. If ACTIVE, this is a re-deploy — skip provisioning.

aws sqs list-queues --queue-name-prefix luciel-memory `
  --region ca-central-1 --query "QueueUrls" --output text
# Expected: empty on first deploy. If queues exist, skip Phase 1.
```

---

## Phase 1 — SQS queue provisioning (5 min)

```powershell
# 1.1 Create DLQ first (main queue references it)
$dlqArn = aws sqs create-queue `
  --queue-name luciel-memory-dlq `
  --region ca-central-1 `
  --attributes "MessageRetentionPeriod=1209600" `
  --query "QueueUrl" --output text

aws sqs get-queue-attributes `
  --queue-url $dlqArn `
  --attribute-names QueueArn `
  --region ca-central-1 `
  --query "Attributes.QueueArn" --output text
# Capture the QueueArn output — used as deadLetterTargetArn below.
$dlqArnValue = "<paste from previous command>"

# 1.2 Create main queue with DLQ + redrive policy (3 receives → DLQ)
aws sqs create-queue `
  --queue-name luciel-memory-tasks `
  --region ca-central-1 `
  --attributes ('{
    "VisibilityTimeout":"30",
    "MessageRetentionPeriod":"345600",
    "RedrivePolicy":"{\"deadLetterTargetArn\":\"' + $dlqArnValue + '\",\"maxReceiveCount\":\"3\"}"
  }' -replace '\s','')
```

---

## Phase 2 — IAM roles for the worker task (10 min)

Two roles: **task execution role** (pulls image, reads SSM, writes logs)
and **task role** (the worker process's runtime AWS identity).

```powershell
# 2.1 Reuse the existing execution role from luciel-backend (no change needed)
$execRoleArn = "arn:aws:iam::729005488042:role/luciel-ecs-execution-role"

# 2.2 Create the worker-specific task role
aws iam create-role --role-name luciel-ecs-worker-role `
  --assume-role-policy-document file://ecs-trust-policy.json

# 2.3 Attach a least-privilege inline policy
@'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SQSReceiveAndDelete",
      "Effect": "Allow",
      "Action": [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:SendMessage",
        "sqs:GetQueueUrl",
        "sqs:GetQueueAttributes",
        "sqs:ChangeMessageVisibility"
      ],
      "Resource": [
        "arn:aws:sqs:ca-central-1:729005488042:luciel-memory-tasks",
        "arn:aws:sqs:ca-central-1:729005488042:luciel-memory-dlq"
      ]
    },
    {
      "Sid": "SSMWorkerSecrets",
      "Effect": "Allow",
      "Action": ["ssm:GetParameters", "ssm:GetParameter"],
      "Resource": [
        "arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/REDIS_URL",
        "arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/DATABASE_URL",
        "arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/OPENAI_API_KEY",
        "arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/ANTHROPIC_API_KEY"
      ]
    }
  ]
}
'@ | Out-File -Encoding utf8NoBOM luciel-worker-policy.json

aws iam put-role-policy --role-name luciel-ecs-worker-role `
  --policy-name luciel-worker-inline `
  --policy-document file://luciel-worker-policy.json

$taskRoleArn = "arn:aws:iam::729005488042:role/luciel-ecs-worker-role"
```

> **Step 28 follow-up:** create separate `luciel_worker` Postgres role with
> SELECT/INSERT on `memory_items, admin_audit_logs` and SELECT on
> `messages, sessions, users, api_keys, tenants, agents, luciel_instances`.
> Provision via `WORKER_DATABASE_URL` SSM param. For Step 27b initial deploy,
> worker reuses the web `DATABASE_URL` — flagged in Step 28 backlog.

---

## Phase 3 — CloudWatch log group (1 min)

```powershell
aws logs create-log-group --log-group-name /ecs/luciel-worker `
  --region ca-central-1
# Retention: null (infinite) — matches /ecs/luciel-backend, PIPEDA-compliant.
```

---

## Phase 4 — Register `luciel-worker` task definition (5 min)

```powershell
$NEW_DIGEST = "<digest from Phase 0.2>"
$ECR        = "729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend"
$NEW_IMAGE  = "$ECR@$NEW_DIGEST"

$workerTaskDef = @{
  family                  = "luciel-worker"
  networkMode             = "awsvpc"
  requiresCompatibilities = @("FARGATE")
  cpu                     = "256"
  memory                  = "1024"
  executionRoleArn        = $execRoleArn
  taskRoleArn             = $taskRoleArn
  containerDefinitions    = @(@{
    name      = "luciel-worker"
    image     = $NEW_IMAGE
    essential = $true
    command   = @(
      "celery", "-A", "app.worker.celery_app", "worker",
      "--loglevel=info",
      "--concurrency=2",
      "--prefetch-multiplier=1"
    )
    healthCheck = @{
      command  = @(
        "CMD-SHELL",
        "celery -A app.worker.celery_app inspect ping -d celery@$HOSTNAME || exit 1"
      )
      interval = 30
      timeout  = 10
      retries  = 3
      startPeriod = 60
    }
    logConfiguration = @{
      logDriver = "awslogs"
      options   = @{
        "awslogs-group"         = "/ecs/luciel-worker"
        "awslogs-region"        = "ca-central-1"
        "awslogs-stream-prefix" = "worker"
      }
    }
    secrets = @(
      @{ name="DATABASE_URL";      valueFrom="arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/DATABASE_URL" },
      @{ name="REDIS_URL";         valueFrom="arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/REDIS_URL" },
      @{ name="OPENAI_API_KEY";    valueFrom="arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/OPENAI_API_KEY" },
      @{ name="ANTHROPIC_API_KEY"; valueFrom="arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/ANTHROPIC_API_KEY" }
    )
    environment = @(
      @{ name="MEMORY_EXTRACTION_ASYNC"; value="true" },
      @{ name="AWS_REGION";              value="ca-central-1" }
    )
  })
} | ConvertTo-Json -Depth 8

$workerTaskDef | Out-File -Encoding utf8NoBOM worker-task-def-v1.json

aws ecs register-task-definition `
  --cli-input-json file://worker-task-def-v1.json `
  --region ca-central-1 `
  --query "taskDefinition.[family,revision,status]"
# Expected: ["luciel-worker", 1, "ACTIVE"]
```

---

## Phase 5 — Create the `luciel-worker-service` ECS service (5 min)

```powershell
aws ecs create-service `
  --cluster luciel-cluster `
  --service-name luciel-worker-service `
  --task-definition luciel-worker:1 `
  --desired-count 1 `
  --launch-type FARGATE `
  --network-configuration ('{
    "awsvpcConfiguration": {
      "subnets": ["subnet-0e54df62d1a4463bc", "subnet-0e95d953fd553cbd1"],
      "securityGroups": ["sg-0f2e317f987925601"],
      "assignPublicIp": "ENABLED"
    }
  }' -replace '\s','') `
  --region ca-central-1 `
  --query "service.[serviceName,status,desiredCount]"
```

> **Note on security group:** worker reuses the web SG for 27b initial deploy
> (Redis SG already accepts traffic from this SG). Step 28 follow-up: create
> dedicated `luciel-worker-sg` with egress only to Redis (6379), RDS (5432),
> SSM/SQS VPC endpoints, and OpenAI/Anthropic public endpoints.

---

## Phase 6 — Smoke tests (10 min)

```powershell
# 6.1 Worker reaches RUNNING state
aws ecs describe-services --cluster luciel-cluster `
  --services luciel-worker-service --region ca-central-1 `
  --query "services[0].[runningCount,desiredCount,deployments[0].rolloutState]"
# Expected: [1, 1, "COMPLETED"]

# 6.2 Worker process is alive (Celery inspect ping over Redis)
# Run from any host with REDIS_URL access; locally requires VPC tunnel.
$env:REDIS_URL = "<paste from Phase 0.1>"
python -c "from app.worker.celery_app import celery_app; print(celery_app.control.ping(timeout=3.0))"
# Expected: a non-empty list of {"celery@<hostname>": {"ok": "pong"}} entries.

# 6.3 Queue depth endpoint returns structured payload (web side)
$env:LUCIEL_PLATFORM_ADMIN_KEY = "luc_sk_kHqA2..."   # prod platform_admin
curl.exe -s `
  -H "Authorization: Bearer $env:LUCIEL_PLATFORM_ADMIN_KEY" `
  https://api.vantagemind.ai/api/v1/admin/worker/queue-depth
# Expected: {"region":"ca-central-1","main_queue":{...,"approximate_messages":0},"dlq":{...,"approximate_messages":0}}

# 6.4 CloudWatch worker log group is receiving entries
aws logs describe-log-streams --log-group-name /ecs/luciel-worker `
  --region ca-central-1 --order-by LastEventTime --descending `
  --query "logStreams[0].[logStreamName,lastEventTimestamp]"
# Expected: a log stream prefixed "worker/" with a recent timestamp.

## Phase 7 — Production verification gate (5 min)

# 7.1 Run the full 11-pillar suite against prod
$env:LUCIEL_BASE_URL = "https://api.vantagemind.ai"
$env:LUCIEL_PLATFORM_ADMIN_KEY = "luc_sk_kHqA2..."  # prod platform_admin

python -m app.verification --json-report step27_report_prod.json

# Expected:
#   - 11/11 pillars green
#   - Pillar 11 detail string contains "MODE=full"
#   - Sub-assertions F1..F10 all referenced in detail
#   - Exit code 0
#   - JSON artifact: step27_report_prod.json

# 7.2 Archive the gate artifact
$STAMP = Get-Date -Format "yyyyMMddHHmm"
Copy-Item step27_report_prod.json "step27_report_PROD_golive_$STAMP.json"
Get-Item "step27_report_PROD_golive_$STAMP.json" | Select-Object Name, Length, LastWriteTime

## Phase 8 — Tag the release (2 min)

# 8.1 Confirm HEAD is the merge commit containing all 27b files
git log -1 --oneline

# 8.2 Tag and push
$tagMsg = @"
Step 27b: async memory extraction via SQS/Celery on luciel-worker

Combined release: 27a hardening (already tagged step-27a-20260422) +
27b async worker. Prod verification 11/11 green at $(Get-Date -Format 'yyyy-MM-dd HH:mm') EDT.

Components:
- New ECS service luciel-worker (task-def luciel-worker:1, desired=1)
- SQS queues luciel-memory-tasks + luciel-memory-dlq (3-receive redrive)
- IAM role luciel-ecs-worker-role (least-privilege SQS + SSM)
- Migration 8e2a1f5b9c4d (memory_items.message_id + luciel_instance_id +
  composite partial unique index)
- Pillar 11 (async memory extraction) registered in app.verification

Rollback bundle:
- Web task-def: luciel-backend:8 -> :7 (15-min recovery, proven 26b pattern)
- Worker scale: aws ecs update-service --service luciel-worker-service
  --desired-count 0 (drains queue without prod chat impact)
- Feature flag: MEMORY_EXTRACTION_ASYNC=false in SSM (sync path resumes)
- Migration: alembic downgrade -1 (additive; safe revert)
- RDS snapshot: luciel-db-pre-step27-20260429
"@

$tagMsg | Out-File -Encoding utf8NoBOM step27tagmsg.txt
git tag -a step-27-20260429 HEAD -F step27tagmsg.txt
Remove-Item step27tagmsg.txt

git push origin step-27-20260429
git ls-remote --tags origin | Select-String "step-27-20260429"


---

## Step 27c rollout addendum (2026-04-25 EDT)

### What landed in prod

- `luciel-backend:14` registered, image digest `sha256:b93b3086`,
  `MEMORY_EXTRACTION_ASYNC=true`. Rolled from `:13` (async OFF) to `:14` at ~21:44 EDT.
- `luciel-worker:4` registered with cleaned command:
  `celery -A app.worker.celery_app worker --loglevel=info -Q luciel-memory-tasks
  --concurrency=2 --without-gossip --without-mingle --without-heartbeat`
  Scaled `luciel-worker-service` from `desired=0` → `desired=1`. Reached `ready`
  at 00:50:39 UTC. Logs clean: no ClusterCrossSlotError, no ListQueues, no CreateQueue.
- IAM hotfix: `luciel-ecs-web-role` inline policy `luciel-web-sqs-readonly`
  expanded to grant `sqs:SendMessage` on `luciel-memory-tasks` and
  `ssmmessages:Create*/Open*` on `*`. Force-new-deployment applied so running
  task picked up the new role.

### Failure caught and corrected during rollout

The web role was originally `sqs-readonly` only. After the `:14` flip, every
chat turn's `enqueue_extraction` call hit `AccessDenied` on `sqs:SendMessage`.
The fail-open contract in `chat_service.py` caught it correctly — chat turns
returned 200 to clients, no errors surfaced — but **no memory rows were written
during the ~1 hour gap.**

Detection signal: 90-second queue watch loop showed flat `main=0 dlq=0` despite
expected traffic. Root-caused via `aws iam get-role-policy`. Fixed by replacing
the inline policy with the consolidated three-statement version.

This is the first concrete production-validated argument for Pillar 11 as a
mandatory gate: every component in the path was individually correct, and only
end-to-end verification would have caught the IAM oversight pre-flip.

### Verification status: PARTIAL

What was empirically verified tonight:
- Worker boot, queue binding, task registration
- Backend env var flip, deployment health
- IAM policy contents post-fix
- SQS queue depth and DLQ depth post-fix
- Code parity: deployed digest matches `main` HEAD `1c3b058`

What was NOT verified tonight:
- A real chat turn through the ALB producing a `memory_items` row via the worker
- Pillar 11 (`async_memory`) in `MODE=full`

### Why verification is incomplete

ECS exec / SSM Session Manager not usable in current VPC topology:
- Zero VPC interface endpoints in `ca-central-1`
- Tasks have public IPs and reach SQS via IGW egress, but SSM Messages
  WebSocket fails to establish (root cause unconfirmed; likely security group
  egress rules or hostname resolution timing in Fargate networking)
- Operator workstation cannot reach RDS (private subnet, no ingress rule)
- No platform-admin key in SSM; no test API key in SSM

End-to-end verification requires resolving at least one of those gaps.

### Step 28 entry criteria (do these before declaring 27 complete)

1. Provision VPC interface endpoints in `ca-central-1`:
   - `com.amazonaws.ca-central-1.ssmmessages`
   - `com.amazonaws.ca-central-1.ssm`
   - `com.amazonaws.ca-central-1.ec2messages`
   Attach to both private subnets used by ECS tasks. Approx cost: ~$22/month per endpoint.
2. Mint a stable platform-admin API key via `scripts/mint_platform_admin_ssm.py` and
   confirm it's stored at `/luciel/production/platform-admin-key` (SecureString).
3. Run `python -m app.verification --mode=full` from inside an exec session against
   `luciel-backend:14`. Require all 11 pillars GREEN, especially Pillar 11.
4. After GREEN: tag `step-27-completed-<YYYYMMDD>` and update this runbook.

### Open risk this leaves on prod

The async memory path is producing rows correctly **as of the IAM fix at ~22:30 EDT**,
inferable because the IAM denial is the only known failure mode and we removed it.
Worst-case risk: a second silent failure mode we haven't identified is also active.
Mitigation: queue-depth admin endpoint and CloudWatch alarms on
`ApproximateAgeOfOldestMessage` will surface backlog within minutes; DLQ
non-zero will surface task-level failures.

### Tag

`step-27c-deployed-20260425` — partial verification, async live, IAM corrected.
**Not** `step-27-completed-*` — that comes after Pillar 11 GREEN.