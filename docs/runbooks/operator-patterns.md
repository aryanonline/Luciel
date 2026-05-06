# Operator Patterns

This document codifies the operational patterns Luciel's solo operator follows
when running infrastructure tasks against production.

Patterns A through L are documented in the canonical recap at
`docs/recaps/{LATEST}-step-28-phase-1-complete-canonical.md` and will be
backfilled into this file in Commit 9 (Phase 1 close). The `{LATEST}` token
is a placeholder for the date-stamped recap filename produced in Commit 9.

This file currently codifies one new pattern, introduced in Commit 8b-prereq:

---
## Pattern E - Secret Handling Discipline

**Rule:** Secrets (admin keys, DB passwords, API tokens) flow through environment
variables sourced at runtime from authoritative stores (AWS SSM SecureString,
local password manager). Secrets are never echoed to terminals, never written
to disk, never committed to git, and never persisted in shell history. The
operator's authentication state lives in the running shell process and dies
with it.

### Why this rule exists

- **Brokerage tech-due-diligence:** "How do credentials reach your operators
  and services?" must have a defensible answer that doesn't include "in a
  config file" or "in a Slack message."
- **Blast-radius containment:** if an operator workstation is compromised,
  shell history and on-disk config files are the first places an attacker
  reads. Pattern E ensures those locations contain no usable credentials.
- **Audit traceability:** secrets sourced from SSM produce CloudTrail
  `GetParameter` events. Secrets typed into a terminal produce no trail.
  CloudTrail is the canonical "who accessed what credential when" record.

### How

- **Source from SSM at session start, into env var, no echo:**
$env:LUCIEL_PROD_ADMIN_KEY = (aws ssm get-parameter `
--name /luciel/production/platform-admin-key `
--with-decryption --query Parameter.Value --output text)
- **Verify shape, not value:** assert `StartsWith("luc_sk_")` and `Length == 50`
to confirm the source returned a valid credential without echoing it.
- **For pasted credentials (e.g., when re-using across sessions):** paste
directly into a `$env:` assignment in PowerShell. Do NOT echo back to
confirm. Close the shell when done if extra paranoia is warranted.
- **Never:**
- `echo $env:LUCIEL_PROD_ADMIN_KEY`
- `Set-Content -Path secrets.txt -Value $env:LUCIEL_PROD_ADMIN_KEY`
- `git add` a file containing a real credential
- Pass credentials as positional CLI args (they appear in `ps` output and
  shell history); use environment variables or `--password-stdin` style
  flags instead

### Verification gate

- `Get-History | Select-String 'luc_sk_'` should return zero lines after
any session involving secrets
- `git log -p -- ':!.env.example'` should show zero real `luc_sk_` prefixes
- SSM parameters under `/luciel/` should all be `Type=SecureString` (not
`String`); `aws ssm describe-parameters` returns the type per parameter

### Known violations and follow-ups

- **D-prod-superuser-password-leaked-to-terminal-2026-05-03:** prod RDS
master password (`luciel_admin`) was echoed in a connection-string error
message during Commit 13 setup work. Rotation pending Phase 2.
- **D-shell-history-key-exposure-2026-05-01:** general process improvement
to codify the corrected Read-Host pattern. Resolved by this section's
"How" guidance.


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

### SQL provenance â€” three independent channels

Every Pattern O invocation captures the verbatim SQL in three places:

1. **CloudTrail `run-task` event** captures `containerOverrides.command` array,
   including the `--sql-literal` argument. Default 90-day retention, extendable.
2. **CloudWatch Logs `_query_input` line** emitted by the script before any
   database interaction. Captured even when the database is unreachable or
   when the connection itself fails. Retention per log group.
3. **SHA256 cross-correlation** â€” every result row, every error row, and the
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

- `0` â€” query succeeded, results emitted.
- `2` â€” `DATABASE_URL` env var not set. The `_query_input` line is still
  emitted before this exit, preserving SQL provenance even when the task-def
  is misconfigured.
- `3` â€” connect failed (network, credentials, RDS unreachable).
- `4` â€” query failed at the Postgres layer. Includes the read-only-rejection
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
## Pattern S - Per-Resource Cleanup Walker for Residue Tenants

**Status note (2026-05-02):** As of Commit 12 (`f9f6f79` - tenant cascade in code),
the platform's `PATCH /api/v1/admin/tenants/{id}` with `active=false` triggers
atomic in-code cascade through all child resources. The Pattern S walker is now a
thin trigger for that cascade plus a teardown-integrity verification probe. Use
the walker as a backup/recovery tool for partial cleanups or when the API path
is unavailable; for normal tenant deactivation, prefer the API.


