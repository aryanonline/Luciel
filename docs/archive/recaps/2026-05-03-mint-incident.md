# Step 28 Phase 2 / Commit 4 — Mint Script DSN Leak Incident

**Date:** 2026-05-03 (Sunday evening EDT)
**Branch:** `step-28-hardening-impl`
**Severity classification:** S2 (credential leak in self-controlled audit
trail; no third-party exposure; caught before any state mutation)
**Status:** Contained — patches committed; deliberate follow-ups scheduled
for next session
**Author:** Aryan Singh, VantageMind AI
**Document revisions:**
- 2026-05-03 (initial) — original write-up
- 2026-05-03 (evening) — §5 and §8 Follow-up A corrected
  inline after reading the actual `luciel-ecs-migrate-role` IAM policy.
  The original claim that the role lacked `ssm:GetParameter` /
  `ssm:PutParameter` was wrong; the role has both. The genuine gap is
  `ssm:GetParameterHistory` only. Strikethroughs preserve the original
  diagnosis as written so the reasoning trail stays auditable. See also
  the rescoped P3-G entry in `docs/PHASE_3_COMPLIANCE_BACKLOG.md`.
- 2026-05-03 (late-evening, this revision) — §11 P3-H Resolution appended
  after rotation + purge executed end-to-end via
  `docs/runbooks/step-28-p3-h-rotate-and-purge.md` §1–§7. The leaked
  `LucielDB2026Secure` is no longer accepted by RDS; the contaminated
  CloudWatch stream is deleted; final residual sweep returned 0 hits.
  Follow-up B is now closed; Follow-up A is closed via P3-G + P3-K.

---

## 1. TL;DR

While executing Commit 4 of Step 28 Phase 2 (mint the `luciel_worker`
Postgres password and store it in SSM SecureString), the first real-run
attempt against prod RDS surfaced two latent defects in
`scripts/mint_worker_db_password_ssm.py`:

1. The script accepted a SQLAlchemy-form DSN (`postgresql+psycopg://...`)
   without normalization, so raw `psycopg.connect()` rejected it with a
   `ProgrammingError`.
2. The exception handler echoed the offending DSN — including the admin
   password — to stderr, which the ECS task's `awslogs` driver then
   shipped to CloudWatch.

**Atomic ordering held.** No DB role was altered, no SSM parameter was
written, and the `luciel_worker` role still has `NULL` password (the
expected pre-mint state). The only durable artifact is the leaked DSN
string in CloudWatch — practically self-contained because only the
solo-founder IAM user (`luciel-admin`) can read CloudWatch in this
account.

Three patches landed in the same session (commit `2b5ff32`):

- DSN-redacting regex applied to every exception printed to stderr
- SQLAlchemy `+driver` prefix stripped before `psycopg.connect()`
- SSM-writability pre-flight runs **before** any DB mutation

The Commit 4 retry is **deliberately deferred** until the
`luciel-ecs-migrate-role` IAM gap is fixed (next session).

---

## 2. Timeline (UTC)

| Time (approx) | Event |
|---|---|
| 19:00 | Block A/B/C recon complete: `luciel_worker` role exists, NULL password, correct grants, 0 sessions |
| 19:30 | Block C.4 recon (Python harness over psycopg) confirmed role state matches migration `f392a842f885` exactly |
| 20:10 | 11-point script review passed (atomicity gap noted and accepted; password literal in `ALTER ROLE` accepted on URL-safe alphabet defense) |
| 20:25 | Dry-run executed — clean exit, fingerprint `f7a2df475d27` |
| 20:35 | Real-run executed against prod RDS as `luciel-migrate:12` ECS task |
| 20:35 | `psycopg.ProgrammingError` raised: invalid DSN due to `+psycopg` driver suffix; full DSN echoed to stderr → CloudWatch |
| 20:40 | Diagnosed: migrate role lacks `ssm:GetParameter`, so SSM put would have failed atomically anyway |
| 20:55 | Three patches authored and applied to working tree |
| 21:15 | Patches verified (`py_compile` OK, `--help` OK, dry-run with SQLAlchemy URL OK, helper unit tests all pass) |
| 21:18 | Commit `2b5ff32` lands on `step-28-hardening-impl` |

