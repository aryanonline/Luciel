# Step 28 Phase 2 / Commit 4 — Mint Script DSN Leak Incident

**Date:** 2026-05-03 (Sunday evening EDT)
**Branch:** `step-28-hardening-impl`
**Severity classification:** S2 (credential leak in self-controlled audit
trail; no third-party exposure; caught before any state mutation)
**Status:** Contained — patches committed; deliberate follow-ups scheduled
for next session
**Author:** Aryan Singh, VantageMind AI

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

## 5. Why atomic ordering held (and why we got lucky twice)

The script's intended ordering is:

1. Verify role state in Postgres
2. `ALTER ROLE` with new password
3. `put_parameter` to SSM SecureString

The crash happened at step 0 (the connect itself), so steps 1–3 never
ran. The `luciel_worker` role is still in its post-migration NULL-pw
state.

But there is a second, independently-protective fact: the
`luciel-ecs-migrate-role` IAM role lacks `ssm:GetParameter` and
`ssm:PutParameter` on `/luciel/production/worker_database_url`. So
even if the connect had succeeded, step 3 would have raised
`AccessDeniedException`, leaving the worker role with a fresh password
and SSM with no value — the worker would not be able to authenticate
on the next ECS task restart, and recovery would have required
`--force-rotate` to re-mint after fixing the IAM gap.

That second failure mode is exactly the atomicity gap the new
`preflight_ssm_writable()` helper now closes (see §6, patch 3).

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

### Follow-up A — Migrate-role IAM gap (P1, blocks Commit 4 retry)

`luciel-ecs-migrate-role` needs:

- `ssm:GetParameter`, `ssm:GetParameterHistory`,
  `ssm:PutParameter` on `arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/worker_database_url`
- `ssm:GetParameter` on `arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/database-url`
  (so the task can read the admin URL — though we should consider
  switching to a dedicated mint task-def that takes the admin URL via
  `aws ssm get-parameter` outside the task and passes it via task env
  rather than reading inside the container, since the inside-the-task
  read is what enabled the leak in the first place)
- KMS `kms:Decrypt` on the SSM-default KMS key (already implied by SSM
  SecureString permissions, but verify)

After this is applied, Commit 4 retry runs the patched mint script
end-to-end. The pre-flight will pass; the SQLAlchemy prefix will be
stripped; the connect will succeed; `ALTER ROLE` will execute;
`put_parameter` will write the SecureString.

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
