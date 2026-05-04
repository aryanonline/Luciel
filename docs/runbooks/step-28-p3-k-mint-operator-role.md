# P3-K + P3-G: Option 3 mint-operator boundary — design + execution runbook

**Status:** Design phase complete (artifacts drafted, no AWS calls yet).
**Branch:** `step-28-hardening-impl`.
**Prerequisite:** P3-J resolved (MFA on `luciel-admin`,
`arn:aws:iam::729005488042:mfa/Luciel-MFA`, enabled 2026-05-03 23:48:11
UTC).
**Cross-references:**
- `docs/recaps/2026-05-03-mint-incident.md` — incident that drove this design
- `docs/PHASE_3_COMPLIANCE_BACKLOG.md` P3-K + P3-G — backlog entries
- `docs/CANONICAL_RECAP.md` Section 12 anchor 7 — locked architectural decision

---

## 1. What this commit does

**P3-K (P1):** creates a brand-new IAM role
`luciel-mint-operator-role` whose only purpose is to read the admin
DSN from SSM in service of human-operator-initiated credential
ceremonies (mint, rotate). Trust policy is locked to `luciel-admin`
and requires MFA. Permission policy is scoped to `ssm:GetParameter`
on `/luciel/database-url` plus KMS decrypt via SSM. Max session
duration: 1 hour.

**P3-G (P2):** adds a single missing action (`ssm:GetParameterHistory`)
to the existing inline policy `luciel-migrate-ssm-write` on
`luciel-ecs-migrate-role`. Bundled with P3-K because both are
IAM-side and we want one IAM-changes commit.

**Mint-script change:** adds a new `--admin-db-url-stdin` flag to
`scripts/mint_worker_db_password_ssm.py` (mutually exclusive with
`--admin-db-url`) so the PowerShell helper can pipe the DSN via
stdin instead of passing it on the command line. This is the small
code change that completes the Option 3 boundary — without it, the
admin DSN would still land in process args.

---

## 2. The four artifacts in this commit

| File | Purpose |
|---|---|
| `infra/iam/luciel-mint-operator-role-trust-policy.json` | Trust policy: only `luciel-admin` can assume, MFA required, MFA age ≤ 3600 s. |
| `infra/iam/luciel-mint-operator-role-permission-policy.json` | Permission policy: `ssm:GetParameter` on `/luciel/database-url` only, plus `kms:Decrypt` scoped to SSM. |
| `infra/iam/luciel-migrate-ssm-write-add-getparameterhistory.diff.md` | Diff doc explaining the one-line P3-G change. |
| `infra/iam/luciel-migrate-ssm-write-after-p3-g.json` | The post-diff version of the migrate role's inline policy, ready to apply. |
| `scripts/mint-with-assumed-role.ps1` | Operator helper that runs the assume-role-with-MFA ceremony and pipes admin DSN to mint script via stdin. |
| `scripts/mint_worker_db_password_ssm.py` | (Modified) New `--admin-db-url-stdin` flag added; existing `--admin-db-url` preserved for backward compat with local dev. |

---

## 3. Why these specific design choices

### 3.1 Trust policy: `luciel-admin` user, not "any IAM user with MFA"

