# Step 28 Phase 1, Commit 8b — `luciel-worker-sg` + prod role mint

**Status**: Awaiting morning execution after Commit 8a (artifacts) lands.
**Estimated time**: 60–90 min including verification.
**Prerequisite**: Commit 7 (`40d9fb8`) merged to `step-28-hardening`,
mint script (`scripts/mint_worker_db_password_ssm.py`) and this runbook
landed via Commit 8a.

## Pre-flight checklist (do these before opening the AWS console)

- [ ] Coffee. Rested. Not coding past midnight.
- [ ] Branch is `step-28-hardening`, working tree clean
      (`git status --short` returns empty).
- [ ] AWS profile authenticated to account `729005488042`,
      region `ca-central-1`. Verify:
      ```powershell
      aws sts get-caller-identity --region ca-central-1
      ```
- [ ] Postgres superuser URL retrieved from password manager into a
      single PowerShell variable (NOT pasted as a CLI arg yet, NOT in
      shell history). Use the `$ADMIN_URL = Read-Host -AsSecureString`
      pattern, then `ConvertFrom-SecureString -AsPlainText` only when
      passing to the mint script.
- [ ] Local verification suite green at HEAD
      (`python -m app.verification` → 16/17 with Pillar 13 pre-existing).
- [ ] `docs/runbooks/step-28-commit-8-luciel-worker-sg.md` (this file)
      open in a separate window. Do not work from memory.

## Recon-locked constants (from 2026-04-30 evening session)

| Value | Constant |
|---|---|
| AWS account | `729005488042` |
| Region | `ca-central-1` |
| Cluster | `luciel-cluster` |
| Worker service | `luciel-worker-service` |
| Backend service | `luciel-backend-service` (NOT touched) |
| Worker task-def family | `luciel-worker` (currently rev `:5`) |
| Migrate task-def family | `luciel-migrate` (currently rev `:10`) |
| Current shared SG | `sg-0f2e317f987925601` (worker + backend; left attached to backend) |
| RDS SG | `sg-05901a66faa636ebb` (`luciel-db-sg`) |
| RDS endpoint | `luciel-db.c3oyiegi01hr.ca-central-1.rds.amazonaws.com:5432` |
| RDS db name | `luciel` |
| ElastiCache SG | `sg-0ccdf4414eaf30989` (`luciel-redis-sg`) |
| VPC endpoint SG | `sg-0b5d426d83fa707c5` (`luciel-vpc-endpoint-sg`, ssm/ssmmessages/ec2messages) |
| Worker subnets | `subnet-0e54df62d1a4463bc`, `subnet-0e95d953fd553cbd1` |
| New SSM path | `/luciel/production/worker_database_url` (env-prefixed, snake_case) |
| Web SSM path (UNTOUCHED) | `/luciel/database-url` |

## Pre-execution: Register `luciel-migrate:11`

