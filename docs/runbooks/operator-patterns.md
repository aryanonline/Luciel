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
surfaced separate prod-state drift (D-prod-3-migrations-behind-2026-05-01,
resolved in Commit 8b-prereq-data). The pattern itself worked exactly as
designed: container ran, Alembic executed, transactional DDL rolled back
cleanly on data-state mismatch, prod RDS unchanged. Fail-fast behavior is
a feature of Pattern N, not a regression.