---

## 3. Pre-conditions and what was actually executed

The mint script was invoked from the ECS one-shot `luciel-migrate:12`
task definition (container `luciel-backend`, log group
`/ecs/luciel-backend`, stream prefix `migrate`). The admin DB URL was
sourced from `/luciel/database-url` — the existing SSM parameter the
running backend already uses — which is stored in SQLAlchemy form:

```
postgresql+psycopg://luciel_admin:LucielDB2026Secure@luciel-db.c3oyiegi01hr.ca-central-1.rds.amazonaws.com:5432/luciel?sslmode=require
```

The arguments passed:

```
--admin-db-url   $(aws ssm get-parameter --name /luciel/database-url --with-decryption ...)
--worker-host    luciel-db.c3oyiegi01hr.ca-central-1.rds.amazonaws.com
--worker-port    5432
--worker-db-name luciel
--ssm-path       /luciel/production/worker_database_url
--region         ca-central-1
```

The script reached `psycopg.connect(args.admin_db_url)` and crashed
because libpq does not recognize the `+psycopg` driver suffix (that's
SQLAlchemy syntax, not a libpq scheme). The `ProgrammingError` message
included the full DSN string verbatim. The script's `except Exception
as exc: print(f"... {exc}", file=sys.stderr)` then wrote that string —
password and all — to the task's stderr stream, which the awslogs
log driver shipped to CloudWatch.

---

## 4. Blast radius

**What leaked:** The plaintext `LucielDB2026Secure` admin password,
embedded inside a `postgresql+psycopg://...` DSN, in one CloudWatch log
event in log group `/ecs/luciel-backend`, stream
`migrate/luciel-backend/d6c927a05eb943b5b343ca1ddef0311c`.

**What did NOT leak:**

- No git artifact — the leak path was stderr → CloudWatch, never the
  filesystem.
- No shell history — the DSN was passed via `--admin-db-url` from an
  in-script `aws ssm get-parameter` substitution, never typed
  literally.
- No third-party surface — CloudWatch is in the same AWS account
  (`729005488042`) as the rest of Luciel infrastructure.
- No secondary-system propagation — the leak was caught immediately;
  no downstream pipeline ingested the log event.

**Who can read it (effective audience of one):**

- AWS account `729005488042` is single-tenant; the only IAM principal
  with CloudWatch read access is `luciel-admin` (the solo founder's IAM
  user).
- Federation, third-party log forwarding, and cross-account roles are
  all absent.
- A successful breach of the `luciel-admin` IAM credentials would
  already grant root-equivalent access to RDS via console-driven
  password reset, so the leaked DSN does not expand the breach surface
  meaningfully.

**What is NOT contained until the follow-up:**

- The CloudWatch log event itself still exists. Until the admin
  password is rotated AND the log stream is deleted, an attacker who
  later compromises the `luciel-admin` IAM user (or AWS support staff
  with break-glass access during a future incident) could read the
  password.

---

## 5. Why atomic ordering held ~~(and why we got lucky twice)~~

The script's intended ordering is:

1. Verify role state in Postgres
2. `ALTER ROLE` with new password
3. `put_parameter` to SSM SecureString

The crash happened at step 0 (the connect itself), so steps 1–3 never
ran. The `luciel_worker` role is still in its post-migration NULL-pw
state.

> **Correction (2026-05-03 evening):** The original §5 went on to claim
> a "second, independently-protective fact" — that the
> `luciel-ecs-migrate-role` IAM role lacked `ssm:GetParameter` and
> `ssm:PutParameter` on `/luciel/production/worker_database_url`,
> providing belt-and-suspenders protection in case the connect had
> succeeded. **That claim is wrong.** A direct read of the role's inline
> policy `luciel-migrate-ssm-write` shows it has both `ssm:GetParameter`
> and `ssm:PutParameter` on `/luciel/production/*` (which matches the
> worker DSN parameter), plus `kms:Decrypt` scoped to SSM. The genuine
> missing action is `ssm:GetParameterHistory`, used only by my new
> pre-flight code. The original diagnosis came from a separate SSM
> recon attempt that hit a different permission surface, which I
> incorrectly conflated into this section. The corrected story:
> **atomic ordering held because the crash was at `psycopg.connect()`,
> full stop. There was no second IAM gap to rely on.**

