# Step 29.y — Prod Access via ECS Exec One-Shot Task

**Date:** 2026-05-08
**Branch:** `step-29y-gapfix`
**Pinned commit:** `813e493` (C18, 25/25 FULL)
**Approach:** ECS Exec into one-shot Fargate task in `luciel-cluster`. Zero standing infrastructure, reuses prod network and IAM posture, dies when done.

## Why this approach

- No EC2 bastion to patch, key-rotate, or forget about
- Reuses `luciel-backend` image so `psql`, `python -m alembic`, and the app's own scripts are already available
- Same VPC, subnets, and security groups as the running services — if the app can reach RDS, this task can
- SSM VPC endpoints already exist in `luciel-vpc` (sg `sg-0b5d426d83fa707c5`), so ECS Exec works without NAT
- Every exec session is logged via SSM session manager (CloudWatch)
- Cost: ~$0.01 for the lifetime of one task

## Inputs

| Item | Value |
|------|-------|
| AWS Account | 729005488042 |
| Region | ca-central-1 |
| Cluster | luciel-cluster |
| RDS endpoint | luciel-db.c3oyiegi01hr.ca-central-1.rds.amazonaws.com |
| ECR image (backend) | 729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend@sha256:933a141ad5d5b617d2d134bb9eb2c1d934b65a84ea32bd837f4c698bb3c2d87f |
| Subnets (one-shot task) | subnet-0e54df62d1a4463bc, subnet-0e95d953fd553cbd1 |
| Security group | sg-0f2e317f987925601 (luciel-ecs-sg) |
| Execution role | arn:aws:iam::729005488042:role/luciel-ecs-execution-role |
| Task role (new) | arn:aws:iam::729005488042:role/luciel-ecs-prod-ops-role |

## Phase A — Provision IAM and Task Definition

### A.1 Create the prod-ops task role

```powershell
aws iam create-role `
  --role-name luciel-ecs-prod-ops-role `
  --assume-role-policy-document file://infra/iam/luciel-ecs-prod-ops-role-trust-policy.json

aws iam put-role-policy `
  --role-name luciel-ecs-prod-ops-role `
  --policy-name luciel-ecs-prod-ops-inline `
  --policy-document file://infra/iam/luciel-ecs-prod-ops-role-permission-policy.json

aws iam get-role --role-name luciel-ecs-prod-ops-role --query 'Role.Arn' --output text
```

**Expected output:** `arn:aws:iam::729005488042:role/luciel-ecs-prod-ops-role`

### A.2 Allow `luciel-ecs-execution-role` to read the SSM parameters this task needs

The execution role pulls `secrets:` at task start. It already reads `/luciel/database-url` for backend, so this should be a no-op. Verify:

```powershell
aws iam list-role-policies --role-name luciel-ecs-execution-role
aws iam list-attached-role-policies --role-name luciel-ecs-execution-role
```

If the inline/managed policies do not already grant `ssm:GetParameters` on `/luciel/database-url`, stop and resolve before continuing — backend wouldn't be running otherwise, but verify.

### A.3 Register the prod-ops task definition

```powershell
aws ecs register-task-definition `
  --cli-input-json file://prod-ops-td-rev1.json `
  --query 'taskDefinition.taskDefinitionArn' --output text
```

**Expected output:** `arn:aws:ecs:ca-central-1:729005488042:task-definition/luciel-prod-ops:1`

### A.4 Run the task with execute-command enabled

```powershell
aws ecs run-task --cli-input-json file://run-task-prod-ops.json `
  --query 'tasks[0].taskArn' --output text
```

Capture the task ARN. Wait for status RUNNING:

```powershell
$TASK_ARN = "<paste from above>"

aws ecs wait tasks-running --cluster luciel-cluster --tasks $TASK_ARN

aws ecs describe-tasks --cluster luciel-cluster --tasks $TASK_ARN `
  --query 'tasks[0].{lastStatus:lastStatus, healthStatus:healthStatus, executeCommandAgentLastStatus:containers[0].managedAgents[?name==`ExecuteCommandAgent`].lastStatus | [0]}'
```

`executeCommandAgentLastStatus` must be `RUNNING` before exec works. Allow ~30 seconds after `lastStatus: RUNNING`.