**Rule:** Tenant cleanup is operator-driven via per-resource DELETE calls in
leaf-first order, NOT a single tenant-level cascade. The PATCH on a tenant's
`active=false` field deactivates only the tenant_configs row; it does NOT
cascade to child resources (api_keys, luciel_instances, agents, domains).
Operators MUST walk the resource tree per-tenant.

**Why:**
- The `PATCH /api/v1/admin/tenants/{tenant_id}` endpoint updates fields on
  tenant_configs only. There is no `DELETE /api/v1/admin/tenants/{tenant_id}`
  endpoint as of this commit. Sibling resources (agents, domains,
  luciel-instances, api-keys) have their own DELETE endpoints which soft-delete
  by flipping `active=false`.
- Pillar 10 (`teardown integrity`) checks `live=0` for tenant_configs +
  domain_configs + luciel_instances + api_keys + agents. All five must
  be deactivated for the tenant to be considered cleanly torn down.
- Verification suites (Pillars 12, 13, 14, 24.5b prod gate, 26b/c verifies,
  27c-final sync verify) historically did not run teardown against prod, so
  prod accumulated 18 active residue tenants between 2026-04-22 and
  2026-04-27. This pattern is the operator procedure for cleaning that up,
  and for keeping prod clean going forward.

**Security boundary (Pattern E inherited):**
Admin key sourced from `$env:LUCIEL_PROD_ADMIN_KEY` (SSM
`/luciel/production/platform-admin-key`), never echoed, never written to
disk, never to shell history. Authorization header
`Authorization: Bearer $env:LUCIEL_PROD_ADMIN_KEY`.

**Deletion order (leaf-first):**
1. **api-keys** (leaf-most, FK from luciel-instances optional)
2. **luciel-instances** (FK target of api-keys)
3. **agents** (children of domains)
4. **domains** (parents of agents, children of tenants)
5. **tenant** PATCH `active=false` (root)

This order avoids FK violations during deletion. Top-down ordering is
unsafe.

**Idempotency:**
Every DELETE endpoint in this walker is soft-delete (flips `active=false`,
row persists). Calling DELETE on an already-inactive resource may return
404 or 200 depending on the endpoint; the walker's GET-then-skip pattern
avoids relying on that ambiguity. Pre-fetch each list, filter to
`active=true`, only act on those. Re-running the walker against a
fully-cleaned tenant produces zero mutations.

**Three-channel audit provenance:**
1. CloudWatch / CloudTrail captures each API call's request/response
   (auth header redacted, method + URL + body + status code preserved).
2. `admin_audit_logs` table receives one row per state-changing endpoint
   (Pillar 17 contract for api-keys; analogous rows for other resources).
3. Local cleanup log JSON (`prod-cleanup-YYYY-MM-DD.json`) captures the
   operator-side view: timestamp, tenant_id, resource_type, resource_id,
   action, http_code. Committed to repo as audit artifact.

**Pre-launch checks:**
1. `aws sts get-caller-identity` returns expected account.
2. `$env:LUCIEL_PROD_ADMIN_KEY` set, starts with `luc_sk_`, length ~50.
3. `curl https://api.vantagemind.ai/health` returns 200.
4. `curl -H "Authorization: Bearer $env:LUCIEL_PROD_ADMIN_KEY" https://api.vantagemind.ai/api/v1/admin/tenants` returns 200 with tenant list.
5. `python -m app.verification` baseline: 16/17 pillars green
   (Pillar 13 pre-existing red).
6. Walker's `-DryRun` mode against the target tenant set produces the
   expected `would-delete` / `would-patch` plan before any real run.

**Tooling:**
- `scripts/cleanup_residue_tenant.ps1` is the canonical walker. Takes
  `-TenantId` (required), `-ApiBase` (default `https://api.vantagemind.ai`),
  `-DryRun` (switch). Exits 0 on success, 2 on missing/malformed admin key,
  4 on post-walk teardown still showing violations, 5 on caught exception.
- `Invoke-RestMethod` for GETs (clean JSON parsing).
- `curl.exe` for PATCH/DELETE (file-body via `--data @path` to avoid
  PowerShell inline-JSON quote-eating, drift
  `D-powershell-curl-inline-json-quoting-2026-05-01`).
- `Write-Host` for log emission, captured via `Start-Transcript` for
  disk persistence.

**Worked example: Commit 8b-prereq-cleanup, 2026-05-01:**
- Canary: `step27-syncverify-7064`, 4 children deleted manually, 1 tenant
  PATCH, teardown passed.
