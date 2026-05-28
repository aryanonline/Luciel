# Arc 11 — Production Deploy Checklist (Step 11)

Copy-pasteable runbook for the founder-driven Arc 11 deploy.
Read end-to-end before starting; each step lists its rollback
procedure inline.

**Pre-conditions:**

- Local repo on `arc11/audit` branch (or whatever branch contains
  the latest Arc 11 close-audit artifacts).
- AWS credentials configured for the `729005488042` account,
  region `ca-central-1`.
- `aws`, `python3`, `alembic`, `git`, `gh` (GitHub CLI) on PATH.

**Final state at end of checklist:**

- All 9 Arc 11 branches merged to `main` (backend + frontend).
- CFN stack `luciel-knowledge-bucket` deployed.
- SSM parameter `/luciel/production/knowledge_s3_bucket` stamped.
- Worker ECS service running task-def `luciel-worker:rev34-arc11`.
- Alembic head at `arc11_d3_hnsw_index_chunks` in prod RDS.
- Smoke probe `GET /admin/forensics/knowledge_pipeline_probe_arc11`
  returns all five booleans `True`.
- `knowledge_retrieval_enabled` remains `False`. **Arc 14 owns the
  flip.**

---

## Step 11.1 — Merge the 9 backend branches to main

**Branch order (each gates on the previous merging first):**

```
arc11/a-schema          → main
arc11/b-rename          → main
arc11/c-repository      → main
arc11/d-rls-hnsw        → main
arc11/e-trace-source-ids → main
arc11/f-embed-worker    → main
arc11/g-api             → main
arc11/h-orchestrator    → main
arc11/audit             → main  (this branch)
```

**Two acceptable strategies:**

| Strategy | Pros | Cons |
|---|---|---|
| **9 sequential PRs** | Each step is reviewable independently. Easy to bisect a regression to one step. | 9 PRs to review; merge conflicts if `main` advances between merges. |
| **Single mega-PR** (squash 9 commits) | One review. No merge-conflict windows. | Loses per-step commit history (the commit messages chain the carry-forward narrative). |

**Recommended:** 9 sequential PRs IF the reviewer has bandwidth.
Single mega-PR otherwise. Either works; **no behavioral difference
in prod state.**

Commands per PR (sequential strategy):

```bash
# For each branch in order:
gh pr create \
    --base main \
    --head arc11/a-schema \
    --title "arc11(a): knowledge_sources table + additive FK" \
    --body "$(git log -1 --format=%B arc11/a-schema)"
# Wait for review + merge, then:
git checkout main && git pull
gh pr create --base main --head arc11/b-rename --title "..."
# etc.
```

**Rollback:** Revert the PR with `gh pr revert <pr-number>`. For
the schema migrations, also run `alembic downgrade -1` against the
prod RDS — but only if no production rows depend on the dropped
schema. At Arc 11 close, all branches are touching a 0-row
`knowledge_chunks` table; rollback is cheap.

---

## Step 11.2 — Run the close-audit locally

```bash
cd /path/to/Luciel
DATABASE_URL='postgresql+psycopg://x:x@localhost/x' \
MODERATION_PROVIDER=null \
python scripts/arc11_close_audit.py
echo "exit code: $?"
```

**Expected:** 13 PASS, 0 FAIL, 1 SKIP. The single SKIP is the RLS
live-DB check (re-runs with `--live` once `LUCIEL_LIVE_POSTGRES_URL`
is set after Step 11.7 deploys the migration).

**Failure mode:** any FAIL aborts the deploy. Read the detail
column, fix the divergence, re-run.

---

## Step 11.3 — Deploy the CFN stack

```bash
aws cloudformation deploy \
    --template-file cfn/knowledge-bucket.yaml \
    --stack-name luciel-knowledge-bucket \
    --capabilities CAPABILITY_NAMED_IAM \
    --region ca-central-1
```

`CAPABILITY_NAMED_IAM` is required because the managed policy
carries an explicit `ManagedPolicyName`.

**Verify:**

```bash
aws s3 ls s3://luciel-knowledge-prod-ca-central-1 --region ca-central-1
# (empty list — bucket exists; no objects yet)

aws iam get-policy --policy-arn arn:aws:iam::729005488042:policy/luciel-knowledge-bucket-access
# (returns the managed policy attached to both task roles)
```

**Rollback:**

