# Step 28 Phase 2 — Operational Hardening Deploy Runbook

**Branch:** `step-28-hardening-impl`
**Working repo:** `Luciel-work` (the original `Luciel-original` is the
fallback if Phase 2 ever needs to be reverted wholesale).
**Pre-deploy gate:** `python -m app.verification` → **19/19 green** on
dev (Pillar 19 audit-log API mount included as of Commit 2).
**Post-deploy gate:** `python -m app.verification` → 19/19 green
against prod admin endpoints + smoke checks below.
**Total estimated wall-clock for the prod-touching commits:** 60–90
minutes for the worker DB role swap, 30–45 minutes each for alarms /
auto-scaling / health checks (Commits 4–7).

---

## 0 — Phase 2 commit map

Phase 2 is split across **9 commits**. Code-only commits (1–3, 8, 9)
ship via the normal CI/CD path with no AWS console steps. Prod-
touching commits (4–7) each ship as **code + IaC + this runbook
section** so we can execute them together at the laptop.

| # | Commit | SHA | Touches | Status |
|---|---|---|---|---|
| 1 | Pillar 15 consent route regression guard | (Phase 1 — `bd9446b`) | code | ✅ shipped |
| 2 | Audit-log API mount (`GET /api/v1/admin/audit-log`) | `75f6015` | code | ✅ shipped |
| 2b | Audit-log review fixes (H1/H2/H3, M1/M3, L1/L2) | `bfa2591` | code | ✅ shipped |
| 3 | Pillar 13 A3 sentinel-extractable fix | `56bdab8` | code (test) | ✅ shipped |
| 4 | Worker DB role swap → `luciel_worker` + admin password rotation | (pending, see §4) | RDS users + ECS task-def + SSM | ⏳ |
| 5 | CloudWatch alarms + SNS pipeline | (pending, see §5) | CloudWatch + SNS | ⏳ |
| 6 | ECS auto-scaling target tracking | (pending, see §6) | ECS + Application Auto Scaling | ⏳ |
| 7 | Container-level healthChecks (web + worker) | (pending, see §7) | ECS task-def | ⏳ |
| 8 | Batched retention deletes / anonymizes | `0d75dfe` | code | ✅ shipped |
| 9 | Phase 2 close — recap + this runbook | (this commit) | docs | 🔄 shipping now |

---

## 1 — Code-only commits (already deployable, no console steps)

These ship via the standard ECR push → task-def register → service
update pattern. No new AWS resources, no IAM changes.

### Commits 2 + 2b — Audit-log API mount

**What it does:** mounts `GET /api/v1/admin/audit-log` returning the
`admin_audit_logs` table with PII-redacted `diff` payloads via a
per-resource `_SAFE_DIFF_KEYS` allow-list. Disallowed keys collapse
to `"<redacted>"`. Cross-tenant `?tenant_id=` overrides are platform-
admin only and emit a `logger.warning` on use.

**Smoke test (post-deploy):**

```powershell
# Expect 200 + audit rows scoped to the caller's tenant.
$h = @{ "X-API-Key" = $env:LUCIEL_PLATFORM_ADMIN_KEY }
Invoke-RestMethod -Uri "https://api.vantagemind.ai/api/v1/admin/audit-log?limit=5" `
  -Headers $h | ConvertTo-Json -Depth 6
```

If any row's `diff` payload contains a value that should have been
redacted but isn't, that is a P0 — file as a new drift entry and
patch `_SAFE_DIFF_KEYS` immediately.

### Commit 3 — Pillar 13 A3

**What it does:** rewrites the Pillar 13 A3 setup turn so the
sentinel content is wrapped in extractable user-fact shape (per
`EXTRACTION_PROMPT` rules). A3 lookup is keyed on
`MemoryItem.message_id` (deterministic FK) with 30 s polling and a
soft sentinel-in-content check. Brings `python -m app.verification`
from 17/18 to 19/19 (Pillar 19 audit-log mount also green).

**Smoke test:** running `python -m app.verification` in a dev shell
with `LUCIEL_PLATFORM_ADMIN_KEY` set is the test.

### Commit 8 — Batched retention

**What it does:** retention purges now run in chunks via
`_batched_delete` / `_batched_anonymize` using
`DELETE/UPDATE WHERE id IN (SELECT id ... FOR UPDATE SKIP LOCKED LIMIT n)`,
committing per batch. New `Settings` knobs (`retention_batch_size`,
`retention_batch_sleep_seconds`, `retention_max_batches_per_run`)
control bandwidth.

**Why it matters:** without this, a year-old tenant with millions of
`messages` rows would issue a single unbounded DELETE that holds row
locks across the tenant's entire history, fills the WAL, blocks
autovacuum, and lags read replicas — a real RDS outage class.

**Partial-failure semantics:** if a mid-run batch raises, prior
batches are durable, a `DeletionLog` row is written with
`reason="...| PARTIAL: <ExcType>: <msg>"`, and the original exception
re-raises. Strict PIPEDA improvement over pre-Commit-8 atomic-or-
nothing behavior.

**Tunable defaults (per `app/core/config.py`):**

| Env var | Default | Notes |
|---|---|---|
| `LUCIEL_RETENTION_BATCH_SIZE` | 1000 | Rows per chunk. Tuned for db.t3.medium warm cache. |
| `LUCIEL_RETENTION_BATCH_SLEEP_SECONDS` | 0.05 | Pause between batches; lets autovacuum + replication catch up. |
| `LUCIEL_RETENTION_MAX_BATCHES_PER_RUN` | 10000 | Defense-in-depth ceiling: 10M rows/call max. |

**Smoke test:** trigger a manual purge in dev against a category
with synthetic >batch-size rows and confirm `deletion_logs` shows
`rows_affected == sum(actual deleted)` and the run completes without
holding locks (check via `pg_locks` mid-run).

### Commit 9 — Phase 2 close

This commit. Updates `docs/CANONICAL_RECAP.md` and creates this
runbook. No code changes.

---

## 2 — Prod-touching commits — execution mode

The remaining four commits each touch live AWS infrastructure.
**Aryan executes these at his laptop with the agent guiding** —
the agent does **not** run AWS CLI against prod from its sandbox.

For every prod-touching commit:
1. Read the corresponding section below in full before opening a
   shell.
2. Run the **dry-run / recon** block first.
3. Run the **mutation** block.
4. Run the **verification** block.
5. If anything looks wrong, run the **rollback** block — do not
   improvise.

All AWS CLI examples assume `--region ca-central-1` and
account `729005488042`.

---

## 3 — Pre-flight ritual (every prod-touching session)

Identical to canonical recap §13.3:

```powershell
# Block 1 — AWS identity (expect 729005488042)
aws sts get-caller-identity --query Account --output text