This runbook assumes `luciel-migrate:11` is the active migrate task-def
revision. If it is not yet registered, complete **Commit 8b-prereq** first
(see `msg.txt` and the `luciel-migrate:11` task-def in commit
`7560397` (`feat(28): Commit 8b-prereq - fix luciel-migrate task-def`).
The rev11 JSON snapshot was previously committed at the repo root as
`migrate-td-rev11.json` and was removed during the 2026-05-03 evening
repo-hygiene pass; reach for the commit, or re-export via
`aws ecs describe-task-definition --task-definition luciel-migrate:11`.
Pattern N
(`docs/runbooks/operator-patterns.md`) explains the migrate task-def
shape and the historical drift it resolved.

After Commit 8b-prereq lands, proceed with Phase A below.

## Phase A — Create `luciel-worker-sg`

### A.1 Discover the VPC ID (we need it for SG creation)

```powershell
$VPC_ID = aws ec2 describe-security-groups `
  --group-ids sg-0f2e317f987925601 `
  --region ca-central-1 `
  --query "SecurityGroups[0].VpcId" `
  --output text
echo $VPC_ID
```

Expected: a `vpc-xxxxx` ID. Capture it.

### A.2 Create the SG

```powershell
$WORKER_SG = aws ec2 create-security-group `
  --group-name luciel-worker-sg `
  --description "Dedicated SG for luciel-worker-service. Step 28 Phase 1 Commit 8." `
  --vpc-id $VPC_ID `
  --region ca-central-1 `
  --query "GroupId" `
  --output text
echo $WORKER_SG
```

Capture `$WORKER_SG`. **Rollback**: `aws ec2 delete-security-group --group-id $WORKER_SG --region ca-central-1`.

### A.3 Remove the default `0.0.0.0/0` egress rule AWS auto-attaches

```powershell
aws ec2 revoke-security-group-egress `
  --group-id $WORKER_SG `
  --region ca-central-1 `
  --ip-permissions 'IpProtocol=-1,IpRanges=[{CidrIp=0.0.0.0/0}]'
```

### A.4 Add the four explicit egress rules

```powershell
# 443 -> SSM endpoints (SG-to-SG, tightest)
aws ec2 authorize-security-group-egress `
  --group-id $WORKER_SG --region ca-central-1 `
  --ip-permissions "IpProtocol=tcp,FromPort=443,ToPort=443,UserIdGroupPairs=[{GroupId=sg-0b5d426d83fa707c5,Description='SSM/ssmmessages/ec2messages VPC endpoints'}]"

# 443 -> 0.0.0.0/0 (SQS, Secrets Manager, STS — IAM-bounded, no managed PL exists)
aws ec2 authorize-security-group-egress `
  --group-id $WORKER_SG --region ca-central-1 `
  --ip-permissions "IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0,Description='SQS/SecretsManager/STS - IAM-bounded'}]"

# 5432 -> RDS SG (SG-to-SG)
aws ec2 authorize-security-group-egress `
  --group-id $WORKER_SG --region ca-central-1 `
  --ip-permissions "IpProtocol=tcp,FromPort=5432,ToPort=5432,UserIdGroupPairs=[{GroupId=sg-05901a66faa636ebb,Description='RDS luciel-db'}]"

# 6379 -> ElastiCache SG (SG-to-SG)
aws ec2 authorize-security-group-egress `
  --group-id $WORKER_SG --region ca-central-1 `
  --ip-permissions "IpProtocol=tcp,FromPort=6379,ToPort=6379,UserIdGroupPairs=[{GroupId=sg-0ccdf4414eaf30989,Description='ElastiCache luciel-redis'}]"
```

### A.5 Verify

```powershell
aws ec2 describe-security-groups --group-ids $WORKER_SG --region ca-central-1 `
  --query "SecurityGroups[0].{Name:GroupName,Egress:IpPermissionsEgress}" --output json
```

**Expected**: 4 egress rules, no ingress rules. **Rollback**: revoke each rule, then `delete-security-group`.

## Phase B — Three ingress modifications on existing SGs

### B.1 RDS SG: allow 5432 from worker SG

```powershell
aws ec2 authorize-security-group-ingress `
  --group-id sg-05901a66faa636ebb --region ca-central-1 `
  --ip-permissions "IpProtocol=tcp,FromPort=5432,ToPort=5432,UserIdGroupPairs=[{GroupId=$WORKER_SG,Description='luciel-worker-sg'}]"
```

### B.2 ElastiCache SG: allow 6379 from worker SG

```powershell
aws ec2 authorize-security-group-ingress `
  --group-id sg-0ccdf4414eaf30989 --region ca-central-1 `
  --ip-permissions "IpProtocol=tcp,FromPort=6379,ToPort=6379,UserIdGroupPairs=[{GroupId=$WORKER_SG,Description='luciel-worker-sg'}]"
```

### B.3 VPC endpoint SG: allow 443 from worker SG  ← **caught by recon A0k**

```powershell
aws ec2 authorize-security-group-ingress `
  --group-id sg-0b5d426d83fa707c5 --region ca-central-1 `
  --ip-permissions "IpProtocol=tcp,FromPort=443,ToPort=443,UserIdGroupPairs=[{GroupId=$WORKER_SG,Description='luciel-worker-sg'}]"
```

**Rollback per rule**: `aws ec2 revoke-security-group-ingress` with same `--ip-permissions`.

## Phase C — Apply pending migrations to prod RDS

> **Pre-execution requirement (Commit 8b-prereq):** `luciel-migrate:11` must
> be the active task-def revision. Pre-2026-05-01, `luciel-migrate` had
> `command: null` for 10 revisions and silently booted into uvicorn instead
> of running Alembic. See `docs/runbooks/operator-patterns.md` Pattern N.
> Verify before running C.1:
>
> ```powershell
> aws ecs describe-task-definition `
>   --task-definition luciel-migrate `
>   --region ca-central-1 `
>   --query "taskDefinition.containerDefinitions[0].command"
> ```
>
> Expected: `["alembic", "upgrade", "head"]`. If `null`, abort.
>
> **Note:** This phase applies whatever migrations are pending in prod's
> alembic_version chain. The first concrete invocation under Pattern N
> happened in Commit 8b-prereq-data (resolution of
> D-prod-3-migrations-behind-2026-05-01). For future migrations, this
> phase runs cleanly as long as no schema-vs-data drift exists.

### C.1 Run the migrate task

```powershell
$TASK_ARN = aws ecs run-task `
  --cluster luciel-cluster `
  --task-definition luciel-migrate:11 `
  --launch-type FARGATE `
  --network-configuration "awsvpcConfiguration={subnets=[subnet-0e54df62d1a4463bc,subnet-0e95d953fd553cbd1],securityGroups=[sg-0f2e317f987925601],assignPublicIp=ENABLED}" `
  --region ca-central-1 `
  --query "tasks[0].taskArn" --output text
echo "TASK_ARN=$TASK_ARN"
```

Capture `TASK_ARN`.

### C.2 Wait and verify

```powershell
aws ecs wait tasks-stopped --cluster luciel-cluster --tasks $TASK_ARN --region ca-central-1

aws ecs describe-tasks --cluster luciel-cluster --tasks $TASK_ARN --region ca-central-1 `
  --query "tasks[0].{exit:containers[0].exitCode,reason:stoppedReason,lastStatus:lastStatus}" --output json
```

**Expected**: `exit: 0, lastStatus: STOPPED`. CloudWatch `/ecs/luciel-backend`
stream prefix `migrate` should show Alembic upgrade completing through
`f392a842f885`:

```powershell
aws logs tail /ecs/luciel-backend `
  --log-stream-name-prefix migrate `
  --since 10m `
  --region ca-central-1
```

### C.3 Verification (Option alpha - no laptop-direct connection)

**Skipped: no operator-laptop psql connection to prod RDS.** Per Pattern N
(`docs/runbooks/operator-patterns.md`), the verification chain is:

1. **Layer 1**: migrate task `exit: 0` (Alembic transactional - if
   `CREATE ROLE` failed, the transaction rolls back and exit is non-zero).
2. **Layer 2**: Phase D mint script's `ALTER ROLE luciel_worker WITH PASSWORD
   ...` errors immediately with "role does not exist" if the role is
   missing, before any SSM write. This is the role-existence verification
   for free.
3. **Layer 3**: Phase G smoke test (single SQS message) confirms the role's
   grants are correct end-to-end via Pillar 11.

Three independent verification layers, zero laptop-to-prod-RDS connections.
Resolves D-runbook-c3-circular-dependency from Commit 8b-prereq.
## Phase D — Mint password + write SSM

```powershell
python -m scripts.mint_worker_db_password_ssm `
  --admin-db-url $ADMIN_URL `
  --worker-host  "luciel-db.c3oyiegi01hr.ca-central-1.rds.amazonaws.com" `
  --worker-port  5432 `
  --worker-db-name "luciel" `
  --ssm-path     "/luciel/production/worker_database_url" `
  --region       "ca-central-1"
```

**Capture** the `pw_fingerprint` from stdout.

### D.1 Verify SSM (controlled location only, NOT a CloudWatch-logged shell)

```powershell
aws ssm get-parameter `
  --name /luciel/production/worker_database_url `
  --with-decryption --region ca-central-1 `
  --query "Parameter.Value" --output text
```

Cross-check fingerprint: extract password segment from the URL, SHA256 it, compare first 12 hex chars to D's `pw_fingerprint`.

## Phase E — Register `luciel-worker:6`

### E.1 Dump rev 5

```powershell
aws ecs describe-task-definition `
  --task-definition luciel-worker:5 --region ca-central-1 `
  --query "taskDefinition" > worker-td-rev5.json
```

### E.2 Edit `worker-td-rev5.json`

- Strip the auto-populated read-only fields: `taskDefinitionArn`, `revision`, `status`, `requiresAttributes`, `compatibilities`, `registeredAt`, `registeredBy`.
- In `containerDefinitions[0].secrets`, change the `DATABASE_URL` entry's `valueFrom` from `arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/database-url` to `arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/worker_database_url`.
- Save as `worker-td-rev6.json`.

### E.3 Register

```powershell
aws ecs register-task-definition `
  --cli-input-json file://worker-td-rev6.json `
  --region ca-central-1 `
  --query "taskDefinition.taskDefinitionArn" --output text
```

**Expected**: `.../luciel-worker:6`.

## Phase F — Update worker service (atomic: new task-def + new SG)

```powershell
aws ecs update-service `
  --cluster luciel-cluster `
  --service luciel-worker-service `
  --task-definition luciel-worker:6 `
  --network-configuration "awsvpcConfiguration={subnets=[subnet-0e54df62d1a4463bc,subnet-0e95d953fd553cbd1],securityGroups=[$WORKER_SG],assignPublicIp=ENABLED}" `
  --region ca-central-1 `
  --query "service.{status:status,td:taskDefinition,deployments:deployments[].{status:status,rolloutState:rolloutState,desired:desiredCount,running:runningCount}}" `
  --output json
```

**Expected**: a new `PRIMARY` deployment in `IN_PROGRESS` state pointing at `:6`,
plus the old `ACTIVE` deployment on `:5` draining.

### F.1 Watch the deployment events for 3–5 minutes

```powershell
# Re-run every 30s until the new deployment goes COMPLETED and old goes INACTIVE
aws ecs describe-services `
  --cluster luciel-cluster --services luciel-worker-service `
  --region ca-central-1 `
  --query "services.deployments[].{status:status,rolloutState:rolloutState,td:taskDefinition,desired:desiredCount,running:runningCount,failed:failedTasks}" `
  --output table
```

**Expected after ~3 min**: one deployment `PRIMARY / COMPLETED`, pointing at `:6`,
1/1 running, 0 failed.

**Failure mode to watch for**: `failedTasks` increments. That means tasks are
launching but immediately dying. Tail the worker log group:

```powershell
$LOG_GROUP = aws ecs describe-task-definition `
  --task-definition luciel-worker:6 --region ca-central-1 `
  --query "taskDefinition.containerDefinitions.logConfiguration.options.\"awslogs-group\"" `
  --output text

aws logs tail $LOG_GROUP --follow --region ca-central-1 --since 5m
```

Common causes if tasks die immediately:
- SG egress rule missing → DB connection refused. Re-verify Phase A.4.
- VPC endpoint SG ingress rule missing → SSM secret resolution times out. Re-verify Phase B.3.
- SSM param value malformed → DB auth fails. Re-run Phase D.1 to inspect.

## Phase G — Verify under prod load

### G.1 Pillar 11 functional probe (single SQS test message)

Use the existing prod-safe smoke-test pattern: the `step-28-commit-8-smoketest`
test tenant created/torn down inside this phase.

```powershell
# (See app/verification/_smoke_send.py — same primitive used by Step 27c worker deploy)
python -m app.verification._smoke_send `
  --tenant-prefix "step-28-commit-8-smoketest" `
  --message "I prefer Toronto neighbourhoods with TTC access; my budget is $1.2M." `
  --region ca-central-1
```

**Expected**:
- SQS message enqueued, `MessageId` printed.
- Within ~10s, worker logs show: task picked up, AuditContext.worker, MemoryItem INSERT, AdminAuditLog INSERT, transaction committed, task ACK.
- `psql $ADMIN_URL -c "SELECT count(*) FROM memory_items WHERE tenant_id=(SELECT id FROM tenants WHERE name LIKE 'step-28-commit-8-smoketest%')"` returns ≥ 1.

### G.2 Tear down the smoke-test tenant

```powershell
python -m app.verification._smoke_teardown `
  --tenant-prefix "step-28-commit-8-smoketest" `
  --region ca-central-1
```

### G.3 Confirm Pillar 11 still green locally (regression signal)

```powershell
python -m app.verification
```

**Expected**: 16/17 green (Pillar 13 unchanged from Commit 7's known state).

## Phase H — Stage / commit / push Commit 8b

### H.1 Snapshot the as-deployed task-def for the commit

```powershell
git checkout step-28-hardening
git pull --ff-only origin step-28-hardening

# Save a sanitized snapshot of rev 6 for posterity
aws ecs describe-task-definition `
  --task-definition luciel-worker:6 --region ca-central-1 `
  --query "taskDefinition" `
  > docs/runbooks/artifacts/step-28-commit-8b-luciel-worker-td-rev6.json
```

### H.2 Stage and write `msg.txt` for Commit 8b

```powershell
git add docs/runbooks/artifacts/step-28-commit-8b-luciel-worker-td-rev6.json
git status --short
```

(`msg.txt` content for Commit 8b is generated post-execution — it captures
the actual SHAs, fingerprint, and any deviations from this runbook.)

### H.3 Commit and push

```powershell
git commit -F msg.txt
git push origin step-28-hardening
git log -3 --oneline
```

**Capture** the new SHA for Commit 9's drift register row.

## Rollback playbook (any phase)

| Phase | Failed at | Rollback |
|---|---|---|
| A | SG creation | `aws ec2 delete-security-group --group-id $WORKER_SG` |
| A | Egress rules | `revoke-security-group-egress` per rule |
| B | Ingress rules | `revoke-security-group-ingress` per rule, on each of the 3 SGs |
| C | Migration | Connect to RDS, `DROP ROLE luciel_worker` (Commit 7's downgrade is symmetric) |
| D | Mint script | If `ALTER ROLE` succeeded but SSM write failed, re-run with `--force-rotate`. If both succeeded but value is wrong, re-run with `--force-rotate` |
| E | Task-def register | Task-defs are append-only; rev 6 stays registered but unused. No cleanup needed. Or `aws ecs deregister-task-definition --task-definition luciel-worker:6` |
| F | Service update | `aws ecs update-service ... --task-definition luciel-worker:5 --network-configuration "...securityGroups=[sg-0f2e317f987925601]..."` reverts in one call |
| G | Smoke test fails | Roll back F first, then investigate logs. Don't proceed to H if G is red. |

### Full back-out (all phases)

If anything materially wrong surfaces in G:

1. Roll back F (worker service back to `:5` + old SG).
2. Wait for deployment `COMPLETED`, confirm worker is healthy on `:5`.
3. Optionally roll back B (revoke 3 ingress rules) and A (delete worker SG)
   — leaving them in place is harmless if F is reverted.
4. Optionally roll back D (mint script wrote SSM; if param exists,
   `aws ssm delete-parameter --name /luciel/production/worker_database_url`).
5. Phase C's migration stays applied — `luciel_worker` role persists in
   prod with NULL password, which is the safe end state. Drop it only if
   you're abandoning the entire D-worker-role direction.

## Drift / divergence log (fill in during execution)

During execution, capture any deviation from this runbook here.
Examples of expected fill-ins:

- Phase A: SG ID minted = `<sg-xxx>` (capture from A.2)
- Phase E: rev 6 task-def ARN = `<arn>` (capture from E.3)
- Phase D: pw_fingerprint = `<12 hex>` (capture from D's stdout)
- Any AWS API throttle/retry delays, console fallbacks, or step
  reorderings.

These fill-ins become Commit 8b's `msg.txt` body and Commit 9's
plan-vs-reality divergence register.

## Post-execution checklist

- [ ] Commit 8b SHA captured: `_______________`
- [ ] `pw_fingerprint` recorded in password manager (alongside the
      retrieved worker URL): `_______________`
- [ ] Worker rev 5 task-def deregistered? (optional cleanup, decide
      after 24 hr stability window)
- [ ] Drift register additions for Commit 9: `D-no-terraform`,
      `D-sqs-vpc-endpoint`, `D-public-ip-tasks`, `D-rds-cidr-broad`,
      naming corrections (`luciel_worker` underscore, service names),
      plan-vs-reality divergence #3 (T2 split: 8 → 8a + 8b).
- [ ] Phase 1 progress: 9 of 10 commits done. Commit 9 is the only
      remaining item.