### A.5 Drop into a shell

```powershell
aws ecs execute-command `
  --cluster luciel-cluster `
  --task $TASK_ARN `
  --container luciel-prod-ops `
  --command "/bin/bash" `
  --interactive
```

You are now inside a container with `DATABASE_URL` set and the app code at `/app`.

## Phase B — Prod Audit (read-only, BEFORE any writes)

Once inside the container:

```bash
# Sanity: who am I, what time, what version
hostname && date -u && cd /app && git rev-parse HEAD 2>/dev/null || echo "no git in image (expected)"

# DB reachability
python -c "import os, sqlalchemy as sa; e = sa.create_engine(os.environ['DATABASE_URL']); print(e.connect().execute(sa.text('select version(), current_database(), current_user, now()')).fetchone())"

# Alembic head + current
python -m alembic current
python -m alembic heads

# Recent audit activity (last 50 rows, last 24h)
python -c "
import os, sqlalchemy as sa
e = sa.create_engine(os.environ['DATABASE_URL'])
with e.connect() as c:
    rows = c.execute(sa.text(\"select id, action, actor_user_id, created_at from admin_audit_log where created_at > now() - interval '24 hours' order by id desc limit 50\")).fetchall()
    for r in rows: print(r)
"

# Confirm leaked platform admin key still active
python -c "
import os, sqlalchemy as sa
e = sa.create_engine(os.environ['DATABASE_URL'])
with e.connect() as c:
    rows = c.execute(sa.text(\"select id, prefix, is_active, created_at, deactivated_at from api_keys where key_type = 'platform_admin' order by id desc limit 5\")).fetchall()
    for r in rows: print(r)
"
```

**Stop conditions** (do NOT proceed to Phase C/D/E if any of these):
- `alembic current` shows a head we don't recognize → stop, investigate
- Audit log has rows with timestamps in the migration cutoff window (`< 2026-05-08 04:00:00+00 UTC`) that look anomalous → stop
- Platform admin key list shows multiple active keys we don't expect → stop

## Phase C — Code Deploy (build/push from 813e493)

(Performed from operator machine, not from inside the ops task. Documented separately in `step-29y-deploy-2026-05-08.md`.)

## Phase D — Migration to prod RDS

From inside the ops task:

```bash
# Dry-run first: show pending migrations
python -m alembic history --verbose | head -30
python -m alembic current

# Apply
python -m alembic upgrade head

# Verify
python -m alembic current
```

The migration `d8e2c4b1a0f3` is forward-only with cutoff `2026-05-08 04:00:00+00 UTC`. If audit rows in Phase B revealed traffic past that cutoff that should have been migrated, STOP and reassess before running upgrade.

## Phase E — Platform admin key rotation (DISC-2026-002)

From inside the ops task:

```bash
# Mint new key, write SecureString to SSM, mark old key inactive in DB (single transaction)
python -m scripts.mint_platform_admin_ssm --ssm-write --deactivate-current
```

(Script extension covered in `step-29y-deploy-2026-05-08.md`. The April 27 rotation used `ssm_write=True`; the `--deactivate-current` flag is added in this segment.)

## Cleanup — Stop the ops task

After Phase B/D/E:

```powershell
aws ecs stop-task --cluster luciel-cluster --task $TASK_ARN --reason "prod-ops session complete"
```

The task will stop. The task definition `luciel-prod-ops:1` and the role `luciel-ecs-prod-ops-role` remain — leave them, they are zero-standing-cost and we'll reuse them next time prod access is needed.

## Audit trail

- Every `aws ecs execute-command` session is logged by SSM (CloudTrail event `StartSession`)
- Every command run inside the container appears in `/ecs/luciel-backend` log group with stream prefix `prod-ops`
- All write actions (Phase D, Phase E) emit `admin_audit_log` rows with `actor_user_id` set to the ops session

## Rollback / break-glass

If the ops task is stuck, exec is broken, or RDS is unreachable from the task:
1. `aws ecs stop-task` immediately
2. Re-run from A.4 with a fresh task — they're disposable
3. If SSM endpoints are the issue, check `sg-0b5d426d83fa707c5` (luciel-vpc-endpoint-sg) ingress allows the ops task SG (`sg-0f2e317f987925601`)