```bash
aws cloudformation delete-stack \
    --stack-name luciel-knowledge-bucket \
    --region ca-central-1
```

The bucket has `DeletionPolicy: Retain` and
`UpdateReplacePolicy: Retain`, so deleting the stack leaves the
bucket in place. If you also want to remove the bucket, do it
manually via `aws s3 rb s3://... --force` AFTER confirming there are
no objects you care about.

---

## Step 11.4 — Stamp the SSM parameter

```bash
aws ssm put-parameter \
    --name /luciel/production/knowledge_s3_bucket \
    --value luciel-knowledge-prod-ca-central-1 \
    --type String \
    --region ca-central-1
```

**Verify:**

```bash
aws ssm get-parameter \
    --name /luciel/production/knowledge_s3_bucket \
    --region ca-central-1 \
    --query 'Parameter.Value' --output text
# luciel-knowledge-prod-ca-central-1
```

**Rollback:**

```bash
aws ssm delete-parameter \
    --name /luciel/production/knowledge_s3_bucket \
    --region ca-central-1
```

---

## Step 11.5 — Register the new ECS task-def

```bash
aws ecs register-task-definition \
    --cli-input-json file://td-worker-rev34-arc11.json \
    --region ca-central-1
```

**Verify:**

```bash
aws ecs describe-task-definition \
    --task-definition luciel-worker \
    --region ca-central-1 \
    --query 'taskDefinition.revision'
# 34 (or higher if subsequent revisions land)
```

**Rollback:** ECS task-defs are immutable revisions — there's
nothing to "delete." Step 11.6 below can roll the service back to
rev33.

---

## Step 11.6 — Update the worker service

```bash
aws ecs update-service \
    --cluster luciel-cluster \
    --service luciel-worker-service \
    --task-definition luciel-worker \
    --force-new-deployment \
    --region ca-central-1
```

`--force-new-deployment` ensures the running tasks recycle even
if the task-def family pinned the same `:latest` revision tag.

**Verify:**

```bash
aws ecs describe-services \
    --cluster luciel-cluster \
    --services luciel-worker-service \
    --region ca-central-1 \
    --query 'services[0].deployments[?status==`PRIMARY`].taskDefinition'
# arn:aws:ecs:ca-central-1:729005488042:task-definition/luciel-worker:34
```

Then watch the worker logs for the new `KNOWLEDGE_S3_BUCKET` env var
being read on boot:

```bash
aws logs tail /ecs/luciel-worker --since 5m --follow --region ca-central-1
# Look for "healthcheck heartbeat: initial touch" + no env-related errors.
```

**Rollback:**

```bash
aws ecs update-service \
    --cluster luciel-cluster \
    --service luciel-worker-service \
    --task-definition luciel-worker:33 \
    --force-new-deployment \
    --region ca-central-1
```

---

## Step 11.7 — Run alembic migrations against prod RDS

Use the existing migration runner pattern. Arc 9 / Arc 10 used a
one-shot ECS task (`td-prod-ops-rev3.json` style) that exec'd
`alembic upgrade head` from inside the prod VPC. Pattern recap:

```bash
# Register the prod-ops one-shot task-def (existing pattern).
aws ecs register-task-definition \
    --cli-input-json file://td-prod-ops-rev3.json \
    --region ca-central-1

# Run alembic upgrade head inside the prod VPC.
aws ecs run-task \
    --cluster luciel-cluster \
    --task-definition luciel-prod-ops \
    --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[subnet-xxxxx],securityGroups=[sg-xxxxx],assignPublicIp=DISABLED}" \
    --overrides '{"containerOverrides":[{"name":"luciel-prod-ops","command":["alembic","upgrade","head"]}]}' \
    --region ca-central-1
```

Then exec into the task with `aws ecs execute-command` and verify:

```bash
aws ecs execute-command --cluster luciel-cluster --task <task-id> \
    --container luciel-prod-ops \
    --command "alembic heads" \
    --interactive --region ca-central-1
# Expected: arc11_d3_hnsw_index_chunks (head)
```

**Verify the new tables + columns exist:**

```sql
-- inside the same exec session, via psql:
\d knowledge_sources
\d knowledge_chunks
SELECT column_name FROM information_schema.columns
 WHERE table_name='traces' AND column_name='source_ids_used';
```

**Verify RLS:**

```sql
SELECT policyname FROM pg_policies
 WHERE tablename='knowledge_sources';
-- Expected: knowledge_sources_admin_isolation,
--           knowledge_sources_admin_isolation_write
```