- Batch: 17 active residue tenants (12 step24-5b-*, 5 step27-prodgate-*).
- Walker invoked once per tenant.
- 70 prod mutations total: 53 DELETEs + 17 PATCHes.
- All 17 walks returned `teardown_passed=true, violation_count=0`.
- Wall-clock: 30.8 seconds. Cost: ~70 API calls, negligible.
- Idempotency re-run produced 0 mutations on 18 tenants (canary + batch).

**Resumption checklist for future cleanups:**
1. Pre-flight: 5/6 checks above.
2. List active residue tenants:
   `GET /api/v1/admin/tenants` then filter
   `WHERE tenant_id MATCHES residue-pattern AND active=true`.
3. Dry-run all candidates first; review `would-*` action plan.
4. Real run with transcript capture.
5. Verify final state: residue active count = 0.
6. Re-run `python -m app.verification`, expect 16/17 (no regression).
7. Commit cleanup log as audit artifact.

---

## Pre-flight gate (Step 28 C9 / P3-N)

**Authored 2026-05-06 to resolve P3-N. Supersedes the inline 5-block
pre-flight in `CANONICAL_RECAP.md` Section 13 Step 3 for any workflow
that exercises the async memory path.**

### Why a script

The original 5-block pre-flight (AWS identity, git state, docker, dev
admin key, local verification) passed cleanly during the Pillar 13 A3
incident on 2026-05-04 even though the local stack was running
degraded -- celery was not up, `settings.memory_extraction_async` was
the local default `False`, and `ChatService`'s sync fallback was
silently servicing memory-extraction calls. The customer-facing
assistant reply ("I'll remember that") was a lie for an entire
prod-parity gap. A correct pre-flight would have caught this on the
first attempted repro instead of after instrumentation.

`scripts/preflight.ps1` runs the 5 historical gates plus 2 new ones:

- **Gate 6** -- `celery -A app.worker.celery_app inspect ping --timeout 5`
  must return at least one responder.
- **Gate 7** -- `app.core.config.settings.memory_extraction_async`
  must equal `True` (matching prod `MEMORY_EXTRACTION_ASYNC=true`
  in `backend-td-rev*.json`).

Both new gates fail loudly by default. The operator can pass
`-AllowDevSync` to convert them to DEGRADED warnings, but ONLY if
the intended workflow does not exercise the async memory path.

### When the script is mandatory (strict mode, no -AllowDevSync)

- Before any verification run targeting Pillar 11 (async memory
  extraction) or Pillar 13 (identity stability under role change /
  cross-tenant identity-spoof guard / departure semantics).
- Before any prod-touching ceremony where local repro must mirror
  prod async behavior.
- Before opening a Phase-3 backlog item that involves
  `MemoryService`, `ChatService` extractor paths, or the celery
  worker.
- Before authoring or modifying any code in `app/memory/service.py`
  or `app/services/chat_service.py` that affects extraction control
  flow.

### When -AllowDevSync is acceptable

- Local UI / docs / schema work that does not invoke chat turns.
- Read-only investigations (e.g. recap drafting, audit-log reads).
- Pillar work that does not touch memory extraction (Pillars 1-8,
  15-23 are all green under sync mode in current matrix).

### Failure mode and recovery

If Gate 6 fails strict, start the celery worker locally:

```
celery -A app.worker.celery_app worker --loglevel=info
```

If Gate 7 fails strict, set the env var before re-running
`python -m app.verification` or `scripts/preflight.ps1`:

```
$env:MEMORY_EXTRACTION_ASYNC = 'true'
```

### SHA pin (operator pull guard)

`scripts/preflight.ps1 -ExpectedSha <prefix>` adds a Gate 2 SHA
assertion that `git rev-parse HEAD` matches the prefix. Use this
after `git pull origin step-28-hardening-impl` when an advisor has
just pushed a specific commit and the next AWS write-side call
references `file://<task-def>.json` from that commit. Resolves the
`D-operator-pull-skipped-before-write-side-aws-ops-2026-05-05` class
of drift.

### Cross-references

- `docs/PHASE_3_COMPLIANCE_BACKLOG.md` P3-N (the originating item)
- Drift `D-preflight-degraded-without-celery-2026-05-04`
- Drift `D-celery-worker-not-running-locally-2026-05-02` (superseded)
- Recap `docs/recaps/2026-05-04-pillar-13-a3-real-root-cause.md` Section 6
- `CANONICAL_RECAP.md` Section 13 Step 3 (historical 5-block, now
  subsumed by this script)