# Block 2 — Git state (expect clean)
git status --short; git log -1 --oneline; git stash list

# Block 3 — Docker
docker info --format "{{.ServerVersion}} {{.OperatingSystem}}"

# Block 4 — Dev admin key (expect True / 50)
$env:LUCIEL_PLATFORM_ADMIN_KEY.StartsWith("luc_sk_"); $env:LUCIEL_PLATFORM_ADMIN_KEY.Length

# Block 5 — Verification (expect 19/19)
python -m app.verification
```

If Block 5 isn't 19/19 green, **diagnose, do not deploy**.

---

## 4 — Commit 4 — Worker DB role swap (Option 3 ceremony)

**Revision history:**
- v1 (2026-05-03, in `925c64a`): described direct invocation of
  `python scripts/mint_worker_db_password_ssm.py`. **Superseded.**
- v2 (this section, 2026-05-04 post-Pillar-13-A3): rewritten to use
  the Option 3 ceremony via `scripts/mint-with-assumed-role.ps1`. The
  v1 flow leaked the admin DSN to CloudWatch on its first attempt
  (see `docs/recaps/2026-05-03-mint-incident.md` for full forensic
  narrative). The v2 flow closes that boundary architecturally.
- v2 also removes the old §4.7 `luciel_admin` rotation — that work
  was completed by P3-H on 2026-05-03 23:18 UTC and is no longer part
  of Commit 4. See P3-H entry in `docs/PHASE_3_COMPLIANCE_BACKLOG.md`
  and `docs/runbooks/step-28-p3-h-rotate-and-purge.md`.
- **v2.1 (commit `e1154bd`, 2026-05-04 post-failed-mint):** adds §4.0.5
  documenting the IAM policy gap discovered when the v2 mint dry-run
  passed but the real run was blocked by `mint_worker_db_password_ssm`'s
  `preflight_ssm_writable` check. Root cause: P3-K's permission policy
  was scoped only for admin-DSN read, but the Option 3 ceremony runs
  the mint script INSIDE the assumed role — so the role itself needs
  worker-SSM write rights. Drift logged as
  `D-p3-k-policy-missing-worker-ssm-write-2026-05-04`. Fix is policy-
  only (no code change to script or helper); see §4.0.5 for the
  apply-and-verify steps.
- **v3 (this commit, 2026-05-05 post-architectural-boundary):** the
  Option 3 ceremony (v2 / v2.1) is **architecturally superseded** by
  the Pattern N variant (P3-S.a). The v2 / v2.1 flow assumed the
  operator could `psycopg.connect(admin_dsn)` from their laptop, which
  is incompatible with the production VPC posture (RDS in private
  subnet, no public ingress, no bastion, no VPN). The boundary was
  never exercised by any prior smoke test because
  `mint_worker_db_password_ssm.py --dry-run` returned at line 491
  before reaching the DB connect at line 554; the gap was discovered
  only on the first real-run attempt 2026-05-04 ~20:12 UTC, which
  aborted at `ConnectionTimeout: connection timeout expired` before
  any production state mutation. Drift entries
  `D-option-3-ceremony-cannot-reach-private-rds-from-laptop-2026-05-04`
  and sister `D-mint-script-dry-run-skips-preflight-2026-05-04`. Full
  forensic narrative:
  `docs/recaps/2026-05-04-mint-architectural-boundary-pause.md`. v3
  adds **§4.0.6** (Pattern N mint architecture) and rewrites **§4.2**
  to dispatch to the new ceremony via `mint-via-fargate-task.ps1`.
  The v2 / v2.1 §4.0.5 IAM policy on `luciel-mint-operator-role`
  remains in force — that role is still what the operator laptop
  assumes — but the mint script no longer runs on the laptop; it
  runs inside the VPC under the new task role `luciel-ecs-mint-role`,
  which holds the same 5 statements (canonical IaC at
  `infra/iam/luciel-ecs-mint-role-permission-policy.json`).
- **v3 also notes the 2026-05-05 admin-DSN chat disclosure incident**
  (`docs/incidents/2026-05-05-admin-dsn-disclosed-in-chat.md`,
  drift `D-admin-dsn-disclosed-in-chat-2026-05-05`). The leaked
  `luciel_admin` master password was rotated end-to-end the same
  session — RDS modified, SSM `/luciel/database-url` written to v3,
  end-to-end verified by a clean `luciel-migrate` Fargate task. The
  v3 mint ceremony described below reads the ROTATED admin DSN; no
  follow-up action required at the runbook level.

**Goal:** worker process stops authenticating to RDS as `luciel_admin`
(superuser) and starts using `luciel_worker` (least-privilege role
created in Phase 1, drift `D-worker-role`).

**Why it matters:** Phase 1 enforces audit-log append-only by API.
This commit enforces it by **DB grant**, so even if a worker bug
attempts `UPDATE admin_audit_logs ...` it fails at Postgres.

**Prerequisite gate (all four must be ✅ before §4.2 mint):**

| Prereq | Status | Evidence |
|---|---|---|
| P3-J — MFA on `luciel-admin` | ✅ 2026-05-03 23:48 UTC | `arn:aws:iam::729005488042:mfa/Luciel-MFA` |
| P3-K — `luciel-mint-operator-role` | ✅ 2026-05-04 00:14 UTC | role + trust policy + `MaxSessionDuration: 3600` |
| P3-G — migrate role `ssm:GetParameterHistory` | ✅ 2026-05-03 ≈20:09 EDT | live policy has 6 SSM actions |
| P3-H — leaked `LucielDB2026Secure` rotated | ✅ 2026-05-03 23:56 UTC | SSM v1 → v2 + log stream deleted |
| P3-K-followup — mint role has worker-SSM write | ✅ 2026-05-04 (this commit) | live policy has 5 statements; §4.0.5 verifies |

**Code/IaC artifacts that land with this commit:**

- `scripts/mint-with-assumed-role.ps1` — Option 3 helper (commit
  `9e48098`). Pre-existing; verify present.
- `scripts/mint_worker_db_password_ssm.py` — hardened mint script
  with `--admin-db-url-stdin` flag (commit `ce66d06` + `2b5ff32`).
  Pre-existing; verify present.
- `app/core/config.py` — already supports per-process SSM key
  selection. Pre-existing; verify before deploy.
- Task-def update for `luciel-worker` to read its DSN from SSM
  `/luciel/production/worker_database_url` (lowercase — canonical)
  instead of `DATABASE_URL`.

### 4.0 — Pre-mint checklist (operator side, do NOT skip)

```powershell
# 1. Pre-flight ritual passes (§3 above), all 5 blocks green.
# 2. Authenticator app is open with the Luciel-MFA TOTP visible.
# 3. The dev `luciel-admin` AWS profile is the active default profile
#    (`aws sts get-caller-identity` returns user `luciel-admin`,
#    account 729005488042). The ceremony assumes
#    `luciel-mint-operator-role` FROM `luciel-admin`.
# 4. The branch is clean and on `step-28-hardening-impl` at the head
#    that contains Commits A + D + repo-hygiene (`86239ab` or later).
# 5. The repo working tree has no uncommitted changes.
```

### 4.0.5 — Verify mint-role IAM policy (added v2.1)

The mint script's `preflight_ssm_writable` (`scripts/mint_worker_db_password_ssm.py:283`)
calls `ssm:GetParameterHistory` on the worker SSM path BEFORE any DB
mutation. This is the atomicity defense for the
"DB-changed-but-SSM-write-failed" failure mode (drift
`D-mint-script-leaks-admin-dsn-via-error-body-2026-05-03` resolution
rationale, see commit `2b5ff32`).

For the pre-flight to pass under the Option 3 ceremony, the assumed
role must hold:

| Action | Resource | Statement Sid |
|---|---|---|
| `ssm:GetParameter` | `/luciel/database-url` | `ReadAdminDsnFromSsm` |
| `ssm:DescribeParameters` | tag-conditioned | `DescribeAdminDsnParameter` |
| `kms:Decrypt` | `*` via SSM | `DecryptAdminDsnViaSsm` |
| `ssm:GetParameter`, `ssm:GetParameterHistory`, `ssm:PutParameter` | `/luciel/production/worker_database_url` | `ReadWorkerSsmForPreflightAndMint` |
| `kms:Encrypt`, `kms:GenerateDataKey` | `*` via SSM | `EncryptWorkerSsmSecureStringViaSsm` |

Canonical IaC source-of-truth:
`infra/iam/luciel-mint-operator-role-permission-policy.json`. The
live AWS policy MUST match this file byte-for-byte.

**Verify the live policy matches:**

```powershell
# 1. Get the live policy from AWS
aws iam get-role-policy `
  --role-name luciel-mint-operator-role `
  --policy-name luciel-mint-operator-permissions `
  --region ca-central-1 `
  --output json | ConvertFrom-Json | Select-Object -ExpandProperty PolicyDocument `
  | ConvertTo-Json -Depth 10

# 2. Compare against the IaC file
Get-Content infra/iam/luciel-mint-operator-role-permission-policy.json
```

