# Operator Patterns

This document codifies the operational patterns Luciel's solo operator follows
when running infrastructure tasks against production.

Patterns A through L are documented in the canonical recap at
`docs/recaps/{LATEST}-step-28-phase-1-complete-canonical.md` and will be
backfilled into this file in Commit 9 (Phase 1 close). The `{LATEST}` token
is a placeholder for the date-stamped recap filename produced in Commit 9.

This file currently codifies one new pattern, introduced in Commit 8b-prereq:

---

## Pattern N - Prod Database Migrations Apply via ECS One-Shot Task

**Rule:** Production database migrations apply via an ECS one-shot Fargate
task (`luciel-migrate:N` family) that runs the backend container image with a
`command: ["alembic", "upgrade", "head"]` override. The operator never connects
to prod RDS directly from a laptop, bastion, or any path outside the VPC.

### Why this rule exists

- **Brokerage tech-due-diligence:** "How do you apply database migrations to
  production?" must have a single, defensible answer documented in source
  control.
- **PIPEDA / future SOC 2:** enforced (not trusted) boundary between operator
  and prod data plane.
- **Reproducibility:** the same migration path applies in dev, staging, and
  prod - only the injected `DATABASE_URL` differs.
- **Rollback symmetry:** `alembic downgrade -1` runs through the same
  task-def shape.

### Task-def shape

- **Family:** `luciel-migrate`
- **Image:** same as `luciel-backend` (single image, multiple task-defs).
  Image is pinned by digest, not tag, so retags do not silently change what
  runs.
- **Command override:** `["alembic", "upgrade", "head"]`
- **Secrets injected:** `DATABASE_URL` only. The Alembic env.py imports the
  full app Settings object from `app.core.config`, but every non-DB field
  has a default that passes pydantic validation, so least-privilege at the
  task-def level is achieved without app-config refactoring.
- **Network:** same subnets and security group as the backend service for
  current reachability to RDS. Future Phase 3 work
  (`D-rename-shared-sg-to-backend-sg`, `D-public-ip-tasks`) will tighten
  this.

### Verification gate

