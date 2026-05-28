# Arc 11 Readiness Report

**Date:** 2026-05-28 00:14 EDT (UTC-04)
**Status:** READY \u2014 Arc 11 unblocked
**Anchoring:** Vision v1 \u00b7 Architecture v1 \u00b7 Customer Journey v1 \u00b7 Sandbox Agent Key Credentials

## Founder directive driving this audit

> "reanlyze our repo fully and make that all entire (both repos and
> are infrastructure) are aligned with each other, and, most
> importantly with each other [the business documents]. Don['t]
> defer anything, our entire system, once fully aligned properly,
> will be start ARC 11."

## Seven-dimension alignment check (per Space instructions)

The Space "System Consistency" rubric:

> "In sync means: deployed code matches merged branch, container
> images match the latest build, schema matches the latest
> migration, env vars match documented config, frontend calls match
> backend contracts."

All seven dimensions audited.

### 1. Deployed code matches merged branch
- **Backend `main` HEAD:** `535e2d3`
- **Backend service running TD:** `luciel-backend:123` (image `main-535e2d3`)
- **Worker service running TD:** `luciel-worker:59` (image `main-535e2d3`)
- **Frontend `main` HEAD:** `1844b94`
- **Frontend deploy:** Amplify job #45, status SUCCEED, commit `1844b94`

### 2. Container images match the latest build
- ECR repository `luciel-backend` contains tag `main-535e2d3` and
  `main-latest` (both pointing at the same digest as the head main
  commit).
- Both backend and worker ECS services run that image.

### 3. Schema matches the latest migration
- Alembic head (local): `arc10_5_drop_dead_config_id_columns`
- Alembic current (prod RDS, verified via in-cluster ECS task
  running `alembic current`): `arc10_5_drop_dead_config_id_columns (head)`
- Database matches the migration chain end-to-end.

### 4. Env vars match documented config
- 19 SSM secrets referenced by backend TD: all present
- 9 SSM secrets referenced by worker TD: all present
- 2 S3 buckets used by app code (`luciel-data-exports`,
  `luciel-audit-cold-archive`): both exist with the encryption +
  lifecycle policies set in Arc 10
- 1 widget CDN bucket (`luciel-widget-cdn-prod-ca-central-1`):
  wired via the CI workflow's `WIDGET_CDN_BUCKET` env var
- 4 CloudWatch alarms (`luciel-arc10-*`): all OK
- 1 SNS topic (`luciel-prod-alerts`): receives all 4 alarms
- Celery beat schedule (4 tasks): every task module + function
  verified to exist in `app/worker/tasks/`

### 5. Frontend calls match backend contracts
Every frontend API call mapped to a backend route handler. All 22
paths in the frontend audit:

| Frontend path | Backend handler |
|---|---|
| `GET /admin/team-members` | `list_team_members_route` |
| `GET /admin/invites` | `list_invites_route` |
| `GET /admin/instances` | `list_instances_route` |
| `GET /admin/api-keys` | `list_api_keys_route` |
| `GET /admin/embed-keys` | (via `/admin/api-keys?key_kind=embed`) |
| `POST /admin/account/close` | `close_account_route` |
| `POST /admin/account/reactivate/stage` | `reactivate_stage_route` |
| ... 15 more, all verified ... | |

**Removed in this readiness pass** (dead frontend\u2192backend wires):
- `GET /admin/agents` (frontend `listAgents()` \u2014 backend route never existed)
- `GET /admin/domains/self-serve` (Dashboard Company tab \u2014 dropped tables)
- `POST /admin/domains/self-serve` (same)
- `DELETE /admin/domains/self-serve/:id` (same)

### 6. Audit chain immutability + RBAC posture
- `luciel_audit_archiver` role has SELECT + UPDATE + INSERT on
  `admin_audit_logs` + USAGE on `admin_audit_logs_id_seq` (Arc 10
  Gap 6 PRs #108, #109)
- `admin_audit_logs.luciel_instance_id` is nullable to allow
  admin-scoped cascade emissions (Arc 10 Gap 7 PR #113;
  Architecture \u00a73.7.3 Wall 3 applies to customer-data, not the
  audit log)
- `traces` no longer carries dead `*_config_id` pointer columns
  (Arc 10.5 migration `arc10_5_drop_dead_config_id_columns`)
- `_PROBES` verifier list in `app/api/v1/verification.py` was
  realigned to the live V2 schema (Arc 11 readiness PR #117)

### 7. End-to-end harnesses against prod RDS
- **Gap 6** (audit-tier archiver): in-cluster ECS task
  `luciel-arc10-gap6-audit-e2e:9` against prod RDS \u2014 **exit 0**
- **Gap 7** (Stripe close \u2192 reactivate): in-cluster ECS task
  `luciel-arc10-gap7-stripe-e2e:4` against prod RDS \u2014 **exit 0**

## Backend test baseline

`pytest tests/` against a fresh local Postgres applied to the head
migration:

```
1432 passed, 40 skipped, 17 warnings in 6.42s
```

No regressions across the entire Arc 10 + Arc 10.5 + Arc 11
readiness sweep.

## Frontend test baseline

`npm test` (Vitest):

```
8 test files passed (96 tests; was 99 before deletion of the 3
listDomainsSelfServe cases tied to the removed Domain layer).
```

## Dropped legacy surfaces (V2 cleanup complete)

The following V1 surfaces are gone from both repos and from prod
schema. Vision/Architecture do not describe any of them.

| Surface | Removed at | Cleanup PR |
|---|---|---|
| `agents` table | Arc 5 Path A | Arc 10 PR #111 (cascade), Arc 10.5 PR #115 (code) |
| `agent_configs` table | Arc 5 Path A | Arc 10.5 PR #115 |
| `domain_configs` table | Arc 5 Path A | Arc 11 readiness PR #117 (cascade); FE PR #12 |
| `tenant_configs` table | Arc 5 Path A | Arc 11 readiness PR #117 |
| `luciel_instances` table | Arc 9.2 PR #99 (renamed to `instances`) | Arc 11 readiness PR #117 |
| `AgentRepository` Python class | Arc 5 Path A | Arc 10 PR #111 (close route); Arc 10.5 PR #115 |
| `AgentConfig` model + schemas | \u2014 | Arc 10.5 PR #115 |
| `LucielInstance` import alias | \u2014 | Arc 11 readiness PR #117 |
| `LucielInstanceForensic` / `LucielInstanceToggleRequest` | \u2014 | Arc 11 readiness PR #117 |
| Dashboard Company / Domains tab | \u2014 | Arc 11 readiness FE PR #12 |
| `traces.tenant_config_id` / `domain_config_id` / `agent_config_id` columns | Arc 10.5 migration | Arc 10.5 PR #115 |

## Production bugs surfaced + fixed (Arc 10 + Arc 10.5 + Arc 11 readiness)

| # | Bug | Where | Fix PR |
|---|---|---|---|
| 1 | `ACTION_AUDIT_LOG_TIER_ARCHIVED` missing from `ALLOWED_ACTIONS` | audit-tier archiver | #107 |
| 2 | `luciel_audit_archiver` role missing INSERT grant | audit-tier archiver | #108 |
| 3 | `luciel_audit_archiver` role missing sequence USAGE | audit-tier archiver | #109 |
| 4 | Batch-audit emission missing `luciel_instance_id` | audit-tier archiver | #110 |
| 5 | `/account/close` imported deleted `AgentRepository` | closure route | #111 |
| 6 | Cascade called `instance_repo.deactivate_all_for_tenant` (renamed away) | closure cascade | #113 |
| 7 | `admin_audit_logs.luciel_instance_id` NOT NULL too strict | closure cascade | #113 |
| 8 | `ACTION_ACCOUNT_CLOSURE_INITIATED` + `ACTION_ACCOUNT_REACTIVATED` missing from `ALLOWED_ACTIONS` | close + reactivate | #113 |
| 9 | `_inverse_restore_table` hardcoded `deactivated_at` for all tables | reactivate | #113 |
| 10 | `signup_free` 500'd on non-IP `request.client.host` | signup | #112 |
| 11 | Dashboard Team tab called `/admin/agents` (404) | Team tab | #115 + FE #11 |
| 12 | Hard-delete cascade `DELETE`'d dropped tables | hard-delete | #115 |
| 13 | `traces.*_config_id` carried dead pointer data | traces | #115 |
| 14 | Dashboard Company tab called `/admin/domains/self-serve` (404) | Company tab | FE #12 |
| 15 | Verifier `_PROBES` covered dropped tables, missed `instances` | verification | #117 |

15 production bugs surfaced and fixed across the Arc 10 re-open +
Arc 10.5 + Arc 11 readiness work.

## Arc 11 gate

All seven alignment dimensions verified. Both repos build green.
Both production E2E harnesses pass against prod RDS. No
known-broken routes; no dropped-table code paths.

**Arc 11 is unblocked.**
