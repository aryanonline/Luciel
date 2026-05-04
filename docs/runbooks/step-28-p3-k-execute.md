# P3-K + P3-G — Execute runbook

**Status:** Dry-run / execute phase. Design phase complete in commit `9e48098`.
**Branch:** `step-28-hardening-impl`.
**Prerequisites met:** P3-J resolved (MFA on `luciel-admin`,
`arn:aws:iam::729005488042:mfa/Luciel-MFA`).
**Companion:** `docs/runbooks/step-28-p3-k-mint-operator-role.md` (the design doc).

This runbook lists the exact commands you will run, in order, with
each argument explained and expected output for each. Nothing executes
from the agent side; you copy-paste into your PowerShell session.

---

## 0. Pre-flight: confirm working directory and AWS identity

```powershell
cd C:\Users\aryan\Projects\Business\Luciel
git status
aws sts get-caller-identity
```

**Expected:**
- `git status` shows clean working tree on `step-28-hardening-impl` at commit `9e48098` (or later).
- `get-caller-identity` returns:
  ```json
  {
    "UserId": "...",
    "Account": "729005488042",
    "Arn": "arn:aws:iam::729005488042:user/luciel-admin"
  }
  ```

If `Account` is anything other than `729005488042`, **STOP** —
you're pointed at the wrong AWS account.

---

## Step 1 — Code edit: add `--admin-db-url-stdin` to mint script

**Why first:** the PS1 helper depends on this flag. We add it before
creating the role so the smoke-test step at the end actually works.

This is the one Python edit. The agent will produce the diff in a
follow-up commit. Apply via `git pull` once that commit lands. For
now, this step is the agent's responsibility — see Step 1a below.

### Step 1a (agent-side, already complete by the time you run this)

The agent will:
1. Edit `scripts/mint_worker_db_password_ssm.py` to add a
   mutually-exclusive group around `--admin-db-url` and
   `--admin-db-url-stdin`.
2. Add stdin-reading logic in `main()`.
3. Update the docstring.
4. Run any existing unit tests.
5. Commit + push.

**Operator action:** `git pull origin step-28-hardening-impl`.

---

## Step 2 — P3-G: apply migrate-role policy diff

This adds `ssm:GetParameterHistory` to the existing inline policy on
the migrate task role. One action added; everything else preserved.

### 2.1 Verify the diff target exists and matches what the design doc expects

```powershell
aws iam get-role-policy `
    --role-name luciel-ecs-migrate-role `
    --policy-name luciel-migrate-ssm-write `
    --output json
```

**Expected:** the policy currently has 5 SSM actions
(`GetParameter`, `PutParameter`, `DescribeParameters`,
`AddTagsToResource`, `ListTagsForResource`). If you see 6 actions
already (i.e., `GetParameterHistory` is already there), **STOP** —
state has drifted from the design doc; ping the agent.

### 2.2 Apply the new policy

```powershell
aws iam put-role-policy `
    --role-name luciel-ecs-migrate-role `
    --policy-name luciel-migrate-ssm-write `
    --policy-document file://infra/iam/luciel-migrate-ssm-write-after-p3-g.json
```

**Expected output:** silence on success (this command returns nothing
on success; non-zero exit on failure).

### 2.3 Verify the post-state

```powershell
aws iam get-role-policy `
    --role-name luciel-ecs-migrate-role `
    --policy-name luciel-migrate-ssm-write `
    --query 'PolicyDocument.Statement[?Sid==`ReadWriteSsmParameters`].Action' `
    --output json
```

**Expected:** a JSON array containing `"ssm:GetParameterHistory"`
alongside the original five actions.

---

## Step 3 — P3-K Part 1: create the new role

```powershell
aws iam create-role `
    --role-name luciel-mint-operator-role `
    --assume-role-policy-document file://infra/iam/luciel-mint-operator-role-trust-policy.json `
    --max-session-duration 3600 `
    --description "Option 3 mint-operator role; MFA-required AssumeRole. P3-K (2026-05-03)."
```

**Argument explanations:**
- `--role-name luciel-mint-operator-role` — the canonical name used
  by the PS1 helper and the design doc.
- `--assume-role-policy-document file://...` — the trust policy. The
  `file://` prefix is required by the AWS CLI for file paths.
- `--max-session-duration 3600` — credentials issued by `AssumeRole`
  expire after at most 1 hour. AWS default is 3600s; we set it
  explicitly so a future change can be detected via diff.
- `--description "..."` — searchable note in the IAM console; helps
  future-you remember what this role is for.

**Expected output:** JSON describing the new role, including
`Arn: arn:aws:iam::729005488042:role/luciel-mint-operator-role` and
the trust policy you just supplied.

### 3.1 Verify the role and its trust policy

```powershell
aws iam get-role --role-name luciel-mint-operator-role --output json
```

**Expected:** `Role.AssumeRolePolicyDocument` shows your trust policy
with the `Bool: aws:MultiFactorAuthPresent=true` and `NumericLessThan:
aws:MultiFactorAuthAge=3600` conditions intact, principal locked to
`arn:aws:iam::729005488042:user/luciel-admin`,
`MaxSessionDuration: 3600`.

---

## Step 4 — P3-K Part 2: attach the inline permission policy

```powershell
aws iam put-role-policy `
    --role-name luciel-mint-operator-role `
    --policy-name luciel-mint-operator-permissions `
    --policy-document file://infra/iam/luciel-mint-operator-role-permission-policy.json
