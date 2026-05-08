# Step 29.y — Prod Cleanup (Cost + Hygiene)

**Date:** 2026-05-08
**Branch:** `step-29y-gapfix`
**Inventory pinned at:** 2026-05-08 ~09:40 EDT (ad-hoc enumeration this session)
**Approach:** Tiered. Tier 1 is safe-tonight. Tier 2 is verify-then-delete. Tier 3 is hardening (additive, not destructive).

## Ground rules

- Every destructive command is preceded by a `describe-*` so we re-confirm at execution time
- Every section says **why** the deletion is safe
- Anything ambiguous is **deferred**, not deleted
- Pattern E (audit hash chain) is NOT in scope here — no DB row deletions ever
- Manual snapshot deletions are scheduled AFTER tonight's prod sync completes (so they remain a rollback option through tonight)

## What is intentionally KEPT

These are correct, in-use, or already-good. Do not touch.

### Already-good observability (good news — was already provisioned in Step 28)
- **`luciel-prod-alarms` CFN stack** — full alarm coverage:
  - `RdsCpuAlarm`, `RdsFreeStorageAlarm`, `RdsConnectionCountAlarm`
  - `WorkerErrorLogRateAlarm`, `WorkerNoHeartbeatAlarm`, `WorkerUnhealthyTaskCountAlarm`
  - `SsmAccessFailureAlarm`
  - SNS topic + email subscription
- **`luciel-prod-worker-autoscaling` CFN stack** — target-tracking worker CPU autoscaling

### Core prod runtime
- `luciel-cluster` (ECS), `luciel-backend-service`, `luciel-worker-service`
- `luciel-backend:32`, `luciel-worker:12` task definitions (current revisions)
- `luciel-prod-ops:1` (the ECS Exec one-shot we just registered)
- `luciel-db` RDS instance + automated daily snapshots (within retention)
- `luciel-redis-0001-001` ElastiCache — both backend and worker have `REDIS_URL` in their secrets contract; treated as in-use
- `luciel-alb` ALB + `luciel-targets` target group + :80/:443 listeners
- `luciel-backend` ECR repo (lifecycle policy added in Tier 1)
- 2 EIPs — these are the ALB's per-AZ public IPs; cannot delete independently
- 2 SQS queues (`luciel-memory-tasks`, `luciel-memory-dlq`) — Celery prod broker
- VPC, subnets, route tables, IGW
- 3 SSM VPC endpoints (ssmmessages, ssm, ec2messages) — required for ECS Exec
- SGs: `luciel-db-sg`, `luciel-vpc-endpoint-sg`, `luciel-worker-sg`, `luciel-ecs-sg`, `luciel-redis-sg`, default
- IAM: `luciel-ecs-execution-role`, `luciel-ecs-web-role`, `luciel-ecs-worker-role`, `luciel-ecs-prod-ops-role` (new), `luciel-ecs-migrate-role`
- All `/luciel/*` SSM parameters

## Tier 1 — Safe tonight (do during prod sync window)

### T1.1 Set CloudWatch log retention

Currently both ECS log groups are "Never expire". Setting 30 days now prevents slow-leak forever. Worker log group is 2 MB today; backend is 17 MB.

```powershell
# Verify current state
aws logs describe-log-groups --log-group-name-prefix /ecs/luciel `
  --query 'logGroups[].{name:logGroupName, retention:retentionInDays, sizeBytes:storedBytes}'

# Apply 30-day retention
aws logs put-retention-policy --log-group-name /ecs/luciel-backend --retention-in-days 30
aws logs put-retention-policy --log-group-name /ecs/luciel-worker --retention-in-days 30

# Re-verify
aws logs describe-log-groups --log-group-name-prefix /ecs/luciel `
  --query 'logGroups[].{name:logGroupName, retention:retentionInDays}'
```

**Why safe:** Logs older than 30 days have no operational value for an actively-iterated product. Audit trail lives in `admin_audit_log` table, not CloudWatch. Container Insights performance log already has 1-day retention (correct).

### T1.2 ECR lifecycle policy

