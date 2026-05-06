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

**Prerequisite gate (all six must be ✅ before §4.2 mint):**

| Prereq | Status | Evidence |
|---|---|---|
| P3-J — MFA on `luciel-admin` | ✅ 2026-05-03 23:48 UTC | `arn:aws:iam::729005488042:mfa/Luciel-MFA` |
| P3-K — `luciel-mint-operator-role` | ✅ 2026-05-04 00:14 UTC | role + trust policy + `MaxSessionDuration: 3600` |
| P3-G — migrate role `ssm:GetParameterHistory` | ✅ 2026-05-03 ≈20:09 EDT | live policy has 6 SSM actions |
| P3-H — leaked `LucielDB2026Secure` rotated | ✅ 2026-05-03 23:56 UTC | SSM v1 → v2 + log stream deleted |
| P3-K-followup — mint role has worker-SSM write | ✅ 2026-05-04 (`e1154bd`) | live policy has 5 statements |
| P3-S Half 2 Steps 1–3 — Pattern N infra: `luciel-ecs-mint-role` + task-def `luciel-mint:1` | ✅ 2026-05-05 morning | RoleId AROA2TPA466VKVXLOBI2C; task-def ACTIVE |
| **P3-S-followup — operator role has `ecs:RunTask` + `iam:PassRole` + DescribeTasks/StopTask + log-read** | **⚠️ apply pending (this commit)** | policy file now has 9 statements; §4.0.7 below verifies post-apply |

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

> **✅ rev 3 dry-run GREEN — 2026-05-05 14:48:55 UTC.** Fargate task
> `ed5fd118ce024fa8b1e1cce15552eee3` launched on `luciel-mint:3`,
> exitCode 0, CloudWatch stream
> `mint/luciel-backend/ed5fd118ce024fa8b1e1cce15552eee3` confirmed
> the new four-stage pre-flight callout verbatim:
> `(pre-flight SSM-writable + first-mint-or-force-rotate + DB connect + role-state PASSED)`.
> The explicit phrasing is the structural proof that the read-only
> `pg_roles` SELECT actually executed in dry-run, closing
> `D-dry-run-validates-subset-of-real-run-pg-authid-not-exercised-2026-05-05`.
> `pw_fingerprint cd9a489dd131` (throwaway dry-run value),
> `force_rotate False`. **Zero production state mutated** — explicit
> `DRY RUN -- no Postgres or SSM writes performed` line; SSM
> `/luciel/production/worker_database_url` remained `ParameterNotFound`
> post-dry-run. Pattern E redaction held; no `<DSN-REDACTED>` strings
> in stdout. One MFA TOTP burned (516097), used productively. rev 3
> is now the canonical entry point.
>
> **Historical — rev 2 dry-run, 2026-05-05 14:16:39 UTC — GREEN but
> incomplete coverage (audit history, do not re-run):** Fargate task
> `33908e96941d4dbda45594f241565c3b` on `luciel-mint:2`, exitCode 0,
> log message `(pre-flight SSM-writable + DB connect-only PASSED)`.
> Coverage was incomplete because rev 2's dry-run path returned
> before `verify_role_state()`, so the `pg_authid` privilege gap that
> later bit Step 5 first-attempt was invisible at this stage. Drift
> `D-mint-script-uses-pg-authid-not-readable-on-rds-2026-05-05`
> resolved by Commit `0cd87be` + `65f8996`; drift
> `D-dry-run-validates-subset-of-real-run-pg-authid-not-exercised-2026-05-05`
> resolved by extending pre-flight Layer 2 to also run
> `verify_role_state(_preflight_conn)` — dry-run = real-run minus
> mutations is now a structural invariant.
>
> **Historical — rev 2 supersession (audit history):** the rev-2 cycle
> required a fresh image (digest `sha256:dcced3566...`) and a new
> task-def `mint-td-rev3.json` because the patched script had to
> ship inside the container. Rev 1 and rev 2 stayed ACTIVE for
> rollback. The rev-3 cycle ran in this exact order: advisor
> `git push` → operator `git fetch && git pull` → ECR login →
> `docker build` → `docker tag` + `docker push` → operator
> `git pull` rev3-td → `aws ecs register-task-definition
> --cli-input-json file://mint-td-rev3.json` (luciel-mint:3 ACTIVE) →
> Step 1 dry-run on rev 3 (above).