```

**Argument explanations:**
- `--policy-name luciel-mint-operator-permissions` — names the inline
  policy on the role. Inline (not managed) is intentional: this role
  has exactly one purpose; a managed policy would imply reuse.

**Expected output:** silence on success.

### 4.1 Verify the permission policy

```powershell
aws iam get-role-policy `
    --role-name luciel-mint-operator-role `
    --policy-name luciel-mint-operator-permissions `
    --output json
```

**Expected:** three statements
(`ReadAdminDsnFromSsm`, `DescribeAdminDsnParameter`,
`DecryptAdminDsnViaSsm`), all with `Effect: Allow`, all scoped as
designed.

### 4.2 List role policies (sanity check — should be exactly one)

```powershell
aws iam list-role-policies --role-name luciel-mint-operator-role
```

**Expected:** `["luciel-mint-operator-permissions"]`. If you see
anything else, **STOP**.

```powershell
aws iam list-attached-role-policies --role-name luciel-mint-operator-role
```

**Expected:** empty `AttachedPolicies` list. We use inline only,
no managed policies attached.

---

## Step 5 — Smoke test the assume-role ceremony (DRY RUN)

This is the moment of truth: does the PS1 helper actually work end-to-end?

We use `--dry-run` so the mint script does **NOT** touch Postgres or SSM.
What we're testing:
- The PS1 helper can prompt for MFA TOTP
- AWS validates the TOTP and issues short-lived credentials
- The helper reads the admin DSN from SSM as the assumed role
- The helper pipes the DSN to the mint script via stdin
- The mint script accepts `--admin-db-url-stdin` and runs through
  argparse + dry-run logic without crashing
- The finally block clears the assumed credentials

**Get a fresh TOTP code from your authenticator app**, then:

```powershell
.\scripts\mint-with-assumed-role.ps1 `
    -WorkerHost "luciel-db.c3oyiegi01hr.ca-central-1.rds.amazonaws.com" `
    -DryRun
```

The script will prompt:
```
Enter current MFA 6-digit code:
```

Type the current TOTP code from your authenticator app and press Enter.

**Expected sequence:**
1. `Calling sts:AssumeRole with MFA...`
2. `AssumeRole OK; credentials valid until <ISO timestamp ~1 hour out>`
3. `Reading admin DSN from SSM as the assumed role...`
4. `Admin DSN read OK (length=<some number> chars; value not echoed)`
5. `Invoking mint_worker_db_password_ssm with assumed credentials...`
6. Mint script's own dry-run output (which prints SSM path, region,
   role name, password SHA256 — NOT the password itself, NOT the DSN)
7. `Mint ceremony complete.`
8. `Assumed credentials cleared from session.`

**If step 1 or 2 fails:** most likely a typo'd or stale TOTP code, or
trust policy not yet propagated (AWS IAM is eventually consistent;
wait 30s and retry).

**If step 3 or 4 fails:** the assumed role doesn't have read access
on `/luciel/database-url`. Verify the permission policy in 4.1.

**If step 5 or 6 fails:** the mint script edit (Step 1) didn't land,
or the stdin path has a bug. Pull latest, retry.

### 5.1 Verify credentials are gone after the script returns

```powershell
$env:AWS_ACCESS_KEY_ID
$env:AWS_SECRET_ACCESS_KEY
$env:AWS_SESSION_TOKEN
```

**Expected:** all three are empty/null. If any has a value, the
finally block didn't fire correctly.

### 5.2 Verify CloudTrail captured the AssumeRole event (optional but encouraged)

```powershell
aws cloudtrail lookup-events `
    --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRole `
    --max-results 5 `
    --query 'Events[].{Time:EventTime,User:Username,Resources:Resources}' `
    --output table
```

**Expected:** an `AssumeRole` event in the last few minutes, attributed
to `luciel-admin`, targeting `luciel-mint-operator-role`. This is the
auditable boundary the design doc §3.5 promised.

---

## Step 6 — Update docs and push

After successful smoke test, the agent will:
1. Mark P3-K + P3-G as ✅ resolved in `docs/PHASE_3_COMPLIANCE_BACKLOG.md`
   with verbatim verification output you paste back.
2. Update `docs/CANONICAL_RECAP.md` §4.1 close-gate clause for the
   mint-operator role with the green check.
3. Add §15 drift register resolution entries.
4. Bump canonical recap version to v1.3.
5. Commit + push as a single docs commit.

After this commit lands, **Commit 4 mint re-run** is unblocked subject
to P3-H (rotate leaked `LucielDB2026Secure` + delete leaking log
stream). P3-H is the last gate before the actual mint re-run.

---

## Rollback procedures

### Rollback P3-G (migrate role)

```powershell
# Pre-diff policy is preserved in:
#   infra/iam/luciel-migrate-ssm-write-add-getparameterhistory.diff.md
# Reconstruct it from the "Current" section, save as a temp file,
# and reapply:
aws iam put-role-policy `
    --role-name luciel-ecs-migrate-role `
    --policy-name luciel-migrate-ssm-write `
    --policy-document file://<temp-pre-diff-policy.json>
```

### Rollback P3-K (mint-operator role)

```powershell
# Delete inline policy first, then the role itself.
aws iam delete-role-policy `
    --role-name luciel-mint-operator-role `
    --policy-name luciel-mint-operator-permissions

aws iam delete-role `
    --role-name luciel-mint-operator-role
```

The role is brand-new with no other identity assuming it and no
resource policies referencing it; deletion has no downstream impact.

---

## What this runbook does NOT cover

- Commit 4 mint re-run (the actual password rotation). That is gated
  on this runbook completing successfully **AND** on P3-H.
- P3-H (rotate leaked `LucielDB2026Secure` + delete leaking log
  stream). Separate runbook to follow.
- Phase 2 Commits 5–7 (alarms, auto-scaling, healthchecks). After
  Commit 4 lands.