**Re-run the close-audit with `--live`** to flip the SKIP to PASS:

```bash
LUCIEL_LIVE_POSTGRES_URL=<prod-url-via-bastion> \
python scripts/arc11_close_audit.py --live
# Expected: 14 PASS, 0 FAIL, 0 SKIP.
```

**Rollback:**

```bash
# Inside the prod-ops exec session:
alembic downgrade arc11_b_rename_embeddings_to_chunks
# (rolls back HNSW + RLS + post-rename policy verify)
# Then if needed:
alembic downgrade arc11_a_knowledge_sources_schema
# (rolls back the rename)
alembic downgrade arc10_5_drop_dead_config_id_columns
# (rolls back the new schema entirely — 0 rows lost since prod is 0-row)
```

---

## Step 11.8 — Hit the knowledge-pipeline smoke probe

```bash
# Acquire a platform_admin session cookie first (existing operator
# pattern; see Arc 9 operator runbook).
curl https://api.vantagemind.ai/api/v1/admin/forensics/knowledge_pipeline_probe_arc11 \
    -H "Cookie: $LUCIEL_PLATFORM_ADMIN_SESSION" \
    -H "Accept: application/json" \
    | jq .
```

**Expected response:**

```json
{
  "celery_task_registered": true,
  "knowledge_queue_wired": true,
  "knowledge_bucket_resolved": true,
  "source_repository_constructible": true,
  "retriever_importable": true,
  "detail": {
    "task_name": "app.worker.tasks.embed_source.embed_source",
    "predefined_queues_present": ["luciel-knowledge-dlq", "luciel-knowledge-tasks", "luciel-memory-dlq", "luciel-memory-tasks"],
    "bucket_resolved_value": "luciel-knowledge-prod-ca-central-1"
  }
}
```

**Any `false` in the booleans aborts the deploy.** Re-check the
corresponding step (11.5/6 for queue, 11.4 for bucket, 11.7 for
repo / retriever).

---

## Step 11.9 — Merge the frontend to main

In the **Luciel-Website** repo:

```bash
cd /path/to/Luciel-Website
gh pr create --base main --head arc11/knowledge-base-ui \
    --title "arc11: Knowledge Base UI in the Configure tab" \
    --body "$(git log -1 --format=%B arc11/knowledge-base-ui)"
```

Once merged, Amplify auto-deploys. Verify the Knowledge section
renders on `/dashboard/luciels/:pk`'s Configure tab.

**Rollback:** Revert the PR; Amplify re-deploys the previous
build. The route mounts a no-op section if `KnowledgeSection`
import fails, so even a half-rollback degrades gracefully.

---

## Step 11.10 — Verify the final-state invariants

Run the close-audit one more time against the deployed system.
Expected to pass without any SKIP:

```bash
LUCIEL_LIVE_POSTGRES_URL=<prod-url> \
python scripts/arc11_close_audit.py --live
# 14 PASS, 0 FAIL, 0 SKIP.
```

**Specifically verify:**

1. `settings.knowledge_retrieval_enabled == False` in the deployed
   backend (curl `/api/v1/version` and grep for the deployed git SHA;
   the env var is locked to `False` at build time).
2. `traces` table is being written to as before (worker emits trace
   rows on every chat turn — verify via a CloudWatch query).
3. Smoke probe still returns five `true` booleans.
4. No customer-visible behavior change. The retriever's not running;
   the UI shows the Knowledge section but uploads silently land at
   `pending` and stay there (the worker IS processing them — verify
   by checking `knowledge_sources.ingestion_status` transitions in
   the DB).

---

## Post-deploy follow-ups

These do **not** block the deploy; they live on the founder's
backlog:

1. Rotate the long-lived IAM key per
   [`SECURITY_FOLLOWUPS.md`](./SECURITY_FOLLOWUPS.md) SF1.
2. Rotate the `luciel_app` DB password per SF2.
3. Plan the post-Arc-11 cleanup PR (items #1-#5 in
   [`CLEANUP_CANDIDATES.md`](./CLEANUP_CANDIDATES.md)) for the
   Arc-11-to-Arc-12 gap.
4. Update VISION_v2 / ARCHITECTURE_v2 with the doctrine clarifications
   in [`DRIFT_FROM_DOCTRINE.md`](./DRIFT_FROM_DOCTRINE.md).
