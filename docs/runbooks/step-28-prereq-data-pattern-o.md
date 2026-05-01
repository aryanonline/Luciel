# Step 28 Phase 1 - Commit 8b-prereq-data: Pattern O Recon Runbook

## Purpose

This runbook documents the operator procedure for running Pattern O
read-only prod recon via the `luciel-recon` ECS task-def family. The
**contract** for Pattern O lives in `docs/runbooks/operator-patterns.md`.
This file documents the **procedure**.

The original scope of Commit 8b-prereq-data was to apply pending Alembic
migrations to prod RDS via Pattern N. Pre-mutation recon via the new
Pattern O surfaced a structural drift item
(`D-prod-tenant-residue-2026-05-01`) that took precedence; the
migration-apply work was deferred to allow scoped cleanup in its own
commit.

## Prerequisites

- Recon image pushed to ECR. As of this commit, the canonical recon image
  is built from working-tree state on top of HEAD `7560397` and pushed as
  `step28-prereq-data-recon-7560397-v2`. The digest is the immutable
  reference; the tag is convenience.
- Task-def `luciel-recon:1` registered. See `recon-td-rev1.json` at repo
  root for the canonical shape; matches Pattern N's `luciel-migrate:11`
  shape with three intentional differences (family, command, log stream
  prefix).
- AWS CLI configured for `ca-central-1`, account `729005488042`, with
  `ecs:RunTask`, `ecs:DescribeTasks`, and `logs:GetLogEvents` permissions
  on the operator's IAM identity.

## Single-query procedure

The procedure for one Pattern O recon read is:

1. Compose the SQL as a single PowerShell string.
2. Write `containerOverrides` and `awsvpcConfiguration` JSON to temp
   files (`file://` arg-passing on PowerShell, see Pattern O Pre-launch
   check 4).
3. `aws ecs run-task` with `--task-definition luciel-recon:1`, capture
   the returned task ARN.
4. `aws ecs wait tasks-stopped` to block until the task transitions
   STOPPED. Typical Fargate cold start: 55-65 seconds for the recon
   image.
5. `aws ecs describe-tasks` to check `containers[0].exitCode`. Expect
   `0` for happy-path, `2` for missing DATABASE_URL (task-def
   misconfigured), `3` for connect failure, `4` for query rejected by
   Postgres.
6. `aws logs get-log-events` with stream
   `recon/luciel-backend/<task-id>` to retrieve the structured JSON
   output.
7. Clean up temp JSON files.

A worked PowerShell template is preserved in this repo's commit history
under Commit 8b-prereq-data; future runbook iterations may extract it
into a small helper script (`scripts/run_prod_recon.ps1`,
`D-pattern-o-helper-script-2026-05-01`).

## CloudWatch log shape

Every Pattern O run produces exactly the following log-line shape:

```json
{"_query_input": "<verbatim SQL>", "_query_sha256": "<hex>"}
{"col1": ..., "col2": ...}     # one JSON object per result row
...
{"_meta": {"row_count": N, "truncated": bool, "row_limit": N, "elapsed_ms": N, "query_sha256": "<hex>"}}
```

Failure paths replace the result rows with a single `_error` line:

```json
{"_query_input": "...", "_query_sha256": "..."}
{"_error": "...", "sqlstate": "...", "detail": "...", "query_sha256": "..."}
```

The `query_sha256` field on every line is the same value, deterministic
on the SQL bytes. Use it to cross-correlate CloudWatch entries with
CloudTrail `run-task` events for full audit reconstruction.

## Worked examples (this commit's recon runs)

The Pattern O recon flow ran 7 queries against prod RDS during work on
this commit. They are listed here as canonical worked examples of
Pattern O usage. Each ran successfully end-to-end, with full
three-channel audit, no prod state changes.

| # | Query intent | SQL shape | Result |
| - | ------------ | --------- | ------ |
| 1 | Count NULL/non-NULL `actor_user_id` in `memory_items` | aggregate with `FILTER` | 1 total, 1 NULL, 0 populated |
| 2 | `memory_items` schema | `information_schema.columns` | 13 columns |
| 3 | One NULL row's metadata (no payload) | row enumeration with payload columns excluded | 1 row, `step27-syncverify-7064` tenant |
| 4 | Confirm tenant orphan status | 5 union-all counts (failed: bad table name) | sqlstate 42P01 - table `tenants` does not exist |
| 5 | Public-schema base table list | `information_schema.tables` | 18 tables |
| 6 | Confirm tenant orphan status (corrected) | 5 union-all counts on real tables | All matches non-zero - tenant alive |
| 7 | Inventory verification residue tenants + total count | LIKE pattern across known shapes | 29 residue, 29 total - 100% residue |

The unexpected 100% residue ratio in run 7 was the discovery that
deferred the migration-apply work and reframed Commit 8b-prereq-data
to ship Pattern O codification only.

## Cost note

Pattern O is cheap. Each Fargate cold start is ~$0.0007 USD on the 512
CPU / 1024 MB sizing inherited from Pattern N. The 7 queries listed
above cost ~$0.005 USD total. CloudWatch log storage and CloudTrail
retention are negligible per query.

## Resumption note for next session

The next Pattern-O-related work is `D-prod-tenant-residue-2026-05-01`
cleanup, scoped as a separate commit (`Commit 8b-prereq-cleanup` or
similar). Suggested approach: invoke the existing admin
`DELETE /tenants/{id}` API endpoint (the same code path Pillar 10 uses
for verification suite teardown) for each of the 29 residue tenants,
verify cascade behavior on the first, then batch the remainder.
The existing image is sufficient for this work; no new image build
required.