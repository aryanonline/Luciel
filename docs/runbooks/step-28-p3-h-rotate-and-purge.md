# P3-H — Rotate `luciel_admin` password + purge CloudWatch leak

**Status:** ✅ EXECUTED end-to-end 2026-05-03 23:18–23:56 UTC.
P3-H is RESOLVED. See `docs/recaps/2026-05-03-mint-incident.md` §11
for the resolution recap and `docs/PHASE_3_COMPLIANCE_BACKLOG.md` P3-H
for the audit metadata. Three POST-EXECUTION CORRECTION notes are
inlined at §3, §4, and §5/§7 below.
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

> **⚠️ POST-EXECUTION CORRECTION (2026-05-03):** The runbook design
> assumed the stored DSN carried `?sslmode=require`. The real value
> (read live during execution) was
> `postgresql+psycopg://luciel_admin:<pw>@host:5432/luciel` — SQLAlchemy
> scheme prefix, no query string. The shape-check regex on line 156
> failed on the first pass; the safety guard correctly aborted before
> any SSM write. **Working regex (used in the actual execution):**
>
> ```
> $oldDsn -match '^postgresql\+psycopg://luciel_admin:[^@]+@[^:]+:5432/[^?]+$'
> ```
>
> **Working substitution:**
>
> ```
> $newDsn = $oldDsn -replace '(postgresql\+psycopg://luciel_admin:)[^@]+(@)', ('${1}' + $plainNew + '${2}')
> ```
>
> Future credential-rotation runbooks should derive the regex from the
> live DSN shape via a non-destructive read (§3.2 of original) before
> committing to a substitution pattern. Backlog item also captured for
> a TLS posture decision: enforce `?sslmode=require` either via DSN or
> SQLAlchemy `connect_args={"sslmode": "require"}`.

```powershell
# 3.1 Read current DSN as a SecureString via the MFA-gated helper.
#     This prompts for MFA, assumes luciel-mint-operator-role, reads
#     /luciel/database-url, returns the DSN as a SecureString, and
#     clears the assumed credentials in its own finally block.
$assumedSecure = .\scripts\mint-with-assumed-role.ps1 -EmitDsnOnly
# Expect: SecureString. Type-check without printing the value:
$assumedSecure.GetType().FullName  # expect: System.Security.SecureString
$assumedSecure.Length              # expect: same as DSN char count, ~140-160

# 3.2 Convert SecureString -> plain string in a single tightly-scoped
#     block, do the password substitution, and IMMEDIATELY clear the
#     plaintext intermediates.
$oldDsn = [System.Net.NetworkCredential]::new('', $assumedSecure).Password

# Sanity-check the OLD DSN shape WITHOUT printing it
($oldDsn -match '^postgresql://luciel_admin:[^@]+@[^:]+:5432/[^?]+\?sslmode=require$')
# Expect: True. If False, the DSN format has drifted and the regex in
# 3.3 will not match. Stop and re-derive the regex against the actual
# shape (do this WITHOUT echoing the value to console).

# 3.3 Build the new DSN by substituting only the password segment
#     (between 'luciel_admin:' and '@'). $plainNew comes from §1.
$newDsn = $oldDsn -replace `
  '(postgresql://luciel_admin:)[^@]+(@)', `
  ('${1}' + $plainNew + '${2}')

# Sanity-check the NEW DSN shape
($newDsn -match '^postgresql://luciel_admin:[^@]+@[^:]+:5432/[^?]+\?sslmode=require$')
# Expect: True

# 3.4 Write back to SSM as SecureString with Overwrite, same KMS key
aws ssm put-parameter `
  --name /luciel/database-url `
  --value $newDsn `
  --type SecureString `
  --key-id alias/aws/ssm `
  --overwrite

# Expect: { "Version": <N+1>, "Tier": "Standard" }
# Capture <N+1> for the evidence record.

# 3.5 Confirm new version is the AdvisedVersion
aws ssm get-parameter-history `
  --name /luciel/database-url `
  --query "Parameters[-1].[Version,LastModifiedDate,DataType]" `
  --output table
# Expect: top row matches the Version returned in 3.4.

# 3.6 Clear sensitive variables — do this even if anything above failed.
#     SecureString.Dispose() actively wipes the protected memory.
$plainNew = $null
$oldDsn   = $null
$newDsn   = $null
if ($assumedSecure) { $assumedSecure.Dispose(); $assumedSecure = $null }
[System.GC]::Collect()
```

---

## 4. Verify ECS picks up the new credential (full verification clause)

The backend ECS service reads `/luciel/database-url` at task start via
`secrets[].valueFrom`. Existing running tasks still hold the **old**
DSN in their environment. We need a fresh task to prove the rotation
end-to-end.

