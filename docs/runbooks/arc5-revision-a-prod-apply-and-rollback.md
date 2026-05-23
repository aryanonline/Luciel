# Arc 5 Revision A — Prod Apply + Rollback Runbook

**Status:** EXECUTED 2026-05-23 (this runbook captures the executed sequence, not a forward plan).
**Anchor:** CANONICAL_RECAP §17 (Arc 5 Commit 7 entry); ARCHITECTURE §3.2.8 Arc 5 Commit 7 bullet.
**Not a canonical document.** This is a Posture B operational satellite. When this runbook and the three canonical documents (`CANONICAL_RECAP.md`, `ARCHITECTURE.md`, `DRIFTS.md`) disagree on any fact, **canon wins automatically.** Update the satellite to match.

---

## 1. Scope

The Revision A additive migration (`alembic/versions/arc5_a_admin_instance_additive.py`) applies to the production RDS database the Admin → Instance → Lead collapse schema additions: 6 new tables (`admins`, `instances`, `instance_composition_grants`, `knowledge_share_grants`, `admin_tier_overrides`, `metering_emissions`) + `subscriptions.billing_model` column with backfill. Pure additive DDL — no destructive changes. Reversible via the migration's `downgrade()`.

Local smoke-test closure-evidence: CANONICAL §17 Arc 5 Commit 5 entry (six steps passed against fresh pgvector compose container, including a full round-trip cycle).

## 2. Pre-execution state

- Local head before this work: `6749658` (Arc 5 Commit 6 — sandbox prod-control-plane posture established)
- Prod alembic head before apply: `b2e5f17a3d9c` (Arc 8 WU-6 email-suppression migration)
- Pinned image in `luciel-migrate:16`: `sha256:8b5020d7…` — predates Revision A, cannot be reused
- `ecr.describe_images` confirmed no existing ECR image in `luciel-backend` repository carries Revision A code

## 3. Executed sequence

All steps executed by the sandbox-agent process (IAM principal `luciel-sandbox-agent`, ARN `arn:aws:iam::729005488042:user/luciel-sandbox-agent`) on 2026-05-23 between 16:48 and 16:53 UTC.

### 3.1 Build image with Revision A baked in

Built with `buildah` (rootless OCI builder, no Docker daemon required — sandbox provides no `/var/run/docker.sock`):

```
buildah bud \
  --build-arg BUILD_GIT_SHA=6749658 \
  -t luciel-backend:arc5-a-6749658 \
  -f Dockerfile .
```

Pre-push verification (inside built image): `alembic/versions/arc5_a_admin_instance_additive.py` present at `/app/alembic/versions/`, `alembic` script directory reports `HEADS: ['arc5_a_admin_instance_additive']`.

### 3.2 Push to ECR

Push required a one-time scope expansion to `LucielSandboxArc5MigrateScope`. See §6 below for the scope-expansion record (executed under the 5-gate doctrine canonical at `D-prod-credential-scope-expansion-protocol-2026-05-23`).

```
buildah login --username AWS --password "$(aws ecr get-login-password)" \
  729005488042.dkr.ecr.ca-central-1.amazonaws.com
buildah tag localhost/luciel-backend:arc5-a-6749658 \
  729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend:arc5-a-6749658
buildah push 729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend:arc5-a-6749658
```

**Landed digest:** `sha256:b58e7b1ae8ba685a1ff4842c986ca8b0d81e8aa2f8b0bad13848fc6485066804`
**Size:** 223.3 MB
**Pushed at:** 2026-05-23 16:50:24 UTC

### 3.3 Register `luciel-migrate:17`

Lifted the full container definition from `luciel-migrate:16` (taskRole, executionRole, networkMode, secrets, logConfig, cpu, memory all preserved byte-identically) and changed only the `image` field to the new digest from §3.2.