Currently the `luciel-backend` repo has no lifecycle. Images accumulate forever. Add:
- Keep the 10 most recent **tagged** images (rollback window)
- Expire **untagged** images after 7 days

Save as `infra/ecr/luciel-backend-lifecycle-policy.json` in the repo (committed by this segment), then apply:

```powershell
aws ecr put-lifecycle-policy `
  --repository-name luciel-backend `
  --lifecycle-policy-text file://infra/ecr/luciel-backend-lifecycle-policy.json

# Verify
aws ecr get-lifecycle-policy --repository-name luciel-backend
```

**Why safe:** Tagged images we deploy are kept (last 10). Untagged images are build artifacts that ECS no longer references — only orphans get expired. Lifecycle policies do not delete images that are currently referenced by a running task.

### T1.3 Enable RDS deletion protection

```powershell
# Verify current state
aws rds describe-db-instances --db-instance-identifier luciel-db `
  --query 'DBInstances[0].DeletionProtection'

# Enable
aws rds modify-db-instance --db-instance-identifier luciel-db --deletion-protection --apply-immediately

# Re-verify
aws rds describe-db-instances --db-instance-identifier luciel-db `
  --query 'DBInstances[0].DeletionProtection'
```

**Why safe:** Cost = $0. Prevents `aws rds delete-db-instance` accidents. Can be turned off when intentionally retiring the DB.

## Tier 2 — Verify-then-delete (after prod sync, before tagging step-29y-complete)

Each item: describe, then delete (commented out — uncomment to execute).

### T2.1 Deregister INACTIVE task definitions (140 of them)

These are already deregistered. Walking them through `delete-task-definitions` removes them from `INACTIVE` listing entirely. Cost: $0. Cosmetic only.

```powershell
# List INACTIVE
aws ecs list-task-definitions --status INACTIVE --query 'length(taskDefinitionArns)'

# Defer this — purely cosmetic. Skip unless console clutter is bothering you.
```

**Decision:** Skip. Zero cost, low value, scriptable later.

### T2.2 Deregister obsolete ACTIVE task def families

These families have never been used by a service and won't be re-run. They cost $0 but clutter task def UI and audit surface.

#### Verify before deletion

```powershell
# luciel-recon (1 rev)
aws ecs describe-task-definition --task-definition luciel-recon:1 `
  --query 'taskDefinition.containerDefinitions[0].command'
# Expected: a one-off recon command, not anything we run regularly

# luciel-smoke (2 revs)
aws ecs describe-task-definition --task-definition luciel-smoke:2 `
  --query 'taskDefinition.containerDefinitions[0].command'

# luciel-verify (16 revs, last is :17 — note rev 13 missing)
aws ecs describe-task-definition --task-definition luciel-verify:17 `
  --query 'taskDefinition.containerDefinitions[0].command'

# luciel-grant-check (1 rev)
aws ecs describe-task-definition --task-definition luciel-grant-check:4 `
  --query 'taskDefinition.containerDefinitions[0].command'

# luciel-mint (4 revs)
aws ecs describe-task-definition --task-definition luciel-mint:4 `
  --query 'taskDefinition.containerDefinitions[0].command'
# DEFER deletion until after key-rotation in Phase E. Mint script is what rotates the platform admin key.
```

#### Deregister (after verify)

```powershell
# luciel-recon
foreach ($rev in 1) {
  aws ecs deregister-task-definition --task-definition "luciel-recon:$rev" --query 'taskDefinition.taskDefinitionArn' --output text
}

# luciel-smoke
foreach ($rev in 1..2) {
  aws ecs deregister-task-definition --task-definition "luciel-smoke:$rev" --query 'taskDefinition.taskDefinitionArn' --output text
}

# luciel-verify (revs 1..12, 14..17 — note 13 is missing in the listing)
foreach ($rev in 1..12 + 14..17) {
  aws ecs deregister-task-definition --task-definition "luciel-verify:$rev" --query 'taskDefinition.taskDefinitionArn' --output text
}