~~But there is a second, independently-protective fact: the
`luciel-ecs-migrate-role` IAM role lacks `ssm:GetParameter` and
`ssm:PutParameter` on `/luciel/production/worker_database_url`. So
even if the connect had succeeded, step 3 would have raised
`AccessDeniedException`, leaving the worker role with a fresh password
and SSM with no value — the worker would not be able to authenticate
on the next ECS task restart, and recovery would have required
`--force-rotate` to re-mint after fixing the IAM gap.~~

~~That second failure mode is exactly the atomicity gap the new
`preflight_ssm_writable()` helper now closes (see §6, patch 3).~~

**Revised:** `preflight_ssm_writable()` is still sound design — it
closes a real atomicity gap that would surface on any future IAM
regression (e.g., a tightened policy that revoked `PutParameter` on the
worker DSN). Today the gap is closed by the existing policy; the
pre-flight is insurance against drift. The justification I originally
gave ("protects against the gap that exists today") was wrong; the
justification "protects against the gap that could exist tomorrow" is
correct.

---

## 6. The three patches (commit `2b5ff32`)

### Patch 1 — Exception-message DSN redaction

`_redact_dsn_in_message(msg)` runs a case-insensitive regex
substitution that replaces any postgres-shaped URL
(`postgres(?:ql)?(?:\+\w+)?://[^\s\"']+`) with `<DSN-REDACTED>`. It is
applied to **every** exception that touches stderr — both the connect
path and the role-update path. The redaction preserves surrounding
context (exception type, descriptive text) so the operator can still
diagnose what failed without seeing credentials.

**Why regex and not a structured logger:** psycopg raises plain
exceptions whose `__str__` embeds the offending DSN as free text; we
do not control the message format. Substituting after the fact is the
only reliable layer.

### Patch 2 — SQLAlchemy driver-prefix stripping

`_strip_sqla_driver_prefix(url)` converts a SQLAlchemy DSN to a libpq
DSN by stripping the `+driver` segment:
`postgresql+psycopg://` → `postgresql://`. Handles `+psycopg`,
`+psycopg2`, `+asyncpg`. No-op for already-libpq URLs.

This means the operator can paste `/luciel/database-url` directly
without manually rewriting it. Removes a class of invocation error
that would otherwise re-trigger the same DSN-in-exception path on
every run.

### Patch 3 — SSM-writability pre-flight

`preflight_ssm_writable(region, ssm_path)` is called **before** any DB
mutation. It uses `ssm:GetParameterHistory` because that operation
requires `ssm:GetParameter` + `ssm:GetParameterHistory` IAM rights but
does NOT require the parameter to exist (it returns
`ParameterNotFound`, which we treat as success — the path is empty
and writable).

The trade-off: this is a read-shaped permission check that infers
write capability. We deliberately avoided a write-then-rollback
pattern (`put_parameter` + `delete_parameter`) because it would
mutate SSM history — exactly what we are trying to keep clean. IAM
policies that grant `PutParameter` on a path almost always grant
`GetParameter` on the same path (the bootstrap-and-verify
convention), so the inference is sound for any policy we control.

On `AccessDenied`, the pre-flight raises a `RuntimeError` whose
message tells the operator exactly which permissions to add — also
DSN-redacted defensively, even though the message contains no
credentials, in case a future maintainer ever embeds one.

---

## 7. Verification performed before commit

- `py_compile` on the patched file: passes
- `python -m scripts.mint_worker_db_password_ssm --help` renders
  without `boto3`/`psycopg` installed (deferred-import discipline
  preserved)
- `--dry-run` invoked with `postgresql+psycopg://...` admin URL: clean
  exit, no DSN echoed in any output, fingerprint generated