```
ecs.register_task_definition(
  family='luciel-migrate',
  containerDefinitions=[{
    ...rev16 container def...,
    'image': '729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend@sha256:b58e7b1ae8ba685a1ff4842c986ca8b0d81e8aa2f8b0bad13848fc6485066804',
    'command': ['alembic', 'upgrade', 'head'],
  }],
  taskRoleArn='arn:aws:iam::729005488042:role/luciel-ecs-migrate-role',
  executionRoleArn='arn:aws:iam::729005488042:role/luciel-ecs-execution-role',
  networkMode='awsvpc',
  requiresCompatibilities=['FARGATE'],
  cpu='512', memory='1024',
)
```

**Result:** `arn:aws:ecs:ca-central-1:729005488042:task-definition/luciel-migrate:17`

### 3.4 Apply Revision A — first RunTask

```
ecs.run_task(
  cluster='luciel-cluster',
  taskDefinition='luciel-migrate:17',
  launchType='FARGATE', count=1,
  networkConfiguration={'awsvpcConfiguration': {
    'subnets': ['subnet-0e54df62d1a4463bc', 'subnet-0e95d953fd553cbd1'],
    'securityGroups': ['sg-0f2e317f987925601'],
    'assignPublicIp': 'ENABLED',
  }},
  startedBy='arc5-commit7-revA-apply',
)
```

**Task ARN:** `arn:aws:ecs:ca-central-1:729005488042:task/luciel-cluster/7204dee519de4d7da5f6a297ad55de80`
**State progression:** PROVISIONING → PENDING → RUNNING → DEPROVISIONING → STOPPED in 50 seconds wall-clock.
**Container exit code:** 0
**Stop code:** EssentialContainerExited
**CloudWatch log (`/ecs/luciel-backend` stream `migrate/luciel-backend/7204dee519de4d7da5f6a297ad55de80`):**

```
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade b2e5f17a3d9c -> arc5_a_admin_instance_additive, Arc 5 Revision A — additive schema for the Admin → Instance → Lead collapse.
```