# luciel-grant-check
aws ecs deregister-task-definition --task-definition "luciel-grant-check:4" --query 'taskDefinition.taskDefinitionArn' --output text

# luciel-mint — DEFER. Run AFTER key rotation tonight.
```

**Why safe:** No service references these task defs (verified above by `describe-services`). Deregistration is reversible by re-registering from the JSON files committed in repo root.

### T2.3 Trim old `luciel-backend` and `luciel-worker` revisions

Keep last 5 of each as rollback window. Deregister the rest.

```powershell
# Backend: keep 28..32, deregister 1..27
foreach ($rev in 1..27) {
  aws ecs deregister-task-definition --task-definition "luciel-backend:$rev" --query 'taskDefinition.taskDefinitionArn' --output text
}

# Worker: keep 8..12, deregister 2..7
foreach ($rev in 2..7) {
  aws ecs deregister-task-definition --task-definition "luciel-worker:$rev" --query 'taskDefinition.taskDefinitionArn' --output text
}

# Migrate: keep last 5 (10..14), deregister 1..9
foreach ($rev in 1..9) {
  aws ecs deregister-task-definition --task-definition "luciel-migrate:$rev" --query 'taskDefinition.taskDefinitionArn' --output text
}
```

**Why safe:** ECS uses task definition revs by ARN. Service is pinned to `:32` (backend) and `:12` (worker). Deregistering older revs does not affect running services. Last 5 retained provides rollback.

### T2.4 Delete obsolete IAM roles

#### Verify what each role does

```powershell
# luciel-apprunner-ecr-role — App Runner is gone (no service exists)
aws iam list-attached-role-policies --role-name luciel-apprunner-ecr-role
aws iam list-role-policies --role-name luciel-apprunner-ecr-role

# luciel-ecs-verify-role — managed by luciel-prod-verify-role CFN stack
aws iam list-attached-role-policies --role-name luciel-ecs-verify-role
aws iam list-role-policies --role-name luciel-ecs-verify-role

# luciel-ecs-mint-role and luciel-mint-operator-role — DEFER. Used in Phase E key rotation.
```

#### Delete `luciel-apprunner-ecr-role`

```powershell
# Detach managed policies (if any)
foreach ($p in (aws iam list-attached-role-policies --role-name luciel-apprunner-ecr-role --query 'AttachedPolicies[].PolicyArn' --output text -split '\s+' | Where-Object {$_})) {
  aws iam detach-role-policy --role-name luciel-apprunner-ecr-role --policy-arn $p
}

# Delete inline policies
foreach ($p in (aws iam list-role-policies --role-name luciel-apprunner-ecr-role --query 'PolicyNames[]' --output text -split '\s+' | Where-Object {$_})) {
  aws iam delete-role-policy --role-name luciel-apprunner-ecr-role --policy-name $p
}

# Delete role
aws iam delete-role --role-name luciel-apprunner-ecr-role
```

#### Delete `luciel-prod-verify-role` CFN stack (which owns `luciel-ecs-verify-role`)

```powershell
aws cloudformation delete-stack --stack-name luciel-prod-verify-role
aws cloudformation wait stack-delete-complete --stack-name luciel-prod-verify-role
```

**Why safe:** `luciel-verify` task family being deregistered in T2.2; nothing else references this role.

### T2.5 Delete obsolete RDS manual snapshots

**DEFER until after tonight's prod sync completes.** Manual snapshots cost ~$0.095/GB-month × 20GB = $1.90/snapshot/month. 4 snapshots = ~$7.60/mo.

After tonight's migration + key rotation are confirmed stable:

```powershell
# Verify before deletion
aws rds describe-db-snapshots --query "DBSnapshots[?SnapshotType=='manual'].{id:DBSnapshotIdentifier, created:SnapshotCreateTime}"

