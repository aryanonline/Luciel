# P3-H — Rotate `luciel_admin` password + purge CloudWatch leak

**Status:** Design phase. Operator review required before any execution.
**Branch:** `step-28-hardening-impl`.
**Authoring commit:** (this commit).
**Prerequisites met:**
- P3-J (MFA on `luciel-admin`) ✅ resolved.
- P3-G (migrate role least-privilege SSM write) ✅ resolved.
- P3-K (`luciel-mint-operator-role` exists, smoke-tested) ✅ resolved.

P3-H is the **last gate before Phase 2 Commit 4 (worker DB role swap mint
ceremony).** It rotates the leaked `luciel_admin` Postgres master
password and purges the only known plaintext copy from CloudWatch.

This runbook is the **canonical credential-rotation incident-response
playbook for Luciel.** Future credential leaks (SSM secrets, IAM access
keys, ALB certs, etc.) should follow the same shape: rotate at source,
update consumer of record, verify propagation, purge plaintext copies,
sweep for unknown contamination, capture evidence.

---

## 0. Pre-flight: confirm working state

```powershell
# 0.1 Confirm working tree clean and on the right branch
cd C:\path\to\Luciel-work
git status
git rev-parse --abbrev-ref HEAD   # expect: step-28-hardening-impl
git rev-parse HEAD                # expect: efc08de or later

# 0.2 Confirm AWS identity
aws sts get-caller-identity
# Expect: arn:aws:iam::729005488042:user/luciel-admin

# 0.3 Confirm RDS instance exists and is available
aws rds describe-db-instances `
  --db-instance-identifier luciel-db `
  --query "DBInstances[0].[DBInstanceStatus,Endpoint.Address,MasterUsername,EngineVersion]" `
  --output table
# Expect: available | <endpoint> | luciel_admin | <pg version>
```

If any of the above is unexpected — **stop**. Do not proceed.

---

## 1. Generate new password (operator-side, never leaves operator host)

The new password must:
- Be at least 32 characters.
- Contain only characters that are safe in a Postgres URI userinfo
  segment when **percent-decoded** (avoid `@`, `:`, `/`, `?`, `#`, `%`,
  `&` to keep the connection string trivially encodable).
- Never appear in agent context, in shell history with `Set-PSReadlineOption`
  history-saving on, or in any log.

**Recommended generator (PowerShell):**

```powershell
# 1.1 Generate password into a SecureString variable in this shell only
Add-Type -AssemblyName System.Web
$plainNew = -join ((48..57) + (65..90) + (97..122) + 33,45,46,61,95 |
                   Get-Random -Count 40 |
                   ForEach-Object {[char]$_})
# 40 chars from [0-9A-Za-z!\-.=_] — URI-safe, no shell-special chars.

# 1.2 Sanity-check length and character set (does NOT print the password)
$plainNew.Length                                      # expect: 40
($plainNew -match '^[A-Za-z0-9!\-.=_]+$')              # expect: True

# 1.3 Suppress PowerShell history capture for the next commands
Set-PSReadlineOption -HistorySaveStyle SaveNothing
```

**Why these choices:**
- `(48..57) + (65..90) + (97..122)` = `[0-9A-Za-z]` (62 chars).
- `33,45,46,61,95` = `! - . = _` — five additional URI-safe punctuation
  marks. Total alphabet: 67 characters. 40 chars × log2(67) ≈ 242 bits
  of entropy, well above the 128-bit floor.
- Excluded URI reserved chars (`:` `/` `?` `#` `[` `]` `@` `!` `$` `&`
  `'` `(` `)` `*` `+` `,` `;` `=`) **except** `!` `-` `.` `=` `_` which
  are URI-unreserved per RFC 3986.

**If you prefer a different generator** (e.g. `openssl`, password
manager): the only invariant is that the value lives in `$plainNew`
(SecureString-equivalent) and never echoes to the screen, never lands
in shell history, and never gets pasted into anything except the two
specific commands in §2 and §3.

---

## 2. Rotate the RDS master password

```powershell
# 2.1 Apply the new password to RDS (immediately, no reboot needed for PG)
aws rds modify-db-instance `
  --db-instance-identifier luciel-db `
  --master-user-password $plainNew `
  --apply-immediately

# Expect: JSON with "DBInstanceStatus": "available" or "modifying".
# RDS applies master password changes synchronously for Postgres; no
# downtime, no reboot. Existing connections stay alive but new auths
# from the moment of return must use the new password.
```

**Verification (do this immediately, while you still hold `$plainNew`):**

```powershell
# 2.2 Confirm RDS accepts the new password by pinging from operator host
#     (requires psql + the RDS endpoint reachable; if your IP is not in
#     the security group, skip this and rely on §4 ECS verification.)
$endpoint = aws rds describe-db-instances `
  --db-instance-identifier luciel-db `
  --query "DBInstances[0].Endpoint.Address" --output text