> **Known issue — helper bails after task STOPPED.** `scripts/mint-via-fargate-task.ps1`
> line 394 calls `aws @logArgs 2>$null` to auto-tail CloudWatch logs.
> PowerShell's native-command stderr handling raises
> `NativeCommandError` even when AWS CLI's stderr is non-empty for
> non-error reasons, and `2>$null` does NOT suppress it. **Ceremony
> correctness is unaffected** — the bail happens AFTER the task has
> reached STOPPED. If the helper bails, read logs manually:
>
> ```powershell
> aws logs get-log-events `
>   --log-group-name /ecs/luciel-backend `
>   --log-stream-name mint/luciel-backend/<task-id> `
>   --region ca-central-1 `
>   --query 'events[*].message' --output json
> ```
>
> The `<task-id>` is printed by the helper before the bail. Use the
> assumed-role credentials still in your shell (the helper clears
> them ONLY on graceful exit — if it bailed, run
> `Remove-Item Env:AWS_ACCESS_KEY_ID, Env:AWS_SECRET_ACCESS_KEY, Env:AWS_SESSION_TOKEN`
> after you finish reading logs). Drift
> `D-mint-helper-aws-stderr-causes-native-command-error-2026-05-05`
> tracks the helper polish; deferred to a standalone commit AFTER
> Step 5 GREEN.


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

> **✅ Step 5 GREEN — 2026-05-05 14:51:27 UTC (rev 3 real-run, mint
> COMPLETE).** Fargate task `27638cebbd8349f8bcb8d70e4c55714b`
> launched on `luciel-mint:3`, exitCode 0; CloudWatch banner
> `WORKER DB PASSWORD MINTED`; `pw_fingerprint ff89f2831b32` (sha256
> first 12 of the actual minted password — fingerprint is the
> integrity anchor, not a leak); `pw_length 43 chars` (matches
> `secrets.token_urlsafe(32)` base64url); `force_rotate False`
> (first-mint path). **Pattern E redaction held perfectly**: no
> `postgresql://` strings, no DSN, no raw password, no stack traces
> in container stdout/stderr. **Production state mutated as
> designed**: (a) `luciel_worker` ALTER ROLE PASSWORD committed to
> RDS master (old bootstrap password is now dead), (b) SSM
> SecureString `/luciel/production/worker_database_url` v1 created
> (KMS-encrypted, `LastModifiedDate 2026-05-05T14:51:29.156Z` — SSM
> PutParameter ran ~2s after ALTER ROLE; the script's atomicity
> defenses ensured no partial-mutation window). Both mutations
> independently verified via `aws ssm get-parameter` (metadata only,
> no `--with-decryption` per
> `D-admin-dsn-disclosed-in-chat-2026-05-05`):
> Name=`/luciel/production/worker_database_url`, Type=SecureString,
> Version=1. One MFA TOTP burned (074206), used productively.
> Assumed-role credentials cleared defensively post-ceremony. The
> `verify_first_mint_or_force_rotate` guard is now armed: with SSM v1
> present, any future mint without `--force-rotate` will be refused.
> **Phase 2 Commit 4 (worker DB role swap) is COMPLETE** — the
> Pattern N ceremony is proven end-to-end through real production
> mint.
>
> **Historical — Step 5 first-attempt on rev 2, 2026-05-05 ~10:30 EDT
> (audit history, do not re-run):** Task
> `6dc293a3be784529948e7a7dc0e73091` failed cleanly with
> `InsufficientPrivilege: permission denied for table pg_authid`
> (drift `D-mint-script-uses-pg-authid-not-readable-on-rds-2026-05-05`,
> resolved). Zero production state mutated — the SELECT failed
> BEFORE `ALTER ROLE`; SSM stayed `ParameterNotFound`; `luciel_worker`
> password unchanged. One MFA TOTP burned. The patch (`pg_roles` +
> SSM-presence rotation guard) landed in Commit `0cd87be`; the
> task-def refresh in Commit `65f8996`; rev 3 is the canonical
> entry point that succeeded above.


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

## 5 — Commit 5 — CloudWatch alarms + SNS pipeline ✅ SHIPPED 2026-05-05