# Delete (run only after Phase F is green and tagged)
aws rds delete-db-snapshot --db-snapshot-identifier luciel-db-pre-step26b-20260420-1940
aws rds delete-db-snapshot --db-snapshot-identifier luciel-db-pre-step27-20260425
aws rds delete-db-snapshot --db-snapshot-identifier luciel-db-pre-step-24-5b-20260426-1321
aws rds delete-db-snapshot --db-snapshot-identifier luciel-db-pre-step-24-5b-20260427-1009
```

**Why safe (after Phase F):** These are pre-step rollback targets for steps 26b, 27, and 24-5b — all closed. Daily automated snapshots cover the active rollback window. Tonight's prod sync gets its own pre-flight snapshot (see T3.4 below).

## Tier 3 — Additive hardening (do tonight)

These don't delete anything. They add safety we currently lack.

### T3.1 Confirm SNS alert email is reachable

```powershell
# Find topic ARN
$TOPIC_ARN = aws cloudformation describe-stack-resource `
  --stack-name luciel-prod-alarms `
  --logical-resource-id AlertTopic `
  --query 'StackResourceDetail.PhysicalResourceId' --output text

aws sns list-subscriptions-by-topic --topic-arn $TOPIC_ARN `
  --query 'Subscriptions[].{endpoint:Endpoint, protocol:Protocol, status:SubscriptionArn}'

# If SubscriptionArn is "PendingConfirmation", the email was never confirmed. Fix it.
# Send a test message:
aws sns publish --topic-arn $TOPIC_ARN --subject "Luciel alert test (Step 29.y close)" --message "If you see this, alerts route correctly."
```

**Why important:** Alarms are useless if no one gets paged. We should verify before closing the step.

### T3.2 Pre-flight RDS snapshot before tonight's migration

This is required for Phase D safety, not optional.

```powershell
$SNAP_ID = "luciel-db-pre-step29y-prod-sync-{0}" -f (Get-Date -Format "yyyyMMdd-HHmm")

aws rds create-db-snapshot `
  --db-instance-identifier luciel-db `
  --db-snapshot-identifier $SNAP_ID

aws rds wait db-snapshot-completed --db-snapshot-identifier $SNAP_ID

aws rds describe-db-snapshots --db-snapshot-identifier $SNAP_ID `
  --query 'DBSnapshots[0].{id:DBSnapshotIdentifier, status:Status, created:SnapshotCreateTime}'
```

**Cost:** ~$1.90/mo until deleted. Plan to delete after one week of stable prod (alongside T2.5 cleanup).

### T3.3 Tag all live resources with `Project=luciel` and `Step=29.y`

So a year from now you can run cost-explorer reports cleanly. Skip if not desired tonight; non-blocking.

### T3.4 Document the auto-snapshot retention window

```powershell
aws rds describe-db-instances --db-instance-identifier luciel-db `
  --query 'DBInstances[0].{backupRetention:BackupRetentionPeriod, backupWindow:PreferredBackupWindow, maintenanceWindow:PreferredMaintenanceWindow}'
```

Capture into runbook so we know how far back automated snapshots cover.

## Execution order tonight

1. **T1.1** (log retention) — 30 seconds, zero risk
2. **T1.3** (deletion protection) — 30 seconds, zero risk
3. **T3.1** (verify alert email) — 1 minute, validates existing observability
4. **T3.2** (pre-flight snapshot) — wait for completion (~3-5 min)
5. **T1.2** (ECR lifecycle) — 30 seconds; do this AFTER tonight's `docker push` so policy applies on next run
6. (Phase A → E from `step-29y-prod-access-2026-05-08.md`)
7. **T2.2, T2.3, T2.4** — after Phase F is green, before tag
8. **T2.5** — defer 7 days to be safe; can do at start of Step 30

## What we're explicitly NOT doing tonight (and why)

- **Multi-AZ RDS:** db.t3.micro single-AZ is appropriate for current scale. Multi-AZ doubles cost. Defer until customer SLA demands it.
- **WAF on ALB:** ~$5/mo + $1/rule. Worth it before public launch. Not worth it during testing. Defer.
- **VPC Flow Logs:** Useful for forensics, not free. Defer.
- **Container Insights extended retention:** 1 day is fine for now.
- **AWS Backup vault:** Native RDS automated snapshots cover us. Defer.
- **Pattern N column rewrite:** Standing rule, not in scope tonight.