# Set PGPASSWORD only for the duration of the next command, then clear.
$env:PGPASSWORD = $plainNew
psql "host=$endpoint port=5432 user=luciel_admin dbname=postgres sslmode=require" `
  -c "SELECT 1 AS rotation_ok;"
$env:PGPASSWORD = $null
# Expect: rotation_ok | 1
```

**If §2.2 returns an authentication error**, the new password did not
take effect. Re-run §2.1; do NOT proceed to §3 until §2.2 succeeds —
otherwise SSM and RDS will diverge and the backend service will fail
auth as soon as ECS restarts a task.

---

## 3. Update `/luciel/database-url` SSM parameter

The current value in `/luciel/database-url` is the connection string
containing the **old** `LucielDB2026Secure` password. We need to
overwrite it with the **new** password while preserving everything else
(host, port, dbname, sslmode params).

```powershell
# 3.1 Read current value into a variable (via the new MFA-required mint
#     role, since luciel-admin no longer has direct read after P3-K).
#     Use the existing helper for pattern consistency:
.\scripts\mint-with-assumed-role.ps1 -EmitDsnOnly
# This prompts for MFA, assumes luciel-mint-operator-role, prints the
# DSN to a SecureString variable $assumedDsn, clears assumed creds.
#
# (If mint-with-assumed-role.ps1 lacks an -EmitDsnOnly mode, see §3.1b
#  below for the manual sts assume-role + ssm get-parameter sequence.)

# 3.2 Substitute the password segment. The DSN format is
#     postgresql://luciel_admin:<OLD>@<host>:5432/<db>?sslmode=require
#     Replace the segment between ":" and "@" after "luciel_admin".
$oldDsn = $assumedDsn   # SecureString -> plain in this shell only
$newDsn = $oldDsn -replace `
  '(postgresql://luciel_admin:)[^@]+(@)', `
  ('${1}' + $plainNew + '${2}')

# Sanity-check shape WITHOUT printing the password
($newDsn -match '^postgresql://luciel_admin:[^@]+@[^:]+:5432/[^?]+\?sslmode=require$')
# Expect: True

# 3.3 Write back to SSM as SecureString with Overwrite, same KMS key
aws ssm put-parameter `
  --name /luciel/database-url `
  --value $newDsn `
  --type SecureString `
  --key-id alias/aws/ssm `
  --overwrite

# Expect: { "Version": <N+1>, "Tier": "Standard" }
# Capture <N+1> for the evidence record.

# 3.4 Confirm new version is the AdvisedVersion
aws ssm get-parameter-history `
  --name /luciel/database-url `
  --query "Parameters[-1].[Version,LastModifiedDate,DataType]" `
  --output table
# Expect: top row matches the Version returned in 3.3.

# 3.5 Clear sensitive variables
$plainNew = $null
$oldDsn   = $null
$newDsn   = $null
$assumedDsn = $null
[System.GC]::Collect()
```

### 3.1b — Manual fallback if helper lacks `-EmitDsnOnly`

```powershell
$mfaSerial = "arn:aws:iam::729005488042:mfa/Luciel-MFA"
$mfaCode   = Read-Host "Enter MFA code"
$creds = aws sts assume-role `
  --role-arn arn:aws:iam::729005488042:role/luciel-mint-operator-role `
  --role-session-name p3h-rotate-$(Get-Date -Format yyyyMMddHHmmss) `
  --serial-number $mfaSerial --token-code $mfaCode `
  --duration-seconds 900 | ConvertFrom-Json
$env:AWS_ACCESS_KEY_ID     = $creds.Credentials.AccessKeyId
$env:AWS_SECRET_ACCESS_KEY = $creds.Credentials.SecretAccessKey
$env:AWS_SESSION_TOKEN     = $creds.Credentials.SessionToken
try {
  $assumedDsn = aws ssm get-parameter `
    --name /luciel/database-url --with-decryption `
    --query "Parameter.Value" --output text
} finally {
  $env:AWS_ACCESS_KEY_ID = $null
  $env:AWS_SECRET_ACCESS_KEY = $null
  $env:AWS_SESSION_TOKEN = $null
}
```

---

## 4. Verify ECS picks up the new credential (full verification clause)

The backend ECS service reads `/luciel/database-url` at task start via
`secrets[].valueFrom`. Existing running tasks still hold the **old**
DSN in their environment. We need a fresh task to prove the rotation
end-to-end.

**Two acceptable verification paths** — pick one based on appetite:

### 4.A — Cheapest: one-shot run-task with the migrate task definition

```powershell
# 4.A.1 Run the migrate task with a no-op command that just opens a
#       connection to RDS and exits. The task definition already has
#       /luciel/database-url wired into DATABASE_URL.
$cluster = "luciel-prod"   # confirm via: aws ecs list-clusters
$migrateTd = aws ecs describe-task-definition `
  --task-definition luciel-backend-migrate `
  --query "taskDefinition.taskDefinitionArn" --output text

aws ecs run-task `
  --cluster $cluster `
  --task-definition $migrateTd `
  --launch-type FARGATE `
  --network-configuration "awsvpcConfiguration={subnets=[<subnet-id>],securityGroups=[<sg-id>],assignPublicIp=DISABLED}" `
  --overrides '{\"containerOverrides\":[{\"name\":\"migrate\",\"command\":[\"python\",\"-c\",\"import os,psycopg2;c=psycopg2.connect(os.environ[\\\"DATABASE_URL\\\"]);cur=c.cursor();cur.execute(\\\"SELECT 1\\\");print(\\\"P3H_VERIFY_OK\\\",cur.fetchone());c.close()\"]}]}' `
  --query "tasks[0].taskArn" --output text

# 4.A.2 Wait for the task to finish, then read its logs
aws ecs wait tasks-stopped --cluster $cluster --tasks <taskArn>

# 4.A.3 Find the verification log stream and confirm "P3H_VERIFY_OK"
aws logs filter-log-events `
  --log-group-name /ecs/luciel-backend `
  --filter-pattern '"P3H_VERIFY_OK"' `
  --start-time ([DateTimeOffset]::UtcNow.AddMinutes(-10).ToUnixTimeMilliseconds())
# Expect: at least one event matching, value "(1,)".
```

### 4.B — More invasive: force a backend service rolling restart

Skipped here unless 4.A is blocked — the service auto-cycles tasks on
its own schedule, and 4.A already proves the credential works without
risking a deployment-window outage.

**If 4.A returns auth failure:** roll back by reading from
`get-parameter-history --query "Parameters[-2]"` and re-applying the
prior version via §3.3 with the prior value. RDS rotation is
already-done at that point, so the actual recovery is to re-rotate
RDS with a fresh password, not roll back SSM. This is a degenerate
case — call it out as drift `D-p3h-rotation-rolled-back-<date>` and
restart §1.

---

## 5. Sweep CloudWatch for plaintext contamination

The known leak is in stream
`migrate/luciel-backend/d6c927a05eb943b5b343ca1ddef0311c`. We do not
assume it's the only one — every prior migrate run is a candidate.

```powershell
# 5.1 Full filter-sweep across the entire log group, last 90 days
aws logs filter-log-events `
  --log-group-name /ecs/luciel-backend `
  --filter-pattern '"LucielDB2026Secure"' `
  --start-time ([DateTimeOffset]::UtcNow.AddDays(-90).ToUnixTimeMilliseconds()) `
  --query "events[].[logStreamName,timestamp,eventId]" `
  --output table

# Capture the full output to a workspace evidence file:
aws logs filter-log-events `
  --log-group-name /ecs/luciel-backend `
  --filter-pattern '"LucielDB2026Secure"' `
  --start-time ([DateTimeOffset]::UtcNow.AddDays(-90).ToUnixTimeMilliseconds()) `
  --output json > docs\evidence\2026-05-03-p3h-leak-sweep.json
```

**Decision rule from sweep results:**

- **0 hits across the group**: the password was never logged, or the
  one known stream was already deleted (unlikely — operator confirmed
  it's still there). If 0 hits, recheck the filter pattern is exact
  including quotes; if confirmed 0, P3-H §6 still proceeds defensively
  for the known stream by name.
- **N hits across M streams**: collect the unique `logStreamName`
  values into a list — those are all the contaminated streams.
- **Hits in any other log group** (`/ecs/luciel-worker`, etc.):
  out-of-scope for P3-H but **must be filed as new drift entries
  immediately** before continuing.

```powershell
# 5.2 Sweep adjacent log groups defensively
foreach ($lg in @("/ecs/luciel-worker","/aws/rds/instance/luciel-db/postgresql")) {
  Write-Host "--- $lg ---"
  aws logs filter-log-events `
    --log-group-name $lg `
    --filter-pattern '"LucielDB2026Secure"' `
    --start-time ([DateTimeOffset]::UtcNow.AddDays(-90).ToUnixTimeMilliseconds()) `
    --query "events | length(@)" --output text
}
# Expect: 0 for both. Any non-zero -> file drift, do NOT proceed.
```

---

## 6. Delete every contaminated log stream

```powershell
# 6.1 For each stream from §5.1 results, delete it
$streams = @(
  "migrate/luciel-backend/d6c927a05eb943b5b343ca1ddef0311c"
  # Append any additional streams from §5.1 here
)

foreach ($s in $streams) {
  Write-Host "Deleting stream: $s"
  aws logs delete-log-stream `
    --log-group-name /ecs/luciel-backend `
    --log-stream-name $s
}

# 6.2 Verify deletion
foreach ($s in $streams) {
  aws logs describe-log-streams `
    --log-group-name /ecs/luciel-backend `
    --log-stream-name-prefix $s `
    --query "logStreams | length(@)" --output text
  # Expect: 0 for each (stream gone).
}
```

---

## 7. Final residual-leak verification

```powershell
# 7.1 Re-run the sweep — must return zero hits
aws logs filter-log-events `
  --log-group-name /ecs/luciel-backend `
  --filter-pattern '"LucielDB2026Secure"' `
  --start-time ([DateTimeOffset]::UtcNow.AddDays(-90).ToUnixTimeMilliseconds()) `
  --query "events | length(@)" --output text