If the live policy is missing the `ReadWorkerSsmForPreflightAndMint`
or `EncryptWorkerSsmSecureStringViaSsm` statements, apply the IaC
file:

```powershell
aws iam put-role-policy `
  --role-name luciel-mint-operator-role `
  --policy-name luciel-mint-operator-permissions `
  --policy-document file://infra/iam/luciel-mint-operator-role-permission-policy.json `
  --region ca-central-1
```

Then re-run the verify step to confirm the apply succeeded. Only
proceed to §4.2 once verify passes.

**Why this is safe to apply directly from `luciel-admin`:** the
assumed-role-with-MFA boundary applies to the mint *operation*, not
to the *configuration* of the role. `luciel-admin` is the IAM-admin
identity and creating/updating policies is its expected duty. P3-K's
original trust-policy creation also ran under `luciel-admin` without
MFA-AssumeRole gating.

**Why we do NOT widen the policy further:** every action is scoped
to exactly one resource ARN (the worker SSM path) or KMS-via-SSM
conditioned. The role still cannot write to the admin DSN, cannot
read or write any other `/luciel/*` path, cannot decrypt KMS keys
that aren't via SSM. The blast radius of a compromised mint session
is: read admin DSN once, write one specific worker DSN once, within
a 1-hour MFA-gated window.

### 4.0.6 — Pattern N mint architecture (added v3)

**Why this section exists:** the Option 3 ceremony as designed in v2
put the mint script's `psycopg.connect(admin_dsn)` call on the
operator's laptop. Production RDS is in a private VPC subnet with no
public ingress; the laptop has no path to it. The v2 ceremony's
first real-run attempt aborted at `ConnectionTimeout`. v3 closes
that boundary by moving the mint into the VPC.

**Architectural shape (P3-S.a — dedicated Fargate task):**

| Layer | What runs | Identity used | Why |
|---|---|---|---|
| Operator laptop | `scripts/mint-via-fargate-task.ps1` | `luciel-admin` IAM user (then assumes `luciel-mint-operator-role` with MFA) | MFA-gated entry point; only purpose is to scope the blast radius of the `aws ecs run-task` call |
| ECS Fargate task | `python -m scripts.mint_worker_db_password_ssm` (same hardened script as v2) | Task role `luciel-ecs-mint-role` (5 statements, same as operator role post-`e1154bd`) | Holds the IAM rights for admin-DSN read + worker-SSM write + KMS-via-SSM. The container is what actually talks to RDS and SSM, inside the VPC. |
| Container ENI | runs in production application subnets (not RDS DB subnets — those have no SSM VPC endpoint) | inherits task role | RDS reachability via VPC routing; SSM/SSM-Messages/EC2-Messages reachable via interface endpoints in the application subnets |

**Crucial network detail:** the application subnets and the RDS DB
subnets are **distinct**. Only the application subnets have the
SSM/ssmmessages/ec2messages interface endpoints; the RDS subnets are
deliberately locked-down with no SSM endpoint and no NAT egress (the
RDS instance itself doesn't need them). A Fargate task launched into
the RDS subnets fails at startup with
`ResourceInitializationError: unable to pull secrets ... context deadline exceeded`.
Verify subnet identity by reading the production `luciel-backend-service`
network config, **never** by inferring from RDS metadata:

```powershell
aws ecs describe-services --cluster luciel-cluster `
  --services luciel-backend-service `
  --region ca-central-1 `
  --query "services[0].networkConfiguration.awsvpcConfiguration"
```

Canonical application subnets (used by `luciel-backend-service` and
baked into `mint-via-fargate-task.ps1` defaults):

```
subnet-0e54df62d1a4463bc
subnet-0e95d953fd553cbd1
```

Application SG with egress to RDS on 5432: `sg-0f2e317f987925601`.

**Crucial admin-DSN-delivery detail:** the admin DSN no longer
traverses the operator laptop. It is delivered to the container via
the task definition's `secrets:` block, which ECS resolves through
the SSM endpoint inside the VPC. The laptop only sees CloudWatch log
lines, which the mint script has already passed through
`_redact_dsn_in_message`.

**Code/IaC artifacts that land with v3:**

| Artifact | Purpose | Status |
|---|---|---|
| `infra/iam/luciel-ecs-mint-role-trust-policy.json` | Trust policy for the new task role; lets `ecs-tasks.amazonaws.com` assume it. Byte-identical pattern to `luciel-ecs-migrate-role`. | Half 1 (this commit) |
| `infra/iam/luciel-ecs-mint-role-permission-policy.json` | Permission policy for the new task role; same 5 statements as `luciel-mint-operator-role-permission-policy.json` | Half 1 (this commit) |
| `mint-td-rev1.json` | Source-of-truth task definition for `luciel-mint:1`. Family `luciel-mint`, image digest pinned, task role `luciel-ecs-mint-role`, execution role `luciel-ecs-execution-role`, env vars `WORKER_HOST` / `WORKER_DB_NAME` / `WORKER_SSM_PATH` / `MINT_DRY_RUN=true` (safe default), secret `ADMIN_DSN` from `/luciel/database-url`, command `python -m scripts.mint_worker_db_password_ssm`, log stream prefix `mint`. | Half 1 (this commit) |
| `scripts/mint-via-fargate-task.ps1` | Operator helper that supersedes `mint-with-assumed-role.ps1` for prod mint. Same MFA + AssumeRole prelude; body is `aws ecs run-task` + CloudWatch tail + describe-tasks polling. | Half 1 (this commit) |
| `scripts/mint_worker_db_password_ssm.py` | Hardened mint script. Patched in this commit to read `ADMIN_DSN` / `WORKER_HOST` / `WORKER_DB_NAME` / `WORKER_SSM_PATH` from env vars (in addition to existing CLI flags) so it works under task-def `secrets:` and `environment:`; plus dry-run preflight fix to exercise `preflight_ssm_writable` AND a connection-only `psycopg.connect(...).close()` before the dry-run early return. | Half 1 (this commit) |
| `scripts/mint-with-assumed-role.ps1` | v2 / v2.1 helper. **Kept on disk for reference; do NOT invoke for prod mint** — it cannot reach RDS. | retained, marked superseded |

**Half 1 vs Half 2:** this commit (Half 1) lands the **code and IaC**
in the repo with **zero AWS apply**. Half 2 (separate commit, gated
on operator scheduling) executes the live AWS apply:

1. `aws iam create-role` for `luciel-ecs-mint-role` with the trust
   policy file.
2. `aws iam put-role-policy` with the permission policy file.
3. `aws ecs register-task-definition --cli-input-json file://mint-td-rev1.json`.
4. Smoke test: `.\scripts\mint-via-fargate-task.ps1` (default dry-run
   mode — task-def safe default `MINT_DRY_RUN=true`). Confirm exit
   code 0, confirm `preflight_ssm_writable` passed, confirm
   connection-only DB connect passed (this is the layer that v2
   skipped and that v3 fixes).
5. Real mint: `.\scripts\mint-via-fargate-task.ps1 -RealRun`.
   Confirm exit code 0. Verify SSM write:
   `aws ssm get-parameter --name /luciel/production/worker_database_url --query "Parameter.Version"`
   (do NOT pass `--with-decryption`).

**Smoke gate (REQUIRED before Half 2 step 5):** the smoke test in
step 4 above MUST run a non-dry-run-equivalent connection to RDS,
not just the AssumeRole + SSM-read path. Every prior smoke test
skipped this layer (because the old `--dry-run` returned before the
DB connect), which is the specific reason the architectural
boundary survived to discovery. The `mint_worker_db_password_ssm.py`
patch in this commit makes `--dry-run` exercise both
`preflight_ssm_writable` and a connection-only `psycopg.connect`
before returning; with that patch, Half 2 step 4's exit-0 is real
proof of end-to-end reachability.

**Cross-references:**

- `docs/PHASE_3_COMPLIANCE_BACKLOG.md` P3-S
- `docs/recaps/2026-05-04-mint-architectural-boundary-pause.md`
- `docs/runbooks/operator-patterns.md` Pattern N
- `docs/incidents/2026-05-05-admin-dsn-disclosed-in-chat.md`
  (rotation chain that landed before this commit)

### 4.1 — Recon (read-only, safe)

```powershell
# Confirm luciel_worker role exists and has the expected grants.
# Run via Pattern N one-shot (luciel-migrate:N) — do NOT add temporary
# IAM ingress to RDS for psql from a laptop.
aws ecs run-task --cluster luciel-cluster `
  --task-definition luciel-migrate:N `
  --launch-type FARGATE `
  --network-configuration "awsvpcConfiguration={awsvpcConfiguration={subnets=[<private-subnet-id>],securityGroups=[<migrate-sg-id>],assignPublicIp=DISABLED}}" `
  --overrides '{
    "containerOverrides":[{
      "name":"luciel-migrate",
      "command":["python","-c","from app.core.config import settings; from sqlalchemy import create_engine, text; e=create_engine(settings.database_url); c=e.connect(); print(c.execute(text(\"SELECT rolname, rolsuper, rolcanlogin FROM pg_roles WHERE rolname IN (''luciel_admin'',''luciel_worker'')\")).fetchall())"]
    }]
  }' --region ca-central-1
```

Expected: two rows. `luciel_admin` super=t login=t,
`luciel_worker` super=f login=t.

**Also confirm SSM target is empty (or non-existent) before mint:**

```powershell
aws ssm get-parameter `
  --name /luciel/production/worker_database_url `
  --region ca-central-1
# Expect: ParameterNotFound (the canonical post-P3-K state — nothing
# was ever written here). If the parameter exists, STOP and audit
# why before proceeding.
```

### 4.2 — Mint via Pattern N Fargate ceremony (the prod-touching action)

> **v3 SUPERSESSION NOTICE.** The original §4.2 (Option 3 ceremony
> via `scripts/mint-with-assumed-role.ps1`) is **architecturally
> superseded** by the Pattern N variant. `mint-with-assumed-role.ps1`
> cannot reach RDS from the operator laptop — the production VPC
> posture forbids it by design (see §4.0.6 above and
> `D-option-3-ceremony-cannot-reach-private-rds-from-laptop-2026-05-04`).
> The new entry point is `scripts/mint-via-fargate-task.ps1`, which
> launches the mint as a one-shot Fargate task in the application
> subnets. The text below has been rewritten accordingly. The
> original v2 / v2.1 ceremony text is preserved verbatim in git
> history at commit `e1154bd` for forensic reference.

**Important:** the mint script is invoked **only** through
`mint-via-fargate-task.ps1`. Direct invocation of
`python scripts/mint_worker_db_password_ssm.py` from a laptop is
**not** part of this runbook (and structurally cannot succeed —
the laptop has no route to RDS). The script may also be invoked
directly inside the container by `aws ecs run-task` overrides for
recovery scenarios, but the Pattern N helper is the ONLY supported
routine entry point for Phase 2 Commit 4.

**Where parameters live now:** under v3, the canonical worker host,
DB name, and SSM path are baked into the `luciel-mint:1` task
definition (`mint-td-rev1.json`). The helper exposes optional
per-invocation overrides (`-WorkerHost` / `-WorkerDbName` /
`-WorkerSsmPath`) for non-production targets, but Phase 2 Commit 4
uses the task-def defaults. Helper-default subnets and SG are also
baked to the production application subnets and the application SG
(canonical values in §4.0.6 above).

**Step 1 — Dry-run ceremony (no DB or SSM mutation):**

```powershell
.\scripts\mint-via-fargate-task.ps1
# - Prompts for MFA TOTP.
# - Assumes luciel-mint-operator-role on the laptop for 1 h.
# - Issues `aws ecs run-task` against luciel-mint:N with task-def
#   default MINT_DRY_RUN=true. Container launches in application
#   subnets, picks up task role luciel-ecs-mint-role.
# - Container runs `python -m scripts.mint_worker_db_password_ssm`,
#   resolves ADMIN_DSN from the SSM `secrets:` injection, runs
#   pre-flight (SSM-writable + DB-connect-only), exits 0 without
#   any ALTER ROLE or SSM put.
# - Helper polls describe-tasks + tails CloudWatch /ecs/luciel-backend
#   stream `mint/luciel-backend/<task-id>`, prints the redacted
#   stdout/stderr to the operator's terminal.
# - Helper clears assumed laptop credentials on exit.
#
# Expected exit: container exitCode 0, with log lines confirming
# pre-flight passed AND DB connect-only succeeded (this is the
# layer the v2 ceremony skipped).
```

If the dry-run fails for any reason (MFA expired, role-trust
rejection, ECS run-task failure, container ResourceInitializationError,
mint-script error), **stop and diagnose**. Do not proceed to the real
run until dry-run is green AND the dry-run logs explicitly show the
DB connect-only succeeded.

**Step 2 — Real ceremony (writes to RDS + SSM):**

```powershell
.\scripts\mint-via-fargate-task.ps1 -RealRun
# - Same MFA + AssumeRole flow as dry-run.
# - -RealRun overrides the task-def's MINT_DRY_RUN=true to false.
# - Container generates a fresh 32-char password, runs
#   ALTER USER luciel_worker WITH PASSWORD '...' on RDS, builds
#   the SQLAlchemy URL, and writes the SSM SecureString at
#   /luciel/production/worker_database_url (lowercase).
# - Password is NEVER printed to container stdout, NEVER echoed to
#   CloudWatch, NEVER returned to the laptop. Pattern E preserved.
# - Helper polls + tails as in dry-run.
# - Assumed laptop credentials cleared on exit.
```

**Why -RealRun is a mandatory switch (not a -DryRun-default-off
switch):** the task-def itself ships with `MINT_DRY_RUN=true` baked
in as the safe default. Even if someone strips the helper script
and calls `aws ecs run-task` directly with no overrides, the
resulting task is a no-op, not a real mint. `-RealRun` on the
helper is the explicit override that flips the env var to `false`
for that one task launch.

**Post-mint confirmation:**

```powershell
# Confirm the SSM parameter now exists. The assumed role is gone, so
# this read goes through the operator's default identity (luciel-admin).
aws ssm get-parameter `
  --name /luciel/production/worker_database_url `
  --region ca-central-1 `
  --query "Parameter.[Name,Type,Version]" --output table
# Expect: Name=/luciel/production/worker_database_url, Type=SecureString,
# Version=1.
#
# DO NOT add --with-decryption — there is no operator-side reason to
# decrypt the worker DSN. Only the worker task role needs that grant.
```

### 4.3 — Update worker task-def to read worker_database_url

```powershell
# Pull current task-def
aws ecs describe-task-definition --task-definition luciel-worker `
  --region ca-central-1 --query "taskDefinition" `
  > taskdef-worker-current.json

# Edit taskdef-worker-current.json:
#  - In containerDefinitions[0].secrets, replace the entry where
#    name == "DATABASE_URL" so its valueFrom points to
#    arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/worker_database_url
#    (keep the env var name DATABASE_URL — Celery code reads that).
#  - Strip read-only fields (taskDefinitionArn, revision, status,
#    requiresAttributes, compatibilities, registeredAt, registeredBy).

aws ecs register-task-definition `
  --cli-input-json file://taskdef-worker-new.json `
  --region ca-central-1 `
  --query "taskDefinition.taskDefinitionArn" --output text
```

The task-def file is in `.gitignore` (`*-task-def-v*.json`) so it
is local-only by design. Capture the new revision number for §4.4.

### 4.4 — Roll the worker service onto the new task-def

```powershell
aws ecs update-service `
  --cluster luciel-cluster `
  --service luciel-worker-service `
  --task-definition luciel-worker:<new-revision> `
  --force-new-deployment `
  --region ca-central-1 `
  --query "service.deployments[0].rolloutState" --output text
```

Watch deployment to `COMPLETED`:

```powershell
aws ecs describe-services --cluster luciel-cluster `
  --services luciel-worker-service --region ca-central-1 `
  --query "services[0].deployments[*].[status,rolloutState,runningCount,desiredCount]" `
  --output table
```

### 4.5 — Verify worker is now connecting as luciel_worker

```powershell
# CloudWatch logs for the new task — expect normal Celery boot, no
# permission errors.
aws logs tail /ecs/luciel-worker --since 5m --region ca-central-1

# RDS recon: who's connected?
# Use Pattern N to run:
#   SELECT usename, application_name, count(*) FROM pg_stat_activity
#   GROUP BY 1,2;
# Expect to see luciel_worker rows from the worker tasks.
```

### 4.6 — Smoke a write that exercises the role

Trigger a memory-extraction Celery task in prod (pick a low-traffic
luciel_instance) and confirm:
- The task succeeds (worker can INSERT into `memory_items`).
- An `admin_audit_logs` row is written with the worker's
  AuditContext (Pattern: worker key prefix preserved).
- A `psql -U luciel_worker -c "DELETE FROM admin_audit_logs"`
  attempted via Pattern N **fails with permission denied** (the
  whole point of the swap).

### 4.7 — (intentionally removed: `luciel_admin` rotation)

The original v1 of this runbook included an §4.7 to rotate the
`luciel_admin` master password and update `/luciel/production/database_url`.
**That work was completed by P3-H on 2026-05-03 23:56 UTC and is no
longer part of Commit 4.** See `docs/runbooks/step-28-p3-h-rotate-and-purge.md`
for the executed runbook and the canonical recap drift register for
the resolution evidence.

### 4.8 — Rollback (Commit 4)

If anything regresses post-mint:

```powershell
# Roll worker service back to the prior task-def revision.
aws ecs update-service --cluster luciel-cluster `
  --service luciel-worker-service `
  --task-definition luciel-worker:<previous-revision> `
  --force-new-deployment --region ca-central-1
```

The SSM parameter `/luciel/production/worker_database_url` survives
the service rollback (it was never read by the prior worker
revision). It can be deleted manually with
`aws ssm delete-parameter` if a clean re-mint is needed; the next
`mint-with-assumed-role.ps1` real run would then create v1 again.

**Note:** the worker DSN minted in §4.2 is NOT rollback-safe in the
sense that `luciel_worker`'s old password is destroyed by the
`ALTER USER` statement. If the rollout in §4.4 fails, the prior
worker task-def revision points at `DATABASE_URL` (admin DSN
post-P3-H rotation), which still works — the rollback path uses
`luciel_admin`, not the now-rotated `luciel_worker`. This is the
intended Phase-2 transitional behavior; after Commit 4 stabilizes
for 7 days, the admin DSN read by the worker task role can be
revoked entirely (Phase 3 follow-up).

---

## 5 — Commit 5 — CloudWatch alarms + SNS pipeline

**Goal:** alarm before users notice. Five alarms, one SNS topic
(`luciel-prod-alerts`), email subscription to Aryan.

**Alarms to land:**

| Alarm | Metric | Threshold | Evaluation |
|---|---|---|---|
| `luciel-sqs-backlog-high` | `ApproximateNumberOfMessagesVisible` on `luciel-memory-tasks` | > 50 | 2 of 2 datapoints, 5-min period |
| `luciel-sqs-dlq-nonzero` | `ApproximateNumberOfMessagesVisible` on `luciel-memory-dlq` | > 0 | 1 of 1, 1-min period |
| `luciel-rds-conn-high` | `DatabaseConnections` on `luciel-prod-db` | > 80% of `max_connections` (compute once, hardcode threshold) | 2 of 2, 5-min |
| `luciel-ecs-cpu-high` | `CPUUtilization` per service | > 80% | 2 of 2, 5-min |
| `luciel-alb-5xx-high` | `HTTPCode_Target_5XX_Count` on backend TG | > 1% of request count | 2 of 2, 5-min |

**Code/IaC artifacts to land:**
- `infra/cloudwatch/alarms.yaml` (CloudFormation) — single template
  declaring the SNS topic + 5 alarms with parameters for queue ARNs,
  RDS instance id, ECS service names, ALB target group.
- New runbook section here describing the deploy and the snooze
  protocol (an alarm fires → you snooze for 30 min while debugging,
  not forever).

### 5.1 Recon

```powershell
# Confirm queue ARNs
aws sqs list-queues --queue-name-prefix luciel-memory --region ca-central-1

# Confirm RDS max_connections
aws rds describe-db-parameters `
  --db-parameter-group-name <luciel-prod-db's pg> `
  --query "Parameters[?ParameterName=='max_connections'].ParameterValue" `
  --output text --region ca-central-1
```

### 5.2 Deploy

```powershell
aws cloudformation deploy `
  --template-file infra/cloudwatch/alarms.yaml `
  --stack-name luciel-prod-alarms `
  --parameter-overrides `
    AlertEmail=aryans.www@gmail.com `
    SqsMainQueueName=luciel-memory-tasks `
    SqsDlqQueueName=luciel-memory-dlq `
    RdsInstanceId=luciel-prod-db `
    EcsClusterName=luciel-cluster `
    EcsBackendService=luciel-backend-service `
    EcsWorkerService=luciel-worker-service `
    AlbTargetGroupFullName=<full TG name> `
    RdsConnectionThreshold=<computed 80%> `
  --region ca-central-1
```

Confirm the SNS subscription email and click the AWS confirmation
link.

### 5.3 Verify each alarm fires when expected

For each alarm, manually breach in dev once (e.g. enqueue 60 SQS
messages with no consumer; expect SNS email within 6 minutes). Do
**not** synthetic-breach in prod — use dev/staging, then trust the
template parity.

### 5.4 Rollback (Commit 5)

```powershell
aws cloudformation delete-stack `
  --stack-name luciel-prod-alarms --region ca-central-1
```

Pure-additive commit; rollback is clean.

---

## 6 — Commit 6 — ECS auto-scaling target tracking

**Goal:** web service scales on CPU, worker service scales on SQS
queue depth.

**Targets:**

| Service | Metric | Target | Min | Max |
|---|---|---|---|---|
| `luciel-backend-service` | `ECSServiceAverageCPUUtilization` | 50% | 1 | 4 |
| `luciel-worker-service` | Custom: `ApproximateNumberOfMessagesVisible` / running task count, target = 10 messages/task | 1 | 4 |

**Code/IaC artifacts:**
- `infra/autoscaling/web-cpu.yaml` — `AWS::ApplicationAutoScaling::ScalableTarget`
  + `AWS::ApplicationAutoScaling::ScalingPolicy` (TargetTrackingScaling).
- `infra/autoscaling/worker-queue.yaml` — same pattern, custom
  CloudWatch metric math expression as the scaling metric.

### 6.1 Deploy

```powershell
aws cloudformation deploy `
  --template-file infra/autoscaling/web-cpu.yaml `
  --stack-name luciel-prod-autoscale-web `
  --parameter-overrides ClusterName=luciel-cluster ServiceName=luciel-backend-service `
  --region ca-central-1 --capabilities CAPABILITY_IAM

aws cloudformation deploy `
  --template-file infra/autoscaling/worker-queue.yaml `
  --stack-name luciel-prod-autoscale-worker `
  --parameter-overrides ClusterName=luciel-cluster ServiceName=luciel-worker-service QueueName=luciel-memory-tasks `
  --region ca-central-1 --capabilities CAPABILITY_IAM
```

### 6.2 Verify

```powershell
aws application-autoscaling describe-scalable-targets `
  --service-namespace ecs --region ca-central-1 `
  --query "ScalableTargets[?ResourceId=='service/luciel-cluster/luciel-backend-service']"

aws application-autoscaling describe-scaling-policies `
  --service-namespace ecs --region ca-central-1
```

Then load-test dev (k6 or hey) and confirm the scale-out alarm fires
+ desired count rises.

### 6.3 Rollback (Commit 6)

Delete both stacks. ECS reverts to the static `desiredCount` set on
the service, which is what we ran on through Phase 1.

---

## 7 — Commit 7 — Container-level health checks

**Goal:** ECS replaces a sick task before the ALB target group
notices. Belt-and-suspenders against the existing ALB target health
check.

**Probes:**

| Container | Command | Interval | Timeout | Retries | StartPeriod |
|---|---|---|---|---|---|
| Backend (web) | `curl -fsS http://localhost:8000/health || exit 1` | 30 s | 5 s | 3 | 30 s |
| Worker | `celery -A app.worker.celery_app inspect ping -d celery@$(hostname) \|\| exit 1` | 60 s | 10 s | 3 | 60 s |

**Code/IaC artifacts:**
- Updated task-def JSON for `luciel-backend` and `luciel-worker`,
  adding `containerDefinitions[0].healthCheck`.
- Image must contain `curl` (backend already does — confirm at
  Dockerfile review). Worker image already has Celery CLI.

### 7.1 Deploy

```powershell
# Pull current task-defs
aws ecs describe-task-definition --task-definition luciel-backend `
  --region ca-central-1 --query "taskDefinition" > td-backend.json
aws ecs describe-task-definition --task-definition luciel-worker `
  --region ca-central-1 --query "taskDefinition" > td-worker.json

# Edit each: add the healthCheck block per the table above.
# Strip read-only fields. Register new revisions.
aws ecs register-task-definition --cli-input-json file://td-backend-new.json `
  --region ca-central-1 --query "taskDefinition.revision" --output text
aws ecs register-task-definition --cli-input-json file://td-worker-new.json `
  --region ca-central-1 --query "taskDefinition.revision" --output text

# Roll services
aws ecs update-service --cluster luciel-cluster --service luciel-backend-service `
  --task-definition luciel-backend:<new> --force-new-deployment --region ca-central-1
aws ecs update-service --cluster luciel-cluster --service luciel-worker-service `
  --task-definition luciel-worker:<new> --force-new-deployment --region ca-central-1
```

### 7.2 Verify

```powershell
# Tasks should reach healthStatus: HEALTHY
aws ecs list-tasks --cluster luciel-cluster --service-name luciel-backend-service `
  --region ca-central-1 --query "taskArns[]" --output text |
  ForEach-Object {
    aws ecs describe-tasks --cluster luciel-cluster --tasks $_ `
      --region ca-central-1 `
      --query "tasks[0].[lastStatus,healthStatus]" --output text
  }
```

Also confirm an intentionally broken container (kill the process
inside) gets killed and replaced by ECS.

### 7.3 Rollback (Commit 7)

Roll services back to the previous task-def revision. Health checks
are removed; behavior reverts to ALB-target-group-only (Phase 1
state).

---

## 8 — Post-Phase-2 verification gate

After Commits 4, 5, 6, 7 are all live in prod:

1. `python -m app.verification` against prod admin endpoints — 19/19
   green.
2. `aws cloudwatch describe-alarms --alarm-names <all 5>` — all in
   `OK` state.
3. `aws application-autoscaling describe-scalable-targets ...` —
   both targets registered.
4. `aws ecs describe-services ...` — both services running with
   healthCheck-enabled task-def revisions.
5. `pg_stat_activity` recon — every connection from worker tasks is
   `usename = 'luciel_worker'`. Zero worker connections as
   `luciel_admin`.
6. Manual end-to-end test: send a chat through a luciel_instance,
   confirm:
   - `admin_audit_logs` row written with worker AuditContext.
   - `memory_items` extraction completes.
   - No CloudWatch alarms fired during the run.
7. Update `docs/CANONICAL_RECAP.md` Section 3 with Phase 2 commits +
   prod state, tag the merge commit `step-28-phase-2-complete`.

---

## 9 — Phase-2 close exit criteria

**Phase 2 is complete when ALL of the following hold:**

- [x] Pillar 13 19/19 green on dev (Commit 3 + Commit 2 audit-log
      mount)
- [x] Audit-log API mounted, reviewed, and PII-redacted (Commits 2 +
      2b)
- [x] Retention purges batched (Commit 8)
- [ ] Worker connects to RDS as `luciel_worker`, NOT `luciel_admin`
      (Commit 4)
- [ ] `luciel_admin` password rotated (Commit 4)
- [ ] 5 CloudWatch alarms armed, SNS subscription confirmed
      (Commit 5)
- [ ] ECS auto-scaling live for backend + worker (Commit 6)
- [ ] Container healthChecks live for backend + worker (Commit 7)
- [ ] Prod `python -m app.verification` 19/19 green
- [ ] `docs/CANONICAL_RECAP.md` updated, tag
      `step-28-phase-2-complete` pushed

When all boxes check, Phase 2 closes and the canonical recap moves
on to Phase 3 (hygiene) or Step 30b (chat widget — REMAX trial
unblock), whichever the user prioritizes.