- Helper unit tests (executed inline):
  - Redaction strips the actual leaked-string template
    (`LucielDB2026Secure` no longer present)
  - Redaction handles plain `postgresql://`, `postgres://`, and
    case-mixed schemes
  - Prefix-strip handles all three SQLAlchemy variants and is no-op
    on plain `postgresql://`

---

## 8. Deliberate follow-ups (NEXT session)

These are explicitly **not** done tonight. Each requires a fresh
context and at least one prerequisite that the current session cannot
satisfy.

### Follow-up A — Migrate-role IAM gap ~~(P1, blocks Commit 4 retry)~~

> **Correction (2026-05-03 evening):** Original Follow-up A overstated
> the gap. The corrected scope is below; the original text is preserved
> with strikethrough underneath. See `PHASE_3_COMPLIANCE_BACKLOG.md` →
> P3-G for the rescoped backlog item.

**Corrected scope:** `luciel-ecs-migrate-role` is missing only
`ssm:GetParameterHistory` on `/luciel/production/*`. That is a one-line
additive diff to the existing `luciel-migrate-ssm-write` inline policy.
The role already has `ssm:GetParameter`, `ssm:PutParameter`,
`kms:Decrypt`, and the other SSM management actions on the right paths.

**Architectural decision (separate from the IAM diff):** Even though we
*could* additionally grant the migrate role `ssm:GetParameter` on
`/luciel/database-url` so the task could read the admin DSN itself,
that is **not** what we will do. Per the decision recorded in the
Phase 3 backlog (P3-K) and master plan (Phase 2 status snapshot), we
are adopting the **Option 3** boundary: a separate
`luciel-mint-operator-role`, MFA-required AssumeRole, used by the human
operator to read the admin DSN locally and pass it to the ECS task via
`containerOverrides command`. The migrate task role NEVER gets read
access to the admin DSN. This eliminates the class of bug that produced
the original leak.

~~Original text (incorrect — over-stated the gap):~~

~~`luciel-ecs-migrate-role` needs:~~

- ~~`ssm:GetParameter`, `ssm:GetParameterHistory`,
  `ssm:PutParameter` on `arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/worker_database_url`~~
- ~~`ssm:GetParameter` on `arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/database-url`
  (so the task can read the admin URL — though we should consider
  switching to a dedicated mint task-def that takes the admin URL via
  `aws ssm get-parameter` outside the task and passes it via task env
  rather than reading inside the container, since the inside-the-task
  read is what enabled the leak in the first place)~~
- ~~KMS `kms:Decrypt` on the SSM-default KMS key (already implied by SSM
  SecureString permissions, but verify)~~

After the corrected diff (`GetParameterHistory` only) lands AND the
`luciel-mint-operator-role` is created AND MFA is enabled on
`luciel-admin`, Commit 4 retry runs the patched mint script end-to-end
via the Option 3 ceremony. The pre-flight will pass; the SQLAlchemy
prefix will be stripped (defensively, even though the operator is now
passing a libpq-form DSN directly); the connect will succeed; `ALTER
ROLE` will execute; `put_parameter` will write the SecureString.

### Follow-up B — Admin password rotation (P1)

The leaked `LucielDB2026Secure` string lives in CloudWatch until we
rotate. Sequence:

1. Mint a fresh admin password using the patched script invoked
   against the admin role (either via `--role-name`-style variant or
   a quick second mint script that targets `luciel_admin` — to be
   designed next session). Store the new value in
   `/luciel/database-url` SecureString with `Overwrite=True`.
2. Force ECS service redeploy of `luciel-backend` and `luciel-worker`
   so they pick up the new SSM value on container restart.
3. `aws logs delete-log-stream --log-group-name /ecs/luciel-backend
   --log-stream-name migrate/luciel-backend/d6c927a05eb943b5b343ca1ddef0311c`
4. Confirm via `aws logs filter-log-events` that no other stream in
   the log group contains `LucielDB2026Secure`.
5. Document timestamp + actor + new fingerprint in this recap as a §9
   addendum.