**Status:** ✅ **SHIPPED 2026-05-05 ~16:49 EDT** via `ce0e3a2` (initial deploy failed on Period=90; see `D-cloudwatch-alarm-period-must-be-multiple-of-60-2026-05-05`), `f49eae4` (Period fix to 60), and operator stack-recreate cycle (`delete-stack` → `wait` → `deploy`). The shipped scope is **wider than the original 5-alarm sketch**: the actual stack is 7 alarms because the heartbeat alarm and the RDS free-storage alarm were both added during Commit 7 / Phase 2 hardening review.

**Shipped design (as deployed in stack `luciel-prod-alarms`):**

| Alarm | Metric | Threshold | Evaluation | Period |
|---|---|---|---|---|
| `luciel-worker-no-heartbeat` | `Luciel/Worker.HeartbeatTouchCount` (filtered from `/ecs/luciel-worker`) | Sum < 1 | 4 of 5 datapoints | 60s |
| `luciel-worker-unhealthy-task-count` | `AWS/ECS.RunningTaskCount` for `luciel-worker-service` | < 1 | 2 of 2 | 60s |
| `luciel-worker-error-log-rate` | `Luciel/Worker.ErrorLogLineCount` (filtered ERROR/CRITICAL/Traceback) | Sum > 5 | 2 of 2 | 300s |
| `luciel-rds-connection-count` | `AWS/RDS.DatabaseConnections` for `luciel-db` | > 90 (~80% of computed `max_connections ≈ 112` on db.t3.micro) | 1 of 1 | 300s |
| `luciel-rds-cpu` | `AWS/RDS.CPUUtilization` for `luciel-db` | > 85% | 2 of 2 | 300s |
| `luciel-rds-free-storage` | `AWS/RDS.FreeStorageSpace` for `luciel-db` | < 4 GiB (4294967296 bytes) on 20 GiB volume | 1 of 1 | 900s |
| `luciel-ssm-getparameter-failures` | `Luciel/Worker.SsmAccessFailureCount` (filtered from `/ecs/luciel-worker`) | Sum > 0 | 1 of 1 | 300s |

SNS topic: `luciel-prod-alerts` (account 729005488042, region ca-central-1). Email subscription to `aryans.www@gmail.com` confirmed by operator at 2026-05-05 ~16:49 EDT (subscription ARN `5ffadb96-1ad6-4b08-8ac1-0a551a8b43ad`). 3 log MetricFilters on `/ecs/luciel-worker` populate the custom metrics. Cost ~$0.70/month.

**Original 5-alarm sketch is intentionally NOT shipped — the as-shipped 7-alarm design is the source of truth.** The original sketch referenced SQS queues (`luciel-memory-tasks`, `luciel-memory-dlq`) and an ALB target group; none of those alarms are in the shipped stack because (a) the celery broker has not been verified as SQS in this session (see `D-celery-broker-not-verified-deferring-backlog-autoscaling-2026-05-05`) and (b) ALB 5xx alerting is deferred until backend autoscaling is in scope. Both should be re-evaluated in a follow-up commit.

### 5.0 Pre-flight (mandatory before any deploy)

```powershell
# Operator pulls latest from advisor branch
git fetch origin
git pull origin step-28-hardening-impl
git rev-parse HEAD  # Cross-check against advisor-provided SHA

# CFN template length audit (Description must be < 1024 chars)
awk '/^Description:/{print length($0)-13}' cfn/luciel-prod-alarms.yaml

# CloudWatch Period audit (catches Period values not in {10,20,30} or n%60==0)
grep -nE 'Period:' cfn/luciel-prod-alarms.yaml | awk '{print $NF}'
# Reject any value not 10, 20, 30, or a multiple of 60
```

### 5.1 Deploy (as actually executed)

```powershell
aws cloudformation deploy `
  --template-file cfn/luciel-prod-alarms.yaml `
  --stack-name luciel-prod-alarms `
  --region ca-central-1 `
  --no-fail-on-empty-changeset
```

The template defaults all parameters (AlertEmail, log group, cluster/service names, RDS instance id, thresholds), so no `--parameter-overrides` are required for the standard deploy.

**If the stack is in `ROLLBACK_COMPLETE` from a prior failed deploy, delete it first:**

```powershell
aws cloudformation delete-stack --stack-name luciel-prod-alarms --region ca-central-1
aws cloudformation wait stack-delete-complete --stack-name luciel-prod-alarms --region ca-central-1
# Then redeploy.
```

