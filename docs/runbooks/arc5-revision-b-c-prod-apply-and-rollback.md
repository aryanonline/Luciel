# Arc 5 Revisions B + C — Prod Apply + Rollback Runbook

**Status:** PLANNED for 2026-05-23 sandbox-driven execution. This runbook captures the protocol immediately ahead of execution; PROD replay sections will be filled in as Commits 20-25 land.
**Anchor:** CANONICAL_RECAP §17 (Arc 5 Commits 17, 18 entries); ARCHITECTURE §3.2.8 Arc 5 entries.
**Not a canonical document.** Posture B operational satellite. When this runbook and the three canonical documents (`CANONICAL_RECAP.md`, `ARCHITECTURE.md`, `DRIFTS.md`) disagree on any fact, **canon wins automatically.** Update the satellite to match.

Companion: `arc5-revision-a-prod-apply-and-rollback.md` (Revision A executed 2026-05-23 16:48-16:53 UTC).

---

## 1. Scope

Two forward-only Alembic migrations to apply against production RDS in a single coordinated window:

| Revision | File | Posture | Reversibility |
| --- | --- | --- | --- |
| `arc5_b_admin_instance_cutover` | `alembic/versions/arc5_b_admin_instance_cutover.py` | DATA backfill; legacy tables untouched | `downgrade()` truncates `admins`/`instances`. Snapshot-based rollback preferred. |
| `arc5_c_admin_instance_subtractive` | `alembic/versions/arc5_c_admin_instance_subtractive.py` | DROP legacy tables, repoint FKs, drop back-pointers, tighten tier CHECK | **Forward-only.** `downgrade()` raises `NotImplementedError`. Recovery = RDS snapshot. |

Revisions A + B + C together complete the Admin → Instance → Lead tenancy collapse the partner authorized in this sprint.

**Local smoke-test closure-evidence:** sandbox has no docker daemon and no pgvector container. The pre-prod smoke is intrinsic to the Revision B / C design: both migrations are fully introspection-based (Revision B reads `tenant_configs` / `luciel_instances` and writes `admins` / `instances`; Revision C drops via `information_schema` discovery for the one PG-auto-named FK) and have been authored against the live PROD schema fingerprint already captured by Revision A's executed `ecr.describe_images` + post-A `alembic current` probe. We defer wet-smoke to the PROD replay itself, gated by per-step snapshots.

## 2. Pre-execution state (to be confirmed at apply time)

- Local head before this work: `2b3aa76` (Commit 18 — Revision C migration + ORM FK repoint)
- Expected prod alembic head before apply: `arc5_a_admin_instance_additive` (set by Revision A executed 2026-05-23)
- Sandbox-agent ARN: `arn:aws:iam::729005488042:user/luciel-sandbox-agent`
- ECR repo: `729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend`
- Migrate task family: `luciel-migrate` (current rev `:17` predates Revisions B/C)
- Backend service tasks: `luciel-backend:82` and `luciel-worker:37`
- RDS instance: identified via boto3 `rds.describe_db_instances` at apply time; DB URL pulled from SSM `/luciel/database-url`

## 3. Planned execution sequence

All steps run by the sandbox-agent process. Each numbered substep is a commit boundary in the local git history (Commits 20-25 land per-step) and a snapshot boundary in RDS.

### 3.1 Build image with Revisions B + C baked in (Commit 20)

Build with `buildah` (rootless OCI builder; sandbox has no Docker daemon):

```
buildah bud \
  --build-arg BUILD_GIT_SHA=2b3aa76 \
  -t luciel-backend:arc5-bc-2b3aa76 \
  -f Dockerfile .
```

Pre-push verification inside built image:

* `alembic/versions/arc5_b_admin_instance_cutover.py` present at `/app/alembic/versions/`
* `alembic/versions/arc5_c_admin_instance_subtractive.py` present at `/app/alembic/versions/`
* `alembic script_directory` reports `HEADS: ['arc5_c_admin_instance_subtractive']`
* `python -c "import app"` exits 0

### 3.2 Push to ECR (Commit 20)

Requires `LucielSandboxArc5MigrateScope` IAM bindings already in place from Revision A. If they were revoked, the 5-gate scope-expansion protocol re-applies — see `arc5-revision-a-prod-apply-and-rollback.md` §6.

```
buildah login --username AWS --password "$(boto3 ecr get-login-password)" \
  729005488042.dkr.ecr.ca-central-1.amazonaws.com
buildah tag localhost/luciel-backend:arc5-bc-2b3aa76 \
  729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend:arc5-bc-2b3aa76
buildah push  729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend:arc5-bc-2b3aa76
```

Record the resulting image digest (sha256) immediately in the commit message.

### 3.3 RDS snapshot — pre-B (Commit 21)

```python
rds.create_db_snapshot(
    DBSnapshotIdentifier='luciel-arc5-pre-revision-b-<UTC-yyyymmddhhmm>',
    DBInstanceIdentifier=<DBInstanceIdentifier>,
)
```

Wait until `Status == 'available'` via `rds.describe_db_snapshots`. Do NOT proceed otherwise.

### 3.4 ECS rolling deploy with the new image (Commit 22)

Register new task-definition revisions for `luciel-backend` and `luciel-worker` pointing at `…/luciel-backend:arc5-bc-2b3aa76`, then `ecs.update_service` rolling them out. Wait for `desiredCount == runningCount` on both services. The app continues to run against the V1 schema (legacy tables still present) — Revision A backed code already tolerates this, and Revision B has not yet run.

### 3.5 Apply Revision B (Commit 23)