This is a deliberate, sequenced operation — not something to rush at
the end of a session.

### Follow-up C — Public ALB attracts CVE scanners (P3 informational)

Independently-observed during this session: the public-facing ALB
receives constant opportunistic probes (PHPUnit RCE, common-CVE
fingerprinting). 401s are holding correctly. Document expected WAF
managed rules (AWS-AWSManagedRulesKnownBadInputsRuleSet,
AWS-AWSManagedRulesCommonRuleSet) for a future hardening commit. No
action required tonight.

---

## 9. Lessons (institutional, for this and future mint scripts)

1. **Any script that takes a credential-bearing URL as an argument
   must redact that URL from every error path.** Pattern E
   ("plaintext credentials live in SSM only") is only as strong as
   its weakest log surface.
2. **`psycopg.connect()` is one of those error paths.** Its
   `ProgrammingError`, `OperationalError`, and connection-string
   parser errors all echo the input DSN verbatim.
3. **Pre-flight every external dependency before the first
   irreversible mutation.** The atomicity gap was theoretical until it
   wasn't.
4. **Dry-runs do not exercise IAM.** They are a tool for argparse and
   ordering verification, not a stand-in for a real-run smoke test in
   a non-prod environment. Add a sandbox/stage test environment to the
   master plan if Phase 4 work makes one feasible.
5. **The repo is the source of truth.** When the agent asked the
   operator to paste script contents during diagnosis, that was a
   process error — the agent has direct read access to the working
   tree. The operator's correction is now part of the standing
   protocol for this branch.

---

## 10. Cross-references