A `ROLLBACK_COMPLETE` stack cannot be re-created with the same name; this delete-then-redeploy is the only path forward.

### 5.2 Verify

```powershell
# (a) All 7 alarms registered with valid Periods
aws cloudwatch describe-alarms --alarm-name-prefix luciel- --region ca-central-1 `
  --query "MetricAlarms[].[AlarmName,StateValue,MetricName,Period]" --output table
# Expect: 7 rows, all Period values in {60, 300, 900}

# (b) SNS subscription confirmed (after operator clicks the email link)
aws sns list-subscriptions-by-topic `
  --topic-arn arn:aws:sns:ca-central-1:729005488042:luciel-prod-alerts `
  --region ca-central-1 `
  --query "Subscriptions[].[Protocol,Endpoint,SubscriptionArn]" --output table
# Expect: email | aryans.www@gmail.com | <real ARN, not 'PendingConfirmation'>

# (c) Heartbeat metric is being published by worker rev 11
aws cloudwatch get-metric-statistics `
  --namespace Luciel/Worker --metric-name HeartbeatTouchCount `
  --start-time (Get-Date).ToUniversalTime().AddMinutes(-10).ToString("yyyy-MM-ddTHH:mm:ssZ") `
  --end-time (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ") `
  --period 60 --statistics Sum --region ca-central-1 `
  --query "Datapoints[*].[Timestamp,Sum]" --output table
# Expect: ~4 per minute (matches 15s producer cadence)

# (d) Heartbeat alarm reaches OK after ~5 minutes of post-creation data
aws cloudwatch describe-alarms --alarm-names luciel-worker-no-heartbeat --region ca-central-1 `
  --query "MetricAlarms[0].[StateValue,StateReason,StateUpdatedTimestamp]" --output table
# Expect at 5 min post-create: StateValue = OK
```

**Cold-start note:** The heartbeat alarm will briefly transition `INSUFFICIENT_DATA → ALARM → OK` immediately after stack creation. This is expected: pre-creation evaluation buckets are treated as breaching (TreatMissingData=breaching), so the first ~2-3 minutes look broken. The alarm self-clears within 2 minutes once enough post-creation datapoints land. Do NOT interpret the cold-start ALARM as a producer failure — cross-check via `get-metric-statistics` first.

Production synthetic-breach testing is intentionally OUT of scope. Don't enqueue test failures or kill workers in prod. The heartbeat → alarm → SNS → email path is implicitly verified end-to-end by the cold-start ALARM → OK transition itself — it proves the metric flows from worker to alarm and the alarm state machine is functioning. Synthetic-breach testing of the other alarms (RDS connections, RDS CPU, etc.) belongs in a follow-up commit using a dev RDS instance.

### 5.3 Rollback (Commit 5)

```powershell
aws cloudformation delete-stack --stack-name luciel-prod-alarms --region ca-central-1
aws cloudformation wait stack-delete-complete --stack-name luciel-prod-alarms --region ca-central-1
```

Pure-additive deploy; rollback is clean. SNS topic, subscription, log MetricFilters, and all 7 alarms are deleted as one atomic operation.

---

## 6 — Commit 6 — ECS Application Auto Scaling on worker service ✅ SHIPPED 2026-05-05

**Status:** ✅ **SHIPPED 2026-05-05 ~17:35 EDT** via `e7b5f95` (initial deploy failed on `Description` length — see `D-cfn-description-1024-char-limit-2026-05-05`) and `69d1a3a` (Description trimmed, deploy succeeded). Original design called for both web CPU autoscaling AND worker SQS-backlog autoscaling; **the shipped scope is narrower than originally planned**:

| Service | Status | Reason |
|---|---|---|
| `luciel-worker-service` | ✅ SHIPPED with CPU TargetTracking at 60%, capacity 1–4 | Broker-agnostic baseline. Sufficient for current load profile. |
| `luciel-backend-service` | ⏭ Out of scope for this commit | Phase 1 already delivered backend through ALB; revisit in a follow-up commit if needed. |
| Worker backlog policy (SQS or Redis LLEN) | 🟡 DEFERRED to follow-up commit | Celery broker (Redis vs SQS) was not verified in-session; authoring a backlog policy against the wrong broker would be a programmatic error. Tracked as `D-celery-broker-not-verified-deferring-backlog-autoscaling-2026-05-05`. |

**Shipped design:**

| Element | Detail |
|---|---|
| Stack name | `luciel-prod-worker-autoscaling` |
| Template | `cfn/luciel-prod-worker-autoscaling.yaml` |
| ScalableTarget | `service/luciel-cluster/luciel-worker-service` on `ecs:service:DesiredCount` |
| Capacity bounds | `MinCapacity: 1`, `MaxCapacity: 4` |
| Capacity ceiling rationale | db.t3.micro `max_connections ≈ 112`; `luciel-rds-connection-count` alarm fires at 90. 4 workers × small SQLAlchemy pool stays well under that. |
| Scaling policy | `luciel-worker-cpu-target-tracking` (TargetTrackingScaling) |
| Predefined metric | `ECSServiceAverageCPUUtilization` |
| Target value | 60% |
| Cooldowns | ScaleOut 60s (absorb bursts fast), ScaleIn 300s (shrink slowly to avoid flap) |
| `DisableScaleIn` | `false` (allow shrink) |
| Service-linked role | `AWSServiceRoleForApplicationAutoScaling_ECSService` (AWS auto-creates on first registration) |
| AWS-managed alarms | TargetTracking auto-creates two alarms (high CPU, low CPU) prefixed `TargetTracking-`; not ours to manage. Free of charge. |

### 6.0 Pre-flight (mandatory before any deploy)

```powershell
# Operator pulls latest from advisor branch
git fetch origin
git pull origin step-28-hardening-impl
git rev-parse HEAD  # Cross-check against advisor-provided SHA

# CFN template length audit (catches Description >1024 BEFORE push)
awk '/^Description:/{print length($0)-13}' cfn/luciel-prod-worker-autoscaling.yaml
# Expect: under 1024 chars

# Numeric-constraint audit (catches Period/Cooldown/TargetValue out-of-range)
grep -nE '(Period|Cooldown|TargetValue|MinCapacity|MaxCapacity):' cfn/luciel-prod-worker-autoscaling.yaml
```

### 6.1 Deploy (as actually executed)

```powershell
aws cloudformation deploy `
  --template-file cfn/luciel-prod-worker-autoscaling.yaml `
  --stack-name luciel-prod-worker-autoscaling `
  --region ca-central-1 `
  --no-fail-on-empty-changeset
```

No `--capabilities` flag needed: the template uses the AWS service-linked role which is auto-created and requires no explicit IAM acknowledgment.

### 6.2 Verify

```powershell
# (a) Scalable target registered with correct capacity bounds
aws application-autoscaling describe-scalable-targets `
  --service-namespace ecs `
  --resource-ids service/luciel-cluster/luciel-worker-service `
  --region ca-central-1 `
  --query "ScalableTargets[0].[ResourceId,ScalableDimension,MinCapacity,MaxCapacity,RoleARN]" `
  --output table
# Expect: service/luciel-cluster/luciel-worker-service | ecs:service:DesiredCount | 1 | 4 | arn:...AWSServiceRoleForApplicationAutoScaling_ECSService

# (b) Scaling policy attached with correct target value and cooldowns
aws application-autoscaling describe-scaling-policies `
  --service-namespace ecs `
  --resource-id service/luciel-cluster/luciel-worker-service `
  --region ca-central-1 `
  --query "ScalingPolicies[].[PolicyName,PolicyType,TargetTrackingScalingPolicyConfiguration.TargetValue,TargetTrackingScalingPolicyConfiguration.PredefinedMetricSpecification.PredefinedMetricType,TargetTrackingScalingPolicyConfiguration.ScaleOutCooldown,TargetTrackingScalingPolicyConfiguration.ScaleInCooldown]" `
  --output table
# Expect: luciel-worker-cpu-target-tracking | TargetTrackingScaling | 60.0 | ECSServiceAverageCPUUtilization | 60 | 300

# (c) Production undisturbed by registration
aws ecs describe-services --cluster luciel-cluster --services luciel-worker-service `
  --region ca-central-1 `
  --query "services[0].[serviceName,status,desiredCount,runningCount,pendingCount,deployments[0].rolloutState,deployments[0].failedTasks]" `
  --output table
# Expect: luciel-worker-service | ACTIVE | 1 | 1 | 0 | COMPLETED | 0
```

Load testing the policy is OPTIONAL for this commit — production traffic profile is already low and the CPU target plus capacity ceiling are conservative. Follow-up commit (broker verification + backlog policy) should include a k6/hey scenario before close.

### 6.3 Rollback (Commit 6)

```powershell
aws cloudformation delete-stack --stack-name luciel-prod-worker-autoscaling --region ca-central-1
aws cloudformation wait stack-delete-complete --stack-name luciel-prod-worker-autoscaling --region ca-central-1
```

Reverts service to its static `DesiredCount`. AWS-managed alarms are deleted automatically with the policy. No data path impact.

---

## 7 — Commit 7 — Container-level health checks ✅ SHIPPED 2026-05-05

**Status:** ✅ **SHIPPED 2026-05-05 ~16:15 EDT** via worker rev 11 (`fceb7e9`). The original design in this section (`celery inspect ping` CMD-SHELL probe) was attempted as rev 7→10 and **failed in production all four times**. The shipped design in rev 11 is structurally different — a producer-heartbeat / mtime-probe topology that inverts which side is observable. **The original design below is preserved verbatim for audit continuity; the actual shipped design follows.**

**Backend container:** No separate container probe was shipped. Backend health continues to come from ALB target group health (Phase 1 state). This is intentional: with ALB fronting backend traffic, the target group probe is already the authoritative liveness signal, and adding a redundant container probe would produce duplicate alarms without new diagnostic value. Revisit only if ALB ever stops fronting `luciel-backend-service`.

**Worker container shipped design (rev 11):**

| Element | Detail |
|---|---|
| Producer | Daemon thread inside celery process. Hooks `worker_ready` signal in `app/worker/celery_app.py` (+87 lines). Touches `/tmp/celery_alive` every 15s and logs `healthcheck heartbeat: touched /tmp/celery_alive` at INFO. `worker_shutdown` signal stops the thread cleanly via `threading.Event`. Logs go to awslogs — the producer is observable in CloudWatch. |
| Probe | 4-line `os.stat()` on `/tmp/celery_alive` in `scripts/healthcheck_worker.py`. Exit 0 if mtime within 60s window, else 1. Pure stdlib, sub-ms latency, no broker, no celery imports. |
| Topology | Producer logs to awslogs (greppable, alarmable, auditable). Probe is silent (CMD-SHELL stdout is opaque — see `D-healthcheck-cmdshell-output-not-in-awslogs-2026-05-05`). |
| HEALTHCHECK config | `interval: 30, timeout: 15, retries: 3, startPeriod: 60` |
| Heartbeat interval | 15s |
| Heartbeat freshness window | 60s |
| Image (rev 11) | `729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend@sha256:f5ae6997cf2a9f3b75a1488994810f61054c8fbf1299a2e106be8558763f3da0`, tag `worker-rev11` |
| Task-definition | `luciel-worker:11` registered via `worker-td-rev11.json` (`fceb7e9`) |
| Service | `luciel-worker-service` (NOT `luciel-worker` — that's the TD family). See naming-asymmetry callout below. |

### 7.0 Naming-asymmetry callout (added 2026-05-05 from D-ecs-service-name-asymmetry-with-td-family)

The ECS *service name* and the task-definition *family name* are deliberately distinct:

- **Service name** is what `aws ecs update-service` targets: `luciel-worker-service`, `luciel-backend-service`. **Always ends in `-service`.**
- **Task-def family** is what `aws ecs register-task-definition` increments: `luciel-worker`, `luciel-backend`. **Equals the service name minus `-service`.**
- Never abbreviate either at the API call site. Every AWS ECS write-side command in this runbook uses the fully-qualified name.

### 7.1 Four-iteration learning record (rev 7 → rev 8 → rev 9 → rev 10 → rev 11)

| Rev | Probe shape | TD commit | Outcome |
|---|---|---|---|
| 7 | `celery -A app.worker.celery_app inspect ping -d celery@$HOSTNAME` | `worker-td-rev7.json` (`837da98`) | UNHEALTHY — Fargate `$HOSTNAME` did not match celery's `socket.gethostname()` mid-init (`D-celery-fargate-hostname-mismatch-in-healthcheck-2026-05-05`). |
| 8 | `celery -A app.worker.celery_app inspect ping` (no `-d` filter) | `worker-td-rev8.json` (`27723b0`) | Failed silently — probe stdout went to Docker per-container health buffer, NOT awslogs (`D-celery-inspect-ping-unobservable-on-fargate-2026-05-05` and generalized `D-healthcheck-cmdshell-output-not-in-awslogs-2026-05-05`). |
| 9 | `python /app/scripts/healthcheck_worker.py` extracting probe to a script (with `procps` added to image) | `worker-td-rev9.json` (`bb6dd7a`), script in `594821e` | Failed because `pip install -e .` had set entrypoint argv0 to `python` not the script name (`D-pip-entrypoint-argv0-is-python-not-script-name-2026-05-05`). |
| 10 | Same probe, with element-membership match in script (`celery@<hostname>` ∈ responder set) | `worker-td-rev10.json` (`dbdc469`), fix in `d56f08c` | Same observability gap as rev 9. Element-membership semantics correct, but diagnosis pathway structurally broken because CMD-SHELL stdout still opaque. **Decision point: stop iterating on inspect-ping family.** |
| 11 | Producer-heartbeat / mtime-probe topology (the win) | `worker-td-rev11.json` (`fceb7e9`), code in `079f327` | ✅ HEALTHY. 17 heartbeat log lines observed in `/ecs/luciel-worker` at 15s cadence, drift 7ms over 3.5min. Single PRIMARY deployment, `rolloutState: COMPLETED`, `failedTasks: 0`. |

**Lessons (locked in as forward-looking guards):**

1. ECS healthcheck CMD-SHELL stdout is invisible to awslogs. It surfaces only via `aws ecs describe-tasks --include CONTAINER_INSTANCE_HEALTH ...`, retained for ~10 most-recent invocations. **Never rely on probe-side stdout being routable.**
2. If the probe must be silent, attach observability to the *producer* of the liveness signal, not the consumer. The producer logs at INFO via the application's own logger; the probe just reads.
3. Avoid host/topology assumptions in container probes. Fargate's `$HOSTNAME` env var, `socket.gethostname()`, and `/etc/hostname` can diverge. The shipped probe assumes nothing about hostname.
4. `pip install -e .` rewrites entrypoint argv0. Never rely on `sys.argv[0]` for self-identification; use `__file__` or pass identity explicitly.
5. Before any AWS write-side `file://` call referencing local JSON: operator runs `git fetch && git pull origin step-28-hardening-impl` AND a SHA cross-check (`git rev-parse HEAD` matches advisor-provided SHA). Closes `D-operator-pull-skipped-before-write-side-aws-ops-2026-05-05`.

### 7.2 Deploy (as actually executed for rev 11)

```powershell
# 1. Operator pulls latest from advisor branch (mandatory pre-AWS ritual)
git fetch origin
git pull origin step-28-hardening-impl
git rev-parse HEAD  # Cross-check against advisor-provided SHA

# 2. Register the new task-def revision
aws ecs register-task-definition `
  --cli-input-json file://worker-td-rev11.json `
  --region ca-central-1 `
  --query "taskDefinition.revision" --output text
# Expect: 11

# 3. Roll the service (note: service name ends in `-service`, family does not)
aws ecs update-service `
  --cluster luciel-cluster `
  --service luciel-worker-service `
  --task-definition luciel-worker:11 `
  --force-new-deployment `
  --region ca-central-1
```

### 7.3 Verify (as actually executed for rev 11)

```powershell
# Deployment reaches COMPLETED
aws ecs describe-services `
  --cluster luciel-cluster --services luciel-worker-service `
  --region ca-central-1 `
  --query "services[0].deployments[?status=='PRIMARY'].[rolloutState,desiredCount,runningCount,failedTasks]" `
  --output text
# Expect: COMPLETED  1  1  0

# Producer heartbeat is visible in awslogs
aws logs filter-log-events `
  --log-group-name /ecs/luciel-worker `
  --filter-pattern "healthcheck heartbeat" `
  --region ca-central-1 `
  --query "events[*].[timestamp,message]" --output text
# Expect: 17+ events at 15s cadence, drift <100ms over several minutes
```

For rev 11 deploy on 2026-05-05: 17 events from 20:10:55.521 (`initial touch of /tmp/celery_alive`) through 20:14:25.528, exact 15.000s ± 1ms cadence.

### 7.4 Rollback (Commit 7)

```powershell
aws ecs update-service `
  --cluster luciel-cluster --service luciel-worker-service `
  --task-definition luciel-worker:6 `
  --force-new-deployment --region ca-central-1
```

Rev 6 is the last pre-healthcheck revision (post-Commit-4 RDS-auth-GREEN). Rolling back removes the container healthCheck block; behavior reverts to ECS task-state-only (no container-level liveness probe). The producer-heartbeat thread is still in the rev-6 image as of the rev-11 deploy because the code shipped together; the thread continues touching `/tmp/celery_alive` and logging — just nothing reads the file. This is harmless. To fully revert the producer too, redeploy from a tag prior to `079f327`.

---

### 7.5 Original design (preserved verbatim for audit continuity)

**Goal:** ECS replaces a sick task before the ALB target group
notices. Belt-and-suspenders against the existing ALB target health
check.

**Probes:**

| Container | Command | Interval | Timeout | Retries | StartPeriod |
|---|---|---|---|---|---|
| Backend (web) | `curl -fsS http://localhost:8000/health || exit 1` | 30 s | 5 s | 3 | 30 s |
| Worker | `celery -A app.worker.celery_app inspect ping -d celery@$(hostname) \|\| exit 1` | 60 s | 10 s | 3 | 60 s |

**Why this design did not ship:** the worker probe failed in production across rev 7, 8, 9, 10 for the reasons in §7.1. Backend probe was deferred because ALB target group health is the authoritative signal.

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
- [x] Worker connects to RDS as `luciel_worker`, NOT `luciel_admin`
      (Commit 4, shipped 2026-05-05 14:51:27 UTC)
- [x] `luciel_admin` password rotated (Commit 4, RDS rotation chain complete)
- [x] 7 CloudWatch alarms armed, SNS subscription confirmed (Commit 5, stack `luciel-prod-alarms`, shipped 2026-05-05 ~16:49 EDT; heartbeat alarm verified ALARM→OK on real heartbeat datapoints)
- [x] ECS Application Auto Scaling live for worker on CPU TargetTracking 60%, capacity 1–4 (Commit 6, stack `luciel-prod-worker-autoscaling`, shipped 2026-05-05 ~17:35 EDT). Backend autoscaling and worker backlog autoscaling deferred to follow-up commits with explicit drift entries.
- [x] Container healthChecks live for worker (Commit 7, rev 11, shipped 2026-05-05); backend continues on ALB target group health by design
- [x] Prod `python -m app.verification` 19/19 green — closed 2026-05-05 ~20:03 EDT via verify task `7b2a5f213b854db5b694245d0040e974` on `luciel-verify:5` against image digest `sha256:195e30fffc157d4536f84ed96781eb90e9cdc4353d7e1e89cebb4b5602c82d51`. Backend on rev20 / digest `sha256:3b695018a3e01b0059e9a0ff53328dee1640ead150180cd7bb54f93acb0821bc` (commits 9-13). Closure path required commits 9 (P19 audit-log mount), 10 (P10 harness response-parser + UUID JSON), 11 (UUID JSON serialization in agent_repository), 12 (5 new admin scope-assignment + user-deactivate routes for harness-via-HTTP), 13 (P12/P13/P14 harness migration to those routes), 14 (P12 promote-ordering + P14 query-string fixes). Five drifts logged this closure cycle (D-audit-log-router-not-mounted-pre-commit-9, D-uuid-not-json-serializable-via-jsonb-2026-05-05, D-verify-harness-direct-db-writes-against-worker-dsn-2026-05-05, D-verify-task-pure-http-2026-05-05, D-luciel-instance-hard-delete-500-still-observed-2026-05-05); first three RESOLVED in-session, last two DEFERRED to Step 29 with explicit follow-up scope.
- [x] `docs/CANONICAL_RECAP.md` updated to v2.0, tag
      `step-28-phase-2-complete` pushed

Phase 2 is now CLOSED. Canonical recap moves on to Phase 3 (hygiene) or Step 30b (chat widget — REMAX trial unblock), whichever the user prioritizes.

**Deferred items tracked as drifts (see CANONICAL_RECAP.md §15):**
- `D-celery-broker-not-verified-deferring-backlog-autoscaling-2026-05-05` — follow-up commit to verify celery broker (Redis vs SQS) and add backlog-per-worker autoscaling policy
- Backend service autoscaling — not in original Commit 6 scope per ALB-front design
- Synthetic-breach validation of RDS/ECS/SSM alarms in dev RDS — follow-up commit