Register a one-shot `luciel-migrate:18` task definition pointing at the new image with command `['alembic', 'upgrade', 'arc5_b_admin_instance_cutover']`, then `ecs.run_task`. Wait for exit code 0. Post-condition: `alembic current` == `arc5_b_admin_instance_cutover`.

Then take a second snapshot:

```python
rds.create_db_snapshot(
    DBSnapshotIdentifier='luciel-arc5-post-revision-b-<UTC-yyyymmddhhmm>',
    DBInstanceIdentifier=<DBInstanceIdentifier>,
)
```

Run the **Revision B sanity probe** (read-only psql via boto3 SSM session):

```sql
SELECT COUNT(*) FROM admins;        -- expect 23
SELECT COUNT(*) FROM instances;     -- expect 1 (sole active luciel_instance)
SELECT DISTINCT tier FROM admins;   -- expect subset of {free, pro, enterprise}
SELECT COUNT(*) FROM audit_log
 WHERE event_type = 'LEGACY_FIXTURE_PURGED';  -- expect non-zero
```

If any sanity check fails, **abort** — restore from `luciel-arc5-pre-revision-b-*` snapshot, follow §4 rollback.

### 3.6 Apply Revision C (Commit 24)

Register `luciel-migrate:19` with command `['alembic', 'upgrade', 'head']` and `ecs.run_task`. Wait for exit code 0. Post-condition: `alembic current` == `arc5_c_admin_instance_subtractive`.

Take the third snapshot:

```python
rds.create_db_snapshot(
    DBSnapshotIdentifier='luciel-arc5-post-revision-c-<UTC-yyyymmddhhmm>',
    DBInstanceIdentifier=<DBInstanceIdentifier>,
)
```

### 3.7 V2 schema verification probe (Commit 25)

Read-only psql probe asserting the V2 end-state:

```sql
-- Legacy tables gone
SELECT table_name FROM information_schema.tables
 WHERE table_name IN
   ('agents','agent_configs','domain_configs','luciel_instances','tenant_configs');
-- Expect 0 rows.

-- V2 tier CHECK tightened
SELECT pg_get_constraintdef(oid)
  FROM pg_constraint
 WHERE conname = 'ck_admins_tier_valid';
-- Expect: CHECK ((tier = ANY (ARRAY['free'::text,'pro'::text,'enterprise'::text])))

-- Transitional CHECK gone
SELECT 1 FROM pg_constraint
 WHERE conname = 'ck_admins_tier_valid_during_migration';
-- Expect 0 rows.

-- V2 FKs landed
SELECT conname FROM pg_constraint
 WHERE conname IN (
   'fk_api_keys_luciel_instance_id',
   'fk_knowledge_embeddings_luciel_instance_id',
   'fk_memory_items_luciel_instance_id',
   'fk_conversations_tenant_id_admins',
   'fk_identity_claims_tenant_id_admins',
   'fk_scope_assignments_tenant_id_admins',
   'fk_user_invites_tenant_id_admins'
 );
-- Expect 7 rows.

-- Back-pointer columns gone
SELECT column_name FROM information_schema.columns
 WHERE (table_name='admins'    AND column_name='legacy_tenant_id')
    OR (table_name='instances' AND column_name IN
          ('legacy_luciel_instance_id','legacy_agent_id'));
-- Expect 0 rows.
```

Then immediate read-path smoke against the live app (no writes): one `GET /api/healthz`, one `GET /api/admin/whoami` with a known admin API key, one widget-side `GET /api/conversations?tenant_id=<admin.id>` returning 200.

## 4. Rollback paths

| Failure point | Rollback action |
| --- | --- |
| §3.4 ECS rolling deploy stuck | `ecs.update_service` back to the previously running task-def revisions (`luciel-backend:82` / `luciel-worker:37`). Schema unchanged. |
| §3.5 Revision B fails or sanity probe fails | `rds.restore_db_instance_from_db_snapshot` from `luciel-arc5-pre-revision-b-*`. Promote restored instance. Re-deploy ECS with prior image. |
| §3.6 Revision C fails | `rds.restore_db_instance_from_db_snapshot` from `luciel-arc5-post-revision-b-*`. Schema returns to Revision B end-state (legacy tables still present, FKs still on legacy targets). |
| §3.7 verification probe fails after Revision C ran clean | Likely an app-layer regression, not a schema problem. Roll back ECS image only (§3.4 procedure). Investigate before re-deploying. |

Revision C is forward-only by design. There is **no Alembic-level downgrade path** from `arc5_c_admin_instance_subtractive` — `downgrade()` raises `NotImplementedError` to make this explicit. Recovery is always via RDS snapshot.

## 5. Post-execution closure

Once §3.7 passes:

* Update CANONICAL_RECAP §17 with the Commit 23, 24, 25 entries (image digest, snapshot IDs, alembic-current evidence).
* Update ARCHITECTURE §3.2.8 with the Arc 5 closure bullet.
* Update DRIFTS.md — strike through every DRIFT row about scope_level / scope_owner_* / Domain / Agent / V1 tenancy; they are now closed.
* Author `docs/_session_notes/arc-5-execution-record.md` with the full Commit 17-26 timeline.
* Git tag `arc-5-tenancy-collapse-complete` on `main` HEAD.

## 6. Credential posture

Prod credentials are sandbox-agent env vars only:

* `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION` — never written to disk, never committed, never echoed to logs, never passed to subagents.
* DB URL pulled from SSM `/luciel/database-url` at run time inside the migrate task; the sandbox-agent never holds DB creds at rest.
* IAM scope at execution time: `LucielSandboxArc5MigrateScope` (provisioned for Revision A and retained per the 5-gate doctrine record `D-prod-credential-scope-expansion-protocol-2026-05-23`).