- Commit hardening the script: `2b5ff32`
- Branch: [`step-28-hardening-impl`](https://github.com/aryanonline/Luciel/tree/step-28-hardening-impl)
- Migration creating the role: `alembic/versions/f392a842f885_step28_create_luciel_worker_role.py`
- Operator runbook: `docs/runbooks/step-28-commit-8-luciel-worker-sg.md`
- Phase 3 backlog: `docs/PHASE_3_COMPLIANCE_BACKLOG.md` (updated this
  session with §A, §B, §C above)
- Step 28 master plan: `docs/recaps/2026-04-27-step-28-master-plan.md`

---

## 11. P3-H Resolution — admin password rotation + CloudWatch purge (2026-05-03 23:18–23:56 UTC)

Follow-up B from §8 above is now closed. The `luciel_admin` Postgres
master password was rotated, `/luciel/database-url` SSM was updated
in lockstep, end-to-end verification passed via the SQLAlchemy
consumption path inside an ECS Fargate task, and the contaminated
CloudWatch log stream was deleted. Final residual sweep returned 0 hits.

### 11.1 Prod-mutation timeline (UTC)

| Time | Action | Evidence |
|---|---|---|
| 23:18:31 | `aws rds modify-db-instance --db-instance-identifier luciel-db --master-user-password <NEW> --apply-immediately` returns synchronously | RDS engine version 16.13; no reboot, no downtime; existing connections retained |
| 23:22:54 | `aws ssm put-parameter --name /luciel/database-url --type SecureString --overwrite` accepted | `Version: 1 → 2`; `Tier: Standard`; KMS key `alias/aws/ssm`; DSN length 118 → 140 |
| 23:31:53 | §4.A SQLAlchemy verification probe in `luciel-migrate:12` Fargate task completes | Task `cd676526e958436dab2406b5f604e3bd`; exit code 0; runtime ~50 s; CloudWatch event `P3H_VERIFY_OK select=1 user=luciel_admin db=luciel` |
| 23:52:16 | `aws logs delete-log-stream` on the contaminated stream | Exit code 0; post-delete `describe-log-streams` returns empty |
| 23:56:22 | §7 final residual-leak sweep | 0 hits across `/ecs/luciel-backend` (targeted `migrate/*` + defensive all-streams) and `/ecs/luciel-worker` (defensive); `/aws/rds/instance/luciel-db/postgresql` log group does not exist |

### 11.2 Deleted-stream metadata (preserved for audit)

```
arn               : arn:aws:logs:ca-central-1:729005488042:log-group:/ecs/luciel-backend:log-stream:migrate/luciel-backend/d6c927a05eb943b5b343ca1ddef0311c
creationTime      : 2026-05-03 21:06:23Z
firstEventTimestamp : 2026-05-03 21:06:35Z
lastEventTimestamp  : 2026-05-03 21:06:35Z
storedBytes       : 0  (CloudWatch billing accounting; §5 sweep proved the event existed)
```

This was a single-event stream from the `mint-with-bare-iam-user.ps1`
run on 2026-05-03 21:06 UTC — the original incident vector. The
first-event/last-event timestamps are identical because the failing
mint task wrote one stderr line (containing the leaked DSN) and exited.

### 11.3 Verification probe contract

The §4 probe (`python -c "..."` invoked via `containerOverrides`) used
`from sqlalchemy import create_engine, text` — the same library and
shape the application uses to consume `/luciel/database-url` — to prove
the rotation works end-to-end through the canonical consumer of record.
This is materially stronger than a `psycopg2` smoke test because it
exercises the full SQLAlchemy URL parser (which is what would catch any
shape drift in the new DSN).

The probe emits exactly three log lines, none of which can leak the DSN:

```python
print("P3H_VERIFY_START", flush=True)
print("P3H_VERIFY_OK select=" + str(row[0]) + " user=" + str(row[1]) + " db=" + str(row[2]), flush=True)
print("P3H_VERIFY_FAIL " + type(e).__name__, flush=True)
```

No `str(e)` or `repr(e)` — only the exception class name on failure.
Verified: the new §4 stream
`migrate/luciel-backend/cd676526e958436dab2406b5f604e3bd` did **not**
appear in the §5 sweep, confirming the contract held under live
conditions.

### 11.4 Runtime corrections folded into the P3-H runbook

Three fixes applied inline as we discovered them:

1. **§3 DSN regex.** The runbook design assumed the SSM-stored DSN
   carried `?sslmode=require`. The real value (read live) was
   `postgresql+psycopg://luciel_admin:<pw>@host:5432/luciel` — no
   query string. Working regex:
   `'(postgresql\+psycopg://luciel_admin:)[^@]+(@)' → '${1}<NEW>${2}'`.
2. **§4 BOM-free overrides JSON.** Initial `Set-Content -Encoding utf8`
   wrote a UTF-8 BOM, which `aws ecs run-task --overrides file://`
   rejected. Fixed by using `[System.IO.File]::WriteAllText($path,
   $json, (New-Object System.Text.UTF8Encoding $false))` and verifying
   `[byte[]]::ReadAllBytes($path)[0] -eq 123` (= `'{'`).
3. **§5/§7 AWS CLI pager + scan-window.** The default `aws` pager hangs
   on long JSON output. Mitigation: `$env:AWS_PAGER = ""` plus narrower
   time windows (7 days, not 90) plus `--log-stream-name-prefix
   migrate/` on the targeted pass. Defensive passes ran with
   per-page 90 s job timeouts to bound failure modes.

### 11.5 Known residual: SSM history v1 (tracked as P3-L)

The SSM §3 update incremented version 1 → 2; v1 still exists in the
parameter history with the leaked plaintext. Only `luciel-admin`
(MFA-gated per P3-J) can call `ssm:GetParameterHistory` on the
parameter. The `luciel-mint-operator-role` permission policy grants
only `ssm:GetParameter` (not history). Migrate / worker / backend task
roles have no read access on this parameter.

Mitigation deferred to post-Commit-4: delete-and-recreate the parameter
will drop history. Tracked in `PHASE_3_COMPLIANCE_BACKLOG.md` P3-L.

### 11.6 What this unblocks

- All four Commit-4 prerequisites (P3-J, P3-K, P3-G, P3-H) are now
  resolved.
- Commit 4 mint re-run via the Option 3 ceremony
  (`scripts/mint-with-assumed-role.ps1` without `-DryRun`) is the next
  Phase 2 work item.
- This recap, the canonical recap (v1.4), and the Phase 3 backlog now
  agree on the resolved state.