# Expect: 0

# 7.2 Capture the final clean-state evidence
aws logs filter-log-events `
  --log-group-name /ecs/luciel-backend `
  --filter-pattern '"LucielDB2026Secure"' `
  --start-time ([DateTimeOffset]::UtcNow.AddDays(-90).ToUnixTimeMilliseconds()) `
  --output json > docs\evidence\2026-05-03-p3h-leak-sweep-post-purge.json
```

If 7.1 returns anything other than `0`, **P3-H is NOT resolved** —
investigate the residual stream, add to §6 deletion list, re-run.

---

## 8. Evidence capture + docs sync

When 7.1 returns 0:

1. Save the §3.3 SSM `Version` number, the §4.A.3 verify timestamp,
   and the §5.1 + §7.1 sweep outputs into the evidence files already
   created above.
2. Edit `docs/PHASE_3_COMPLIANCE_BACKLOG.md` P3-H section: prepend
   `✅ **RESOLVED** <UTC timestamp>` header; preserve original entry
   below for audit trail.
3. Edit `docs/CANONICAL_RECAP.md`:
   - Bump version `v1.3 → v1.4`.
   - §4.1 close gate: add fourth ✅ row for P3-H with verification
     command (`filter-log-events ... --query "events | length(@)"` → 0).
   - §15 drift register: mark
     `D-prod-superuser-password-leaked-to-terminal-2026-05-03` as
     **RESOLVED**, cite this runbook + evidence files.
   - §3.1b: append Commit row for the P3-H execution.
4. Single commit: `docs(28-p3-h): mark P3-H resolved — admin password
   rotated, CloudWatch purged; canonical recap v1.3 -> v1.4`.
5. Push.
6. Mark TODO item 10 complete; promote item 11 (Commit 4) to
   in_progress.

---

## 9. Rollback considerations

| Failure point | Recovery |
|---|---|
| §2.1 (RDS modify) returns error | RDS still on old password. No state change. Investigate, retry. |
| §2.2 verify fails after §2.1 success | Re-run §2.1 with same `$plainNew`; if still failing, escalate (network, IAM, RDS health). |
| §3.3 (SSM put) fails after §2 success | **Critical:** RDS has new pw, SSM has old. Backend will auth-fail on next task restart. Either re-run §3.3 immediately, or roll RDS back to a freshly-generated password and update SSM with that. Do NOT walk away. |
| §4.A verify fails | SSM and RDS may disagree. Read SSM, decode password segment, compare with `$plainNew` hash. Re-run §3.3 if mismatch; rotate RDS again if match. |
| §5.1 sweep finds hits in other log groups | Stop. File drift entries. Do not proceed to §6 until scope is fully understood and incorporated. |
| §6 delete fails | Permission issue (`logs:DeleteLogStream` not on `luciel-admin`). Add policy, retry. |
| §7.1 returns non-zero after §6 | New events arrived during the rotation window (unlikely — the leak vector was the v1 mint script which is no longer used). Identify stream, add to §6 list, re-run. |

---

## 10. Notes for future credential-rotation runbooks

This shape — **rotate-at-source → update-canonical-store →
verify-via-fresh-consumer → sweep-for-contamination → purge → final
verify → evidence + docs** — is the template. Future leaks should
duplicate this runbook structure, swapping out the source (RDS master
pw → IAM access key → ALB cert → etc.) and the canonical store (SSM →
Secrets Manager → ACM → etc.).

The drift entry for any future leak should reference *this* runbook
as the IR template.

---

**End of P3-H runbook (design phase).**