The single jump `b2e5f17a3d9c -> arc5_a_admin_instance_additive` landed transactionally (Alembic's "Will assume transactional DDL." line is the proof — Postgres wraps the migration in a transaction so a mid-migration failure would leave the DB at `b2e5f17a3d9c`).

### 3.5 Verify head — second RunTask

```
ecs.run_task(
  cluster='luciel-cluster',
  taskDefinition='luciel-migrate:17',
  launchType='FARGATE', count=1,
  networkConfiguration=<same as §3.4>,
  overrides={'containerOverrides': [{
    'name': 'luciel-backend',
    'command': ['alembic', 'current'],
  }]},
  startedBy='arc5-commit7-revA-verify',
)
```

**Task ARN:** `arn:aws:ecs:ca-central-1:729005488042:task/luciel-cluster/a5b4c27311854301ae650eeaa6ba906a`
**Container exit code:** 0
**CloudWatch log output:**

```
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
arc5_a_admin_instance_additive (head)
```

**Closure evidence: prod alembic head is now `arc5_a_admin_instance_additive`.**

## 4. Rollback procedure (NOT executed; kept for record)

If post-apply verification had revealed a problem, the rollback is a single command override on the same task-def:

```
ecs.run_task(
  cluster='luciel-cluster',
  taskDefinition='luciel-migrate:17',
  launchType='FARGATE', count=1,
  networkConfiguration=<same as §3.4>,
  overrides={'containerOverrides': [{
    'name': 'luciel-backend',
    'command': ['alembic', 'downgrade', 'b2e5f17a3d9c'],
  }]},
  startedBy='arc5-commit7-revA-rollback',
)
```

The migration's `downgrade()` was verified locally at Commit 5 Step 4 (round-trip `upgrade head → downgrade -1 → upgrade head` returned byte-identical schema). Rollback is reversible without data loss because Revision A is purely additive: dropping the 6 new tables + 1 new column reverses the migration completely (no data has been written to the new tables yet at this point in the arc — the application code that writes them is the Revision B work-unit ahead).

## 5. Post-apply state

- Prod alembic head: `arc5_a_admin_instance_additive`
- Prod tables added: `admins`, `instances`, `instance_composition_grants`, `knowledge_share_grants`, `admin_tier_overrides`, `metering_emissions`
- Prod column added: `subscriptions.billing_model VARCHAR(16) NULL` (backfilled `'flat'` for all existing Pro rows in the same migration)
- Application code (backend + worker services running `luciel-backend:82` / `luciel-worker:37`) is **unchanged** at this commit — the application does not yet read or write any of the new tables. Revision B is the work-unit that introduces application-layer code against the new shape; that work lands in subsequent commits and is gated on partner approval per pause-between-commits discipline.

## 6. Scope-expansion record (executed under D-prod-credential-scope-expansion-protocol-2026-05-23)

The pre-Commit-7 inline policy `LucielSandboxArc5MigrateScope` lacked the ECR push action set and `ecs:RegisterTaskDefinition`. The 5-gate procedure was executed:

1. **Necessity.** TODO #13 (Arc 5 Commit 7 / EXECUTION on prod — Revision A apply) cannot be satisfied without ECR push (no existing image carries Revision A) + RegisterTaskDefinition (the new image must be referenced by a new task-def revision).
2. **Minimality.** Two statements added:
   - `EcrPushBackendOnly`: `ecr:BatchCheckLayerAvailability`, `ecr:InitiateLayerUpload`, `ecr:UploadLayerPart`, `ecr:CompleteLayerUpload`, `ecr:PutImage` — `Resource = arn:aws:ecr:ca-central-1:729005488042:repository/luciel-backend` (single repo).
   - `EcsRegisterTaskDef`: `ecs:RegisterTaskDefinition` — `Resource = "*"` because the AWS API does not support resource-level scoping on this action (the IAM service condition key `ecs:family` does **not** exist — verified at attempted-save; AWS IAM Console rejected the family-condition draft with `Invalid Service Condition Key`). The narrow scope-bound is enforced at the execution side instead: every other `ecs:*` action in this policy is family-scoped to `luciel-migrate`, so a sandbox principal that can register an arbitrary task-def family still cannot `RunTask`, `UpdateService`, or `DescribeTasks` on it. The blast radius of an over-broad `RegisterTaskDefinition` here is "writes a task-def row that this principal cannot subsequently execute" — essentially zero (no compute, no data access, no cost).
   - Rejected alternative: build + push from the partner's Windows + Docker box and let the sandbox only register + run. Rejected because it reintroduces the partner-paste-loop the Commit 6 posture shift was designed to eliminate.
3. **Surfacing.** This runbook + the live agent ↔ partner exchange leading into Commit 7 names the four required artifacts: the need (above), the exact JSON (above), the blast radius (essentially zero per above), the alternatives rejected (above).
4. **Approval.** Partner approved the corrected scope draft in-session 2026-05-23 12:50 EDT ("okay I have made the changes, it worked").
5. **Audit-trail.** This commit (Arc 5 Commit 7) updates both this runbook §6 **and** `CANONICAL_RECAP.md` §17 (the sibling Arc 5 Commit 7 entry), per the 5-gate audit-trail rule. The CloudTrail trace of the policy edit on the IAM user is the AWS-side leg of the same audit.

## 7. Discipline rules (carry-forward)

- Future prod-touching commits use this runbook as the template: pre-state record, executed-sequence record with all relevant ARNs / digests / task IDs / log streams, post-state verification, rollback procedure kept for record, scope-expansion record only when scope was expanded.
- This runbook is the single source of operational truth for the Revision A prod apply. If a future arc needs to re-apply or post-verify, this is where to look first.
- The runbook stays inert (no further edits) until either Revision B/C work updates the post-apply state OR a future incident references the apply timeline.