A more permissive trust policy (e.g., "any IAM user in the account
who has MFA enabled") would be functionally equivalent today —
because `luciel-admin` is the only IAM user. But it would silently
expand the attack surface the moment a second IAM user is created.
Locking the principal to a specific user ARN means "if you want
another user to have mint power, you have to deliberately edit the
trust policy" — which is exactly the audit boundary we want.

### 3.2 MFA age ≤ 3600 s, not the AWS default

`aws:MultiFactorAuthPresent` alone passes if the operator MFA'd at
console login 8 hours ago. Adding `aws:MultiFactorAuthAge < 3600`
forces a fresh MFA challenge for each mint ceremony. This is the
control that turns "MFA on the user" into "MFA on the operation."

### 3.3 Max session duration: 1 hour

The role's `MaxSessionDuration` is set to 3600 s. Combined with the
MFA-age condition, this means the assumed credentials are short-lived
both in absolute time and relative to the MFA event. If they leak,
they expire fast.

### 3.4 Permission policy: `ssm:GetParameter` only — no `PutParameter`

The mint-operator role can READ the admin DSN. It cannot WRITE or
ROTATE it. Rotation of the admin DSN itself is a separate operation
(P3-H) that the operator does directly as `luciel-admin`, not through
this role. This is the smallest blast-radius role we can make and
still serve its purpose.

### 3.5 Why migrate task role does NOT get this permission

The migrate task role runs on ECS, not in a human session. Giving it
`ssm:GetParameter` on `/luciel/database-url` would re-create the
exact failure mode that produced the original leak (a log line in an
ECS task can land in CloudWatch). The Option 3 architecture
deliberately routes the admin DSN read through a path that:

- Requires a human in front of a terminal with a phone
- Produces a CloudTrail `AssumeRole` event tagged with the human's
  principal and the MFA condition value
- Holds the credential in a process memory that exits within seconds

The migrate role can do its actual job (run Alembic migrations)
without ever touching the admin DSN. Any future code that wants
the admin DSN in an automated context should be pushed back to a
human ceremony, not granted to a task role.

### 3.6 Why pipe via stdin, not via CLI argument

A CLI argument like `--admin-db-url "postgres://..."` is visible
in `ps` / `Get-Process` output to anyone with system access while
the script is running. Stdin is process-private. The hardened mint
script (`2b5ff32`) already strips DSNs from error bodies; the stdin
path closes the last leak vector (process args).

---

## 4. Mint-script change (the only Python edit)

The current script (after `2b5ff32`) has:

```python
p.add_argument("--admin-db-url", required=True, help="...")
```

The change replaces this with a mutually-exclusive group:

```python
g = p.add_mutually_exclusive_group(required=True)
g.add_argument("--admin-db-url", help="...")
g.add_argument(
    "--admin-db-url-stdin",
    action="store_true",
    help=(
        "Read the admin DB URL from stdin instead of accepting it "
        "as a CLI argument. Use this in production via the "
        "mint-with-assumed-role.ps1 helper to avoid landing the "
        "DSN in process args."
    ),
)
```

Plus a small block at the top of `main()` that reads stdin if the
flag is set, with whitespace stripping, length sanity checks, and
the same DSN-shape validation already applied to the CLI form.

---

## 5. Apply order (when we move to execute phase)

1. **(Code)** Edit `scripts/mint_worker_db_password_ssm.py` to add
   `--admin-db-url-stdin`. Run existing tests; commit code change
   in same commit as IAM artifacts (one commit, code + infra docs).
2. **(IAM, P3-G first)** Apply the migrate-role policy diff:
   `aws iam put-role-policy --role-name luciel-ecs-migrate-role
   --policy-name luciel-migrate-ssm-write --policy-document
   file://infra/iam/luciel-migrate-ssm-write-after-p3-g.json`
3. **(IAM, P3-K)** Create the new role:
   `aws iam create-role --role-name luciel-mint-operator-role
   --assume-role-policy-document
   file://infra/iam/luciel-mint-operator-role-trust-policy.json
   --max-session-duration 3600
   --description "Option 3 mint-operator role; MFA-required AssumeRole."`
4. **(IAM, P3-K)** Attach inline permission policy:
   `aws iam put-role-policy --role-name luciel-mint-operator-role
   --policy-name luciel-mint-operator-permissions --policy-document
   file://infra/iam/luciel-mint-operator-role-permission-policy.json`
5. **(Verification, read-only)** Three `aws iam get-role` /
   `get-role-policy` calls to confirm the live state matches the JSON.
6. **(Smoke test)** Run `mint-with-assumed-role.ps1 -DryRun` end-to-end.
   Should successfully assume the role, read the admin DSN, hand it to
   the mint script, and have the mint script exit cleanly without
   touching Postgres or SSM.

The actual Commit 4 mint re-run happens **after** all of the above
and after P3-H (leaked-password rotation). Steps 5–6 only validate
that the mechanism works, not that we use it for the real mint yet.

---

## 6. Rollback plan

- **P3-G rollback:** reapply pre-diff policy via `aws iam put-role-policy`.
  Pre-diff policy is preserved in the diff doc.
- **P3-K rollback:** `aws iam delete-role-policy` then `aws iam delete-role`.
  Role is brand-new; deletion has no downstream impact because no other
  identity assumes it and no resource policy references it.
- **Mint-script rollback:** `git revert` the code commit. Existing
  `--admin-db-url` flag is preserved in the change, so backward
  compatibility for local dev is not affected.

---

## 7. What this commit does NOT do

- Does NOT execute Commit 4 (the actual mint re-run). That is gated
  on P3-H (leaked-password rotation + log-stream delete) AND on a
  successful dry-run of this ceremony.
- Does NOT rotate the leaked `LucielDB2026Secure` password. That is P3-H.
- Does NOT touch any ECS task definition, service, or running
  container. The migrate task role's policy is updated, but no task
  is restarted.
- Does NOT create a CloudTrail trail (assumed already on; verify in
  smoke step if uncertain).