> **⚠️ POST-EXECUTION CORRECTIONS (2026-05-03):**
>
> **(a) Use SQLAlchemy, not psycopg2, in the verification probe.** The
> design used `import os,psycopg2;c=psycopg2.connect(os.environ[...])`
> but the application consumes the DSN via SQLAlchemy. The actual probe
> used:
>
> ```python
> from sqlalchemy import create_engine, text
> eng = create_engine(url, connect_args={"connect_timeout": 10})
> with eng.connect() as conn:
>     row = conn.execute(text("SELECT 1, current_user, current_database()")).fetchone()
> ```
>
> This exercises the SQLAlchemy URL parser — the same library and shape
> the application uses — so any DSN-shape drift would surface here
> rather than at first real backend task start.
>
> **(b) Probe must emit only safe markers, never `str(e)` / `repr(e)`.**
>
> ```python
> print("P3H_VERIFY_START", flush=True)
> print("P3H_VERIFY_OK select=" + str(row[0]) + " user=" + str(row[1]) + " db=" + str(row[2]), flush=True)
> print("P3H_VERIFY_FAIL " + type(e).__name__, flush=True)  # class name only
> ```
>
> This is the contract that prevents the verification step from
> reproducing the original leak. Verified: §5 sweep did NOT find the
> new §4 stream.
>
> **(c) BOM-free overrides JSON file.** PowerShell's
> `Set-Content -Encoding utf8` writes a UTF-8 BOM (`EF BB BF`) which
> `aws ecs run-task --overrides file://` rejects with cryptic JSON parse
> errors. **Working pattern (PowerShell 5.x and 7.x):**
>
> ```powershell
> $utf8NoBom = New-Object System.Text.UTF8Encoding $false
> [System.IO.File]::WriteAllText($overridesPath, $overridesJson, $utf8NoBom)
> # Verify: bytes[0] should be 123 ('{'), NOT 239,187,191 (EF BB BF BOM)
> $firstBytes = [System.IO.File]::ReadAllBytes($overridesPath)[0..2]
> if ($firstBytes[0] -ne 123) { throw "BOM still present, abort" }
> ```
>
> **(d) Inline `--overrides` JSON via -c is fragile under PowerShell.**
> Multi-line Python with embedded quotes gets mangled by PowerShell
> argument tokenization when passed via `'{...}'` inline. Always write
> the overrides JSON to a temp file (with the BOM-free pattern above)
> and pass `--overrides "file://$path"`.

**Two acceptable verification paths** — pick one based on appetite:

### 4.A — Cheapest: one-shot run-task with the migrate task definition

```powershell
# 4.A.1 Run the migrate task with a no-op command that just opens a
#       connection to RDS and exits. The task definition already has
#       /luciel/database-url wired into DATABASE_URL.
$cluster = "luciel-cluster"   # confirm via: aws ecs list-clusters
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

> **⚠️ POST-EXECUTION CORRECTIONS (2026-05-03) — applies to §5 and §7:**
>
> **(a) Disable AWS CLI pager.** Default `aws` behavior pipes JSON
> through a pager when output exceeds terminal height; this hangs
> PowerShell pipelines silently for minutes. Set
> `$env:AWS_PAGER = ""` at the start of any sweep block, or pass
> `--no-cli-pager` to every `aws` invocation.
>
> **(b) Bound the time window.** `filter-log-events` over 90 days against
> a busy log group (`/ecs/luciel-backend`) can take many minutes
> server-side even with a filter pattern — the filter narrows what is
> *returned*, not what is *scanned*. The leak happened today; 7 days is
> generous. Use
> `[int64]((Get-Date).AddDays(-7) - (Get-Date "1970-01-01Z").ToUniversalTime()).TotalMilliseconds`
> for `--start-time`.
>
> **(c) Use `--log-stream-name-prefix` for targeted passes.** A
> three-pass sweep (targeted `migrate/*` → defensive all-streams →
> defensive other log groups) bounds latency on the dominant pass and
> still catches unknown contamination defensively. The actual execution
> used: pass 1 `--log-stream-name-prefix migrate/` (fast, 1 page);
> pass 2 all streams (defensive, same group); pass 3 all streams in
> `/ecs/luciel-worker` (defensive, different group).
>
> **(d) Wrap each page in a 90 s job timeout.** Use `Start-Job` +
> `Wait-Job -Timeout 90` to bound any single CLI call so a slow
> server-side scan aborts cleanly with a `TIMEOUT` message instead of
> a silent hang.
>
> See the actual executed sweep block in chat history (or in the
> retroactive runbook update commit) for the full PowerShell pattern.

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