The migrate task `exit: 0` is the verification that the migration applied.
CloudWatch logs (under `/ecs/luciel-backend` with stream prefix `migrate`)
should show three lines like:
INFO [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO [alembic.runtime.migration] Will assume transactional DDL.
INFO [alembic.runtime.migration] Running upgrade PREV -> NEW, DESCRIPTION

If the migration adds DB objects, the next downstream operation (e.g., a
mint script ALTER ROLE, a service that uses the new schema) will error
loudly if the migration did not actually apply. This is the second
verification layer.

### Rollback

Register a new `luciel-migrate:N+1` revision with command
`["alembic", "downgrade", "-1"]` and run it the same way. Or, in true
emergencies, re-register a previous revision and run it (semantics depend on
the migration downgrade body - verify before running).

### Pre-launch operator checks (REQUIRED before each migration)

1. Confirm the target migration down_revision matches the current prod
   `alembic_version` row before running upgrade. How to check without
   laptop-direct connection: read the previous migration commit body in
   git; the chain of down_revision fields is the source of truth.
2. Confirm the migration `op.execute(...)` SQL is transactional-compatible
   (no DDL outside transaction, no autocommit blocks).
3. Confirm the task-def command override is `["alembic", "upgrade", "head"]`,
   not `null`:

       aws ecs describe-task-definition --task-definition luciel-migrate --region ca-central-1 --query "taskDefinition.containerDefinitions[0].command"

   Expected: `["alembic", "upgrade", "head"]`. If `null`, abort and
   register a new revision before proceeding. Pre-2026-05-01, the
   `luciel-migrate` task-def had `command: null` for 10 revisions; this is a
   known historical drift item resolved in Commit 8b-prereq.

### Provenance

This pattern was codified in **Commit 8b-prereq** (Step 28 Phase 1,
2026-05-01) after recon discovered all 10 prior `luciel-migrate` revisions
had hollow command overrides. The `D-undocumented-laptop-migrate-path`
drift was resolved by establishing this pattern and registering
`luciel-migrate:11` as the first working migrate task-def in the project's
history.

First-run note: invocation of `luciel-migrate:11` on 2026-05-01
surfaced separate prod-state drift (D-prod-3-migrations-behind-2026-05-01).
Resolution scoped to a future commit (Phase 1 close or Phase 2),
not Commit 8b-prereq-data, which discovered an additional structural
drift item (D-prod-tenant-residue-2026-05-01) that takes precedence.
The pattern itself worked exactly as
designed: container ran, Alembic executed, transactional DDL rolled back
cleanly on data-state mismatch, prod RDS unchanged. Fail-fast behavior is
a feature of Pattern N, not a regression.
---

## Pattern O - Read-Only Prod Recon via ECS One-Shot Task

**Rule:** Ad-hoc read-only queries against production RDS run via an ECS
one-shot Fargate task in the `luciel-recon:N` family. The operator never
connects a laptop directly to prod RDS for any purpose, including read-only
inspection. Pattern O is the read-only sibling of Pattern N: same network
path, same image, same audit shape, distinct task-def family.

### Why this rule exists

- **Brokerage tech-due-diligence:** "How do you inspect production data?" has
  the same single-defensible-answer requirement as migrations.
- **Operator-to-prod-RDS boundary:** the rule "no laptop-direct prod connection"
  is uniform across all interactions. No carve-out for "but it's only a SELECT."
- **Audit completeness:** every prod read produces a CloudTrail run-task event
  AND a CloudWatch log entry capturing the verbatim SQL. Three independent
  channels (see Provenance below).
- **Reusable infrastructure:** the same task-def runs many ad-hoc queries via
  per-invocation `containerOverrides.command`, so the recon family does not
  accumulate revision spam the way Pattern N's `luciel-migrate` family does.

### Security boundary

The script `scripts/prod_readonly_query.py` sets `psycopg.Connection.read_only =
True` immediately after `psycopg.connect()`. This causes Postgres to issue
`SET TRANSACTION READ ONLY` at the start of every transaction. Any DML or DDL
attempt is rejected by Postgres itself with sqlstate `25006` ("read only SQL
transaction"), exit code 4, and a structured `_error` JSON line.

The Postgres transaction is the only security layer. The script does NOT
implement Python-side regex deny-listing of mutating statements; such a layer
would suggest it does real work when it does not (DO blocks, function calls
with side effects, statement injection variants would all bypass regex). The
honest documentation is "the security boundary is at the database, not at the
script."

### Task-def shape

- **Family:** `luciel-recon`
- **Image:** same as `luciel-backend` (single image, multiple task-def families).
  Image is pinned by digest, not tag.
- **Command (in task-def):** `["python", "scripts/prod_readonly_query.py"]`
  with no `--sql-literal` baked in. SQL is supplied per-invocation via
  `aws ecs run-task --overrides '{"containerOverrides":[{"command":[..., "--sql-literal", "<query>"]}]}'`.
- **Secrets injected:** `DATABASE_URL` only, from
  `arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/database-url`.
  The full-access SSM URL is acceptable here because the script's READ ONLY
  transaction rejects mutations regardless of credential privileges; future
  Phase 3 hardening may introduce a true read-only Postgres role with its own
  SSM parameter (`D-recon-task-role-reuses-migrate-role-2026-05-01`).
- **Network:** same subnets and security group as Pattern N's migrate task.
- **Stream prefix:** `recon` (distinct from `migrate`, `web`, `worker`).

### SQL provenance — three independent channels

Every Pattern O invocation captures the verbatim SQL in three places:

1. **CloudTrail `run-task` event** captures `containerOverrides.command` array,
   including the `--sql-literal` argument. Default 90-day retention, extendable.
2. **CloudWatch Logs `_query_input` line** emitted by the script before any
   database interaction. Captured even when the database is unreachable or
   when the connection itself fails. Retention per log group.
3. **SHA256 cross-correlation** — every result row, every error row, and the
   `_meta` summary line carry `query_sha256` so log entries from any of the
   above channels can be deterministically correlated.

The triple redundancy is intentional. Each channel has different retention,
different access controls, and different failure modes. Together they are
brokerage-DD-defensible regardless of which channel an auditor first asks
about.

### Output format

- One JSON object per stdout line (CloudWatch-Insights-friendly).
- First line: `{"_query_input": "<verbatim SQL>", "_query_sha256": "<hex>"}`.
- Then one line per result row, with all values stringified safely
  (datetimes ISO 8601, UUIDs as strings, decimals as strings, bytes as hex).
- Final line: `{"_meta": {"row_count": N, "truncated": bool, "row_limit": N,
  "elapsed_ms": N, "query_sha256": "<hex>"}}`.
- Truncation enforced at `--row-limit` (default 1000) as a belt-and-suspenders
  guard against accidental unbounded scans.

### Exit codes

- `0` — query succeeded, results emitted.
- `2` — `DATABASE_URL` env var not set. The `_query_input` line is still
  emitted before this exit, preserving SQL provenance even when the task-def
  is misconfigured.
- `3` — connect failed (network, credentials, RDS unreachable).
- `4` — query failed at the Postgres layer. Includes the read-only-rejection
  path (sqlstate `25006`) for any accidental mutation attempt.

### Pre-launch operator checks

1. Confirm the SQL is genuinely read-only in intent. The transaction layer
   will catch mutations either way, but writing read-only SQL up front avoids
   wasted Fargate cold-starts on rejected queries.
2. For queries returning user data, deliberately exclude PIPEDA-relevant
   payload columns (e.g., `memory_items.content`). Scope the SELECT to
   metadata only unless content access is the explicit purpose of the recon.
3. For queries that may scan large tables, prefer aggregate forms
   (`count(*)`, `count(*) FILTER (...)`) over row enumeration. Use
   `--row-limit` to bound output if row enumeration is necessary.
4. The `containerOverrides` JSON is passed via `file://` from a temp file,
   not as an inline argument, due to PowerShell's interaction with AWS CLI
   JSON argument parsing (`D-powershell-aws-cli-json-arg-quoting-2026-05-01`).

### Provenance

This pattern was codified in **Commit 8b-prereq-data** (Step 28 Phase 1,
2026-05-01) during work that was originally scoped to apply pending
migrations to prod RDS. Pre-mutation recon via the new pattern surfaced an
unexpected structural drift item: 29 verification residue tenants in
production from Step 24.5b/26/27 deploy verifications, accounting for 100%
of `tenant_configs` rows in prod. The migration-apply work was deferred to
allow deliberate scope of cleanup (`D-prod-tenant-residue-2026-05-01`) in
its own commit. The recon flow itself worked as designed: 7 Fargate task
runs, ~$0.005 USD total compute cost, full three-channel audit trail in
CloudWatch and CloudTrail, no prod state changes.

First-run discovery: Pattern O's first production use found that prior
verification flows (run during deploys, not via the verification suite's
own ephemeral path) had been creating tenants in prod RDS without an
associated teardown contract. This is a separate finding from
Pillar 10's per-suite teardown integrity, which is suite-internal and
runs against dev, not prod.