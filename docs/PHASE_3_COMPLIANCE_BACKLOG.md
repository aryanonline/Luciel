# Phase 3 — Compliance Backlog

**Status:** Tracked. Items here are **not** part of Step 28 Phase 2.
They surfaced during Phase 2 hotfix diagnosis and represent compliance
gaps in service of Luciel's PIPEDA posture and future SOC 2 / GDPR
readiness for the multi-tenant brokerage SaaS use case.

**Owner:** Aryan Singh
**Created:** 2026-05-03 (mid-Phase 2)
**Trigger:** Phase 2 HOTFIX commit `2c7d0fb` revealed under-instrumented
audit-emission paths during the Pillar 17 / Pillar 19 diagnosis.
**Schedule rule:** Phase 3 begins **after** Phase 2 prod-touching commits
4-7 land green and stable in prod for ≥ 7 days. Do not interleave.

---

## Severity tiers (compliance-first, not feature-first)

- **P0 — silent integrity loss.** A regulator-facing claim ("we revoke
  keys", "we audit tenant lifecycle", "we cascade retention") has no
  audit evidence. Sue-risk on customer dispute. Fix before any sales
  motion that promises audit immutability.
- **P1 — gap with workaround.** Compliance posture is provable today
  via DB inspection but not via the audit-log API surface. Fixable by
  threading audit emission through the missing layer; no data loss
  if deferred a quarter.
- **P2 — documentation / posture.** Behavior is correct but undocumented;
  a regulator or auditor would have to reconstruct intent from code.
  Risk surfaces during diligence, not during operation.

---

## P3-A. OnboardingService writes ZERO audit rows  *(P0)*

**Discovered:** 2026-05-03 during Pillar 19 diagnosis.

**What's missing:** When a new tenant is onboarded via
`POST /api/v1/admin/tenants/onboard` → `OnboardingService.onboard_tenant`,
the service creates atomically:

1. `tenant_configs` row
2. `domain_configs` row (default domain)
3. `retention_policies` rows (5 categories: sessions, messages,
   memory_items, traces, knowledge)
4. `api_keys` row (the first admin key)

**Zero of these emit `admin_audit_logs` rows.** Verified by:
```bash
grep -c "audit\|AdminAudit\|record(" app/services/onboarding_service.py
# returns 0
```

The downstream `api_key_service.create_key()` call also skips audit
emission — only the API endpoint `POST /api/v1/admin/api-keys` writes
the `ACTION_CREATE` audit row, and onboarding bypasses that endpoint
to mint its first key directly through the service.

**Why this is P0:**
- A brokerage onboarded today has no immutable record of:
  - WHO created the tenant (which platform_admin actor)
  - WHEN it was created (audit log timestamp, not just `created_at`)
  - WHAT retention policies were initially set (vs later modified)
  - WHAT the first admin key's permissions and rate limit were
- For PIPEDA: the tenant lifecycle event is not in the audit trail.
  A breach investigation would have to fall back to `created_at`
  columns and infer actor from operational logs.
- For SOC 2: this is a CC7.2 audit-evidence gap.
- For commercial defense: a brokerage disputing a charge or claiming
  unauthorized account creation cannot be answered from the audit log.

**Fix shape (when prioritized):**
1. Add `audit_ctx: AuditContext` parameter to `OnboardingService.onboard_tenant`
   (REQUIRED, not optional — same contract as `bulk_soft_deactivate_memory_items_for_domain`).
2. Emit four audit rows in the same transaction as the writes:
   - `ACTION_CREATE` / `RESOURCE_TENANT` (with `tenant_id=new_tid`)
   - `ACTION_CREATE` / `RESOURCE_DOMAIN` (with default_domain_id)
   - `ACTION_CREATE` / `RESOURCE_RETENTION_POLICY` × 5 (one per category, OR a single bulk row with breakdown)
   - `ACTION_CREATE` / `RESOURCE_API_KEY` (admin key — currently emitted only by API endpoint mint path)
3. Thread `audit_ctx` down from the API endpoint at `app/api/v1/admin.py`
   line 151 (`OnboardingService(db)` construction).
4. Add Pillar 20 (or extend Pillar 1) to assert exactly four (or 8 if
   per-policy) `ACTION_CREATE` rows tagged with the new tenant_id
   appear after onboard.

**Estimated effort:** 1 commit, ~120 LOC + 1 new pillar (~80 LOC).
**Touches:** `app/services/onboarding_service.py`, `app/api/v1/admin.py`,
`app/verification/tests/pillar_20_onboarding_audit.py` (new).
**Cross-references:** canonical-recap §4.1 (drift list), Invariant 4
(audit-before-commit), Pillar 19 docstring.

---

## P3-B. ApiKeyService.create_key writes no audit row  *(P1)*

**Discovered:** 2026-05-03 during Pillar 19 diagnosis (related to P3-A).

**What's missing:** `ApiKeyService.create_key()` (api_key_service.py
line 118-212) flushes a new `api_keys` row but emits no `admin_audit_logs`
row. The audit emission lives in the API endpoint at
`app/api/v1/admin.py` line 594-611, *after* the service call returns.

**Why this is P1, not P0:** Today, the only callers of `create_key` that
skip the API endpoint are:
1. `OnboardingService.onboard_tenant` (covered by P3-A above)
2. Internal scripts (`scripts/mint_platform_admin_ssm.py`,
   `scripts/rotate_platform_admin_keys.py`) — bootstrap and break-glass
   flows that already log to operational logs and SSM.

So the gap is real but the blast radius is contained. Still worth
fixing to make the contract uniform: every `api_keys` insertion produces
an audit row, end of story.

**Fix shape:**
1. Move audit emission into `ApiKeyService.create_key()` itself, gated
   on `audit_ctx is not None` (with `AuditContext.system(label="create_key")`
   fallback for legacy/script callers).
2. Remove the duplicate emission block from `app/api/v1/admin.py`
   line 594-611 to avoid double-counting (or audit dedup if the API
   endpoint wants additional context beyond the service-level row).
3. Add a regression test that exercises `OnboardingService` and
   asserts the admin key's `ACTION_CREATE` audit row is present (this
   overlaps with P3-A's Pillar 20).

**Estimated effort:** 1 commit, ~40 LOC. Trivial *after* P3-A lands.
**Cross-references:** Pillar 17 docstring (D5 contract).

---

## P3-C. Bulk-summary audit emission is undocumented as compliance posture  *(P2)*

**Discovered:** 2026-05-03 during Pillar 7 diagnosis.

**What's missing:** Several cascade paths emit ONE summary audit row
covering N affected resources, not N+1 individual rows:

- `LucielInstanceRepository.deactivate_all_for_domain` — one row covers
  both domain-scope and agent-scope LucielInstances.
- `AdminService.bulk_soft_deactivate_memory_items_for_domain` — one row
  covers all `memory_items` rows attributed to agents in the domain.
- `AdminService.bulk_soft_deactivate_memory_items_for_tenant` — same
  pattern at tenant scope.
- `AdminService.deactivate_domain` — one row covers all `agents` in
  the domain.

The `after_json` payload always contains `affected_pks`, `count`, and
sometimes a `breakdown` (per-agent or per-instance grouping). So the
information IS preserved per-resource — just compressed into one row.

**Why this is correct but undocumented:**
- For audit *export* (CSV/JSON for a regulator), one row per cascade
  event reads more naturally than N+1 rows reconstructed from scratch.
- For audit *immutability proofs* (e.g. hash-chain of rows), fewer
  rows means smaller proofs and less write amplification.
- For audit *retention* (PIPEDA P5), bulk rows compress better and
  age out cleaner than per-resource rows.

**The gap:** No document explains this is the *intended posture*.
A regulator asking "why does row N represent 47 deactivated memory items
instead of 47 rows" needs a written answer in the canonical recap or
in a dedicated `docs/compliance/audit-emission-posture.md`.

**Fix shape:**
1. Add `docs/compliance/audit-emission-posture.md` documenting the
   bulk-vs-per-row decision matrix, what `after_json` carries, how
   to expand a bulk row into per-resource detail at audit-export time.
2. Add a §X to canonical-recap referencing the posture doc.
3. (Optional) Add a helper in `app/repositories/admin_audit_repository.py`
   that takes a bulk row and emits per-resource shadow rows on demand
   — useful if a customer/regulator demands per-row format.

**Estimated effort:** 1 commit, ~60 LOC of docs + 1 recap section update.
**Touches:** `docs/compliance/audit-emission-posture.md` (new),
`docs/CANONICAL_RECAP.md`, optionally `admin_audit_repository.py`.

---

## P3-D. Cross-tenant scope-leak fuzz suite  *(P1)*

**Discovered:** 2026-05-03 (proactive, not bug-driven).

**What's missing:** Pillar 19 asserts that *one* specific cross-tenant
attempt (a tenant_admin querying `?tenant_id=<other-real-tenant>`)
returns rows scoped to the caller's tenant. But the audit-log API has
many filter axes:

- `tenant_id` (currently guarded)
- `domain_id`
- `agent_id`
- `actor_label`
- `resource_pk`
- `resource_type`
- date ranges

Each axis is a potential leak vector if scope-override middleware
forgets to also force that filter for non-platform-admin callers.

**Fix shape:**
1. Add `app/verification/tests/pillar_21_audit_log_fuzz_scope.py` — for
   each filter axis, mint a row in tenant B, then query as tenant_admin
   of tenant A with that filter pointing at tenant B's row. Assert the
   row is NOT visible.
2. Cover positive case (caller's own data IS visible with the same
   filter shape) so the test isn't passing by accident.

**Estimated effort:** 1 commit, ~150 LOC.

---

## P3-E. Audit-log immutability proof  *(P1)*

**Discovered:** 2026-05-03 (proactive).

**What's missing:** Phase 2 Commit 2 assertion 4 confirms POST/PUT/PATCH/
DELETE on `/audit-log` return 404/405. Good — that closes the API
surface. But:

- DB role grants are not asserted at runtime. A `luciel_admin` role
  with UPDATE/DELETE on `admin_audit_logs` would let a compromised
  app process tamper with the audit log directly via SQL.
- No hash-chain or write-once-read-many (WORM) enforcement at the row
  level. An operator with DB superuser could rewrite history.

**Fix shape (per pillar):**
1. Pillar 22: at runtime, query `information_schema.role_table_grants`
   and assert the app's DB role has `INSERT, SELECT` only on
   `admin_audit_logs`, no UPDATE/DELETE.
2. Pillar 23 (longer): add a per-row hash chain (each row's hash
   includes the previous row's hash). On read, verify the chain.
   Tampering breaks the hash chain immediately. This needs an Alembic
   migration to add `hash` and `prev_hash` columns.

**Estimated effort:** Pillar 22: ~1 commit, ~80 LOC. Pillar 23:
~3 commits, schema + repo + verification, ~400 LOC.

---

## P3-F. Retention purge audit coverage  *(P1)*

**Discovered:** 2026-05-03 (during Phase 2 Commit 8 retention work).

**What's missing:** `RetentionService.enforce_all_policies` writes
`deletion_logs` rows (not `admin_audit_logs`) for purged data. Per
PIPEDA P5 / data-minimization, the deletion event itself IS recorded.
But `deletion_logs` is a separate table from `admin_audit_logs` — it
doesn't surface in the audit-log API and isn't subject to the same
immutability guarantees.

**Decision needed:** Should retention purges *also* emit a row to
`admin_audit_logs` (for unified audit export), or is `deletion_logs`
the canonical compliance record? Currently both exist with overlapping
but non-identical semantics.

**Fix shape:** Either
- (A) Document `deletion_logs` as the canonical record for purge events,
  exclude from the `admin_audit_logs` API contract, treat as separate
  audit stream. Add §to recap.
- (B) Mirror every `deletion_logs` insert with an `admin_audit_logs`
  row of `action='retention_purge'`, accept the duplication, gain
  unified export.

**Estimated effort:** Decision + 1 commit, ~80 LOC either way.

---

## P3-G. Migrate-role missing `ssm:GetParameterHistory`  *(P2 — RESOLVED 2026-05-03 evening)*

**Status:** ✅ **RESOLVED** 2026-05-03 ~20:09 EDT (operator-applied via
`aws iam put-role-policy` per
`docs/runbooks/step-28-p3-k-execute.md` Step 2).

**Resolution evidence (verbatim live policy, captured 2026-05-03 22:56 EDT):**

```json
{
    "RoleName": "luciel-ecs-migrate-role",
    "PolicyName": "luciel-migrate-ssm-write",
    "PolicyDocument": {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ReadWriteSsmParameters",
                "Effect": "Allow",
                "Action": [
                    "ssm:GetParameter",
                    "ssm:GetParameterHistory",
                    "ssm:PutParameter",
                    "ssm:DescribeParameters",
                    "ssm:AddTagsToResource",
                    "ssm:ListTagsForResource"
                ],
                "Resource": [
                    "arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/*",
                    "arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/bootstrap/*"
                ]
            },
            {
                "Sid": "KmsViaSsm",
                "Effect": "Allow",
                "Action": [
                    "kms:Encrypt",
                    "kms:Decrypt",
                    "kms:GenerateDataKey"
                ],
                "Resource": "*",
                "Condition": {
                    "StringEquals": {
                        "kms:ViaService": "ssm.ca-central-1.amazonaws.com"
                    }
                }
            }
        ]
    }
}
```

Live state matches
`infra/iam/luciel-migrate-ssm-write-after-p3-g.json` byte-for-byte
(verified by canonical-form `diff` post-resolution).

---

### Original P3-G entry (preserved for audit trail)



**Discovered:** 2026-05-03 during the Commit 4 mint real-run.
**Rescoped:** 2026-05-03 evening, after a direct read of the
`luciel-ecs-migrate-role` inline policy `luciel-migrate-ssm-write`.

**What's actually missing (corrected):**
`luciel-ecs-migrate-role` (`arn:aws:iam::729005488042:role/luciel-ecs-migrate-role`)
is missing only one IAM action: `ssm:GetParameterHistory` on
`/luciel/production/*`. The role's existing inline policy already
grants:

- `ssm:GetParameter`, `ssm:PutParameter`, `ssm:DescribeParameters`,
  `ssm:AddTagsToResource`, `ssm:ListTagsForResource` on
  `/luciel/production/*` and `/luciel/bootstrap/*`
- `kms:Encrypt`, `kms:Decrypt`, `kms:GenerateDataKey` scoped via
  `kms:ViaService = ssm.ca-central-1`

The new `preflight_ssm_writable()` helper in commit `2b5ff32` calls
`ssm:GetParameterHistory` to verify writability without mutating
state; that single action is the genuine remaining gap.

**Original (incorrect) framing:** The first version of this item
claimed the role lacked `ssm:GetParameter` and `ssm:PutParameter` and
that the gap blocked Commit 4 entirely. That diagnosis was wrong; it
conflated a separate SSM recon attempt with the actual policy state.
The corrected severity is P2 (the patched mint script will run today
with a single IAM action added), not P1.

**Why this is P2 (corrected):** The mint script will function with
the existing policy IF we drop the pre-flight, but we want to keep
the pre-flight as drift-insurance (see `2026-05-03-mint-incident.md`
§5 corrected text). Adding `GetParameterHistory` is a one-line policy
diff with no risk profile.

**Important: this item does NOT block Commit 4 anymore.** Commit 4 is
blocked instead on P3-J (MFA on `luciel-admin`) and P3-K
(mint-operator role) per the Option 3 architectural decision.

**Fix shape:**
1. Edit `luciel-migrate-ssm-write` inline policy on
   `luciel-ecs-migrate-role`. Add `ssm:GetParameterHistory` to the
   `Action` list of the existing `SsmManageBootstrapAndProductionAdminKeyParams`
   statement. No new statement needed; no new resource needed.
2. Save the updated policy JSON to
   `infra/iam/luciel-ecs-migrate-role-policy.json` for reproducibility.
3. Apply via `aws iam put-role-policy --role-name luciel-ecs-migrate-role --policy-name luciel-migrate-ssm-write --policy-document file://infra/iam/luciel-ecs-migrate-role-policy.json`.
4. Verify via `aws iam simulate-principal-policy --policy-source-arn <role-arn> --action-names ssm:GetParameterHistory --resource-arns arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/worker_database_url` (expect `allowed`).

**Sequencing:** Bundle with P3-K execution (the mint-operator role
setup). Same session, same commit.

**Estimated effort:** ~15 minutes including the JSON capture.
**Cross-references:** `docs/recaps/2026-05-03-mint-incident.md` §8
Follow-up A (corrected), commit `2b5ff32`. Supersedes the original
(incorrect) P3-G in commits `31e2b16` and `43e2e7a`.

---

## P3-H. Admin password rotation needed (CloudWatch leak)  *(P1 — RESOLVED 2026-05-03 23:56:22 UTC)*

**Status:** ✅ **RESOLVED** 2026-05-03 23:56:22 UTC. RDS master password
rotated, SSM `/luciel/database-url` updated to v2, ECS-side end-to-end
verification passed via SQLAlchemy probe in `luciel-migrate:12` task,
contaminated CloudWatch log stream deleted, final residual-leak sweep
returned 0 hits across `/ecs/luciel-backend`, `/ecs/luciel-worker`
(`/aws/rds/instance/luciel-db/postgresql` log group does not exist).

Applied per `docs/runbooks/step-28-p3-h-rotate-and-purge.md` §1–§7
end-to-end. Operator + agent walked the runbook step-by-step;
three runtime fixes were folded into the runbook inline (see
§§3/§4/§5 correction blocks).

**Prod-mutation timeline (UTC):**

| Time | Action |
|---|---|
| 23:18:31 | RDS `modify-db-instance --master-user-password` synchronous return; no reboot, no downtime |
| 23:22:54 | SSM `put-parameter` v1 → v2 on `/luciel/database-url` (length 118 → 140; standard tier; alias/aws/ssm KMS key) |
| 23:31:53 | §4 verification: `P3H_VERIFY_OK select=1 user=luciel_admin db=luciel` from `luciel-migrate:12` Fargate task `cd676526e958436dab2406b5f604e3bd`, exit code 0, runtime ~50 s |
| 23:52:16 | §6 `delete-log-stream` on `/ecs/luciel-backend / migrate/luciel-backend/d6c927a05eb943b5b343ca1ddef0311c` — exit 0, post-delete `describe-log-streams` returned empty |
| 23:56:22 | §7 final sweep: 0 hits across all three target log groups |

**Deleted-stream metadata snapshot (preserved for audit):**

```
arn               : arn:aws:logs:ca-central-1:729005488042:log-group:/ecs/luciel-backend:log-stream:migrate/luciel-backend/d6c927a05eb943b5b343ca1ddef0311c
creationTime      : 2026-05-03 21:06:23Z
firstEventTimestamp : 2026-05-03 21:06:35Z
lastEventTimestamp  : 2026-05-03 21:06:35Z
storedBytes       : 0  (single-event stream; CloudWatch billing accounting)
```

**Verification probe (the §4 fresh task) used the SQLAlchemy consumption
path** — `from sqlalchemy import create_engine, text` — not raw psycopg.
This exercises the same code path the real backend uses to consume the
DSN, proving the rotation works end-to-end through the canonical
consumer of record. Probe contract: emit only `P3H_VERIFY_START`,
`P3H_VERIFY_OK select=N user=X db=Y`, or `P3H_VERIFY_FAIL <ExceptionClassName>`
— never `str(e)` or `repr(e)`. Verified: the new §4 stream
`migrate/luciel-backend/cd676526e958436dab2406b5f604e3bd` was excluded
from the §5 sweep results, confirming the contract held.

**Known residual (tracked separately as P3-L below):** SSM parameter
`/luciel/database-url` history version 1 still contains the plaintext
`LucielDB2026Secure`. Only `luciel-admin` (now MFA-gated per P3-J) can
read parameter history via `ssm:GetParameterHistory`. Mitigation
deferred to post-Commit-4 cleanup.

---

### Original P3-H entry (preserved for audit trail)

**Discovered:** 2026-05-03 during the Commit 4 mint real-run.

**What happened:** The `luciel_admin` Postgres password
(`LucielDB2026Secure`) was leaked to one CloudWatch log event in log
group `/ecs/luciel-backend`, stream
`migrate/luciel-backend/d6c927a05eb943b5b343ca1ddef0311c`, when an
unsanitized `psycopg.ProgrammingError` echoed the admin DSN to the
ECS task's stderr.

**Blast radius (effective audience of one):** AWS account
`729005488042` is single-tenant; only the `luciel-admin` IAM user has
CloudWatch read access. No third-party log forwarding, no federated
roles, no cross-account access. See incident recap §4 for full
analysis.

**Why this is P1:** The leaked credential persists in CloudWatch
until rotated. Time-bounded by the next IAM-credential incident or
any future SOC 2 / penetration-test review that would surface the
leaked log line.

**Fix shape (sequenced — do as a single deliberate operation):**
1. Mint a fresh admin password using the patched mint script
   (`2b5ff32`). Either (a) extend the script with a `--role-name`
   argument so it can target `luciel_admin` not just `luciel_worker`,
   or (b) write a parallel `mint_admin_password.py` that reuses the
   same helpers (`_redact_dsn_in_message`,
   `_strip_sqla_driver_prefix`, `preflight_ssm_writable`).
2. Store the new admin DSN at `/luciel/database-url` SecureString with
   `Overwrite=True`.
3. Force ECS service redeploy of `luciel-backend` and any other
   service that consumes `/luciel/database-url`, so they pick up the
   new value on container restart.
4. Smoke-test post-redeploy: confirm the backend serves traffic with
   the new credential.
5. Delete the leaking log stream:
   `aws logs delete-log-stream --log-group-name /ecs/luciel-backend --log-stream-name migrate/luciel-backend/d6c927a05eb943b5b343ca1ddef0311c`
6. Verify no other stream in the log group contains the leaked
   string:
   `aws logs filter-log-events --log-group-name /ecs/luciel-backend --filter-pattern '"LucielDB2026Secure"'`
   (expect zero events).
7. Append a §9 addendum to `docs/recaps/2026-05-03-mint-incident.md`
   recording timestamp, actor, new admin-pw fingerprint.

**Sequencing constraint:** Do **after** P3-G (the IAM gap fix) so the
rotation can run cleanly via the patched mint pattern.

**Estimated effort:** ~1 hour operator time once P3-G is closed.
**Cross-references:** `docs/recaps/2026-05-03-mint-incident.md` §8
(Follow-up B).

---

## P3-J. Enable MFA on `luciel-admin` IAM user  *(P0 — RESOLVED 2026-05-03 23:48 UTC)*

**Status:** ✅ **RESOLVED** 2026-05-03 23:48:11 UTC.

**Resolution evidence (verbatim from operator console, 2026-05-03 evening):**

```
aws iam list-mfa-devices --user-name luciel-admin
{
    "MFADevices": [
        {
            "UserName": "luciel-admin",
            "SerialNumber": "arn:aws:iam::729005488042:mfa/Luciel-MFA",
            "EnableDate": "2026-05-03T23:48:11+00:00"
        }
    ]
}
```

**P3-J Step 0b (account-wide sweep) — also clean:**

```
aws iam list-users --query "Users[].UserName" --output table
+----------------+
|  luciel-admin  |
+----------------+
```

Account `729005488042` has exactly one IAM user. With MFA now enabled
on that user, the account-wide privileged-human MFA boundary is fully
closed.

**MFA SerialNumber for downstream P3-K trust policy:**
`arn:aws:iam::729005488042:mfa/Luciel-MFA` — this is the value the
`luciel-mint-operator-role` trust policy will use in its
`Bool: aws:MultiFactorAuthPresent=true` + `NumericLessThan:
aws:MultiFactorAuthAge=3600` conditions.

**Forward-looking guard (added as part of resolution):** every IAM
user created in `729005488042` from this point onward must have MFA
enabled before first console use. This applies to future contractors,
co-founders, CI users with console access, and any service user that
is given a console password. Service users created with programmatic
access only (no console password) are exempt — they authenticate via
long-lived access keys, which is a separate Phase 3 concern (P3-X,
future: short-lived credentials via SSO / Identity Center).

---

### Original P3-J entry (preserved for audit trail)

**Discovered:** 2026-05-03 evening, while designing the Option 3
boundary (P3-K). Confirmed via `aws iam list-mfa-devices --user-name
luciel-admin` returning `"MFADevices": []`.

**What's missing:** `luciel-admin` — the single human IAM principal
that can do anything in AWS account `729005488042` — has no MFA device
attached. Authentication relies entirely on the long-lived password
+ access key pair. There is no second factor.

**Why this is P0 (the highest-severity item in the backlog):**

- This is bigger than the worker-DSN incident we just spent two
  sessions diagnosing. That incident had a single-IAM-user blast
  radius. This gap IS the single IAM user's blast radius.
- A brokerage CTO doing tech DD will find this in the first 10
  minutes of an IAM review. Nothing else in the compliance posture
  matters until this is fixed.
- For SOC 2 CC6.1, MFA on privileged users is a baseline control,
  not an aspiration.
- Every other Option 3-style boundary we're considering depends on
  MFA being a meaningful condition. `aws:MultiFactorAuthPresent` is
  paper-only without an actual MFA device.

**Fix shape (operator-executed, ~5 minutes):**

1. Sign in to AWS console as `luciel-admin`.
2. Top-right → username → Security credentials → Multi-factor
   authentication → Assign MFA device.
3. Device name: `luciel-admin-virtual-mfa` (or the operator's
   convention).
4. Choose Authenticator app. Open authenticator (Google Authenticator,
   Authy, 1Password, Bitwarden) on phone, scan QR.
5. Enter two consecutive 6-digit codes.
6. Click Add MFA.
7. Verify with `aws iam list-mfa-devices --user-name luciel-admin` —
   expect a device entry with a `SerialNumber` ARN.
8. Record the SerialNumber ARN; it is the MFA condition value used in
   P3-K's trust policy.

**Sequencing:** This is the absolute first item to execute in any
remaining Phase 2 / Phase 3 work. Nothing else depends on staying
deferred. *(Resolved 2026-05-03 23:48 UTC — see Status block at top
of this section.)*

**Estimated effort:** ~5 minutes operator time. *(Actual: ~2 minutes.)*
**Cross-references:** `docs/recaps/2026-04-27-step-28-master-plan.md`
Phase 2 Status Snapshot section, commit `2b5ff32`.

---

## P3-K. Create `luciel-mint-operator-role` (MFA-required AssumeRole)  *(P1 — RESOLVED 2026-05-03 evening)*

**Status:** ✅ **RESOLVED** 2026-05-03 ~20:14 EDT (role created), ~20:14
EDT (inline policy attached), ~20:19 EDT (smoke test via
`mint-with-assumed-role.ps1 -DryRun` succeeded).

Applied per `docs/runbooks/step-28-p3-k-execute.md` Steps 3, 4, 5.
Operator ran the runbook end-to-end without docs-side coordination;
recon pass on 2026-05-03 22:54 EDT confirmed live state matches design
byte-for-byte. Drift entry below covers the audit-trail aspect.

**Resolution evidence — role (verbatim, captured 2026-05-03 22:58 EDT):**

```json
{
    "Role": {
        "Path": "/",
        "RoleName": "luciel-mint-operator-role",
        "RoleId": "AROA2TPA466VPPPBLFBAJ",
        "Arn": "arn:aws:iam::729005488042:role/luciel-mint-operator-role",
        "CreateDate": "2026-05-04T00:14:10+00:00",
        "AssumeRolePolicyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "AllowLucielAdminToAssumeWithMFA",
                    "Effect": "Allow",
                    "Principal": {
                        "AWS": "arn:aws:iam::729005488042:user/luciel-admin"
                    },
                    "Action": "sts:AssumeRole",
                    "Condition": {
                        "Bool": {
                            "aws:MultiFactorAuthPresent": "true"
                        },
                        "NumericLessThan": {
                            "aws:MultiFactorAuthAge": "3600"
                        }
                    }
                }
            ]
        },
        "Description": "Option 3 mint-operator role; MFA-required AssumeRole. P3-K (2026-05-03).",
        "MaxSessionDuration": 3600,
        "RoleLastUsed": {
            "LastUsedDate": "2026-05-04T00:19:22+00:00",
            "Region": "ca-central-1"
        }
    }
}
```

**Resolution evidence — inline permission policy (verbatim):**

```json
{
    "RoleName": "luciel-mint-operator-role",
    "PolicyName": "luciel-mint-operator-permissions",
    "PolicyDocument": {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ReadAdminDsnFromSsm",
                "Effect": "Allow",
                "Action": ["ssm:GetParameter"],
                "Resource": ["arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/database-url"]
            },
            {
                "Sid": "DescribeAdminDsnParameter",
                "Effect": "Allow",
                "Action": ["ssm:DescribeParameters"],
                "Resource": "*",
                "Condition": {"StringEquals": {"ssm:ResourceTag/luciel:role": "mint-operator"}}
            },
            {
                "Sid": "DecryptAdminDsnViaSsm",
                "Effect": "Allow",
                "Action": ["kms:Decrypt"],
                "Resource": "*",
                "Condition": {"StringEquals": {"kms:ViaService": "ssm.ca-central-1.amazonaws.com"}}
            }
        ]
    }
}
```

**Smoke-test evidence:** `RoleLastUsed.LastUsedDate:
2026-05-04T00:19:22+00:00` (= 2026-05-03 20:19 EDT). The role was
successfully assumed via
`mint-with-assumed-role.ps1 -DryRun` ~5 minutes after creation.
`aws ssm get-parameter --name /luciel/production/worker_database_url`
returned `ParameterNotFound` post-smoke-test, confirming the dry-run
performed no SSM writes and no Postgres mutations — i.e. the mechanism
is proven without having executed the real Commit 4 mint.

**Live-vs-design verification:** all three policies (trust,
permission, migrate-role-after-P3-G) confirmed byte-for-byte identical
to `infra/iam/*.json` design files via canonical-form `diff` on
2026-05-03 22:58 EDT. Zero drift.

**MaxSessionDuration:** 3600 s (1 hour) as designed.
**No managed policies attached** (`AttachedPolicies: []` confirmed).
**Inline policies:** exactly one (`luciel-mint-operator-permissions`).

---

### Original P3-K entry (preserved for audit trail)



**Discovered:** 2026-05-03 evening, during the architectural
discussion of how to hand the admin DSN to mint operations. The
obvious option (grant the migrate task role `ssm:GetParameter` on the
admin DSN) reproduces the conditions that produced the original leak.
A tighter alternative exists.

**What's missing:** A dedicated IAM role for human-operator-initiated
credential operations, with three properties:

1. Trust policy: only `luciel-admin` IAM user can assume it, AND the
   assume-role call must include MFA (`aws:MultiFactorAuthPresent =
   true`).
2. Permission set: `ssm:GetParameter` on `/luciel/database-url`
   (admin DSN), `kms:Decrypt` scoped to SSM. Nothing else.
3. Max session duration: 1 hour (3600 s) or shorter.

The existing pattern (read admin DSN as `luciel-admin` directly) has
two failure modes the new role eliminates:

- The read looks like normal admin activity in CloudTrail; there is
  no auditable boundary between "human did a sensitive credential
  operation" and "human read a config value."
- If the admin DSN is needed for an automated task, the task role
  needs read access — which is exactly what produced the original
  leak.

With the new role, every human-initiated mint produces a CloudTrail
`AssumeRole` event tagged with the operator's principal and the MFA
condition. The migrate task role NEVER gets read on the admin DSN.

**Why this is P1 (and where it sits in the priority stack):**

- P3-J (MFA) must come first. Without MFA on `luciel-admin`, this
  role's trust policy condition is paper-only.
- After P3-J, this is the immediate next step. It unblocks Commit 4
  mint re-run (the original Phase 2 work).
- For SOC 2: this is the IAM-traceable boundary between
  human-initiated credential operations and routine admin activity.
  It's the kind of control auditors specifically look for.

**Fix shape:**

1. Author trust policy at
   `infra/iam/luciel-mint-operator-role-trust-policy.json`. Principal:
   `arn:aws:iam::729005488042:user/luciel-admin`. Condition:
   `"Bool": {"aws:MultiFactorAuthPresent": "true"}` AND
   `"NumericLessThan": {"aws:MultiFactorAuthAge": "3600"}`.
2. Author permission policy at
   `infra/iam/luciel-mint-operator-role-policy.json`. Action:
   `ssm:GetParameter` on
   `arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/database-url`,
   plus `kms:Decrypt` scoped via `kms:ViaService = ssm.ca-central-1`.
3. Create role: `aws iam create-role --role-name luciel-mint-operator-role --assume-role-policy-document file://... --max-session-duration 3600`.
4. Attach inline policy:
   `aws iam put-role-policy --role-name luciel-mint-operator-role --policy-name luciel-mint-operator-read --policy-document file://...`.
5. Author PowerShell helper at `scripts/mint-with-assumed-role.ps1`
   that wraps the assume-role + env-var dance + admin-DSN read +
   ECS task launch + env-var clear.
6. Test the ceremony end-to-end (assume-role with MFA token, read
   `/luciel/database-url`, smoke-test that the migrate task role
   CANNOT read it).
7. Document in
   `docs/runbooks/step-28-commit-8-luciel-worker-sg.md` and link from
   the canonical recap §2.6 (Operator patterns).

**Sequencing:** Execute after P3-J. Bundle the migrate-role policy
diff (P3-G corrected) in the same session and same commit — they're
related IAM work and reviewing them together is cleaner.

**Estimated effort:** ~45 minutes operator + 1 commit (~200 LOC of
JSON + ~80 LOC of PowerShell helper + runbook update).
**Cross-references:** `docs/recaps/2026-05-03-mint-incident.md` §8
(corrected Follow-up A), commit `2b5ff32`.

---

## P3-L. SSM parameter history retains plaintext `LucielDB2026Secure`  *(P2 — deferred to post-Commit-4)*

**Discovered:** 2026-05-03 during P3-H execution. The remediation in
P3-H replaced the *current* value of `/luciel/database-url` (v1 → v2)
but SSM retains parameter version history. v1 is still readable via
`aws ssm get-parameter-history --name /luciel/database-url --with-decryption`
and contains the leaked plaintext.

**Why this is P2, not P0/P1:**

- The only IAM principal that can call `ssm:GetParameterHistory` on this
  parameter is `luciel-admin`, which is now MFA-gated per P3-J
  (`Bool: aws:MultiFactorAuthPresent=true` enforced on the AWS account
  for the only IAM user). The `luciel-mint-operator-role` permission
  policy only grants `ssm:GetParameter` (not `GetParameterHistory`),
  and the migrate / worker / backend task roles have no read on this
  parameter at all.
- The blast-radius argument from `2026-05-03-mint-incident.md` §4 still
  applies: any compromise of `luciel-admin` is already a root-equivalent
  breach via console-driven RDS password reset, so the historical SSM
  value does not expand the breach surface meaningfully.
- The plaintext was rotated at 2026-05-03 23:18:31 UTC; the leaked
  password is no longer accepted by RDS as of that moment.

**Why this is still worth fixing:**

- For SOC 2 / regulator-facing posture, "plaintext credential persists
  in SSM history" reads worse than "plaintext credential persists in
  CloudWatch log stream" — even though the access-control story is
  identical. Cleaner to make the residual zero.
- Defense-in-depth against future IAM regression that might broaden
  history-read access.

**Why this is deferred to post-Commit-4:**

- The cleanest mitigation is to **delete and recreate** the parameter
  (deletion clears all history; recreation starts from v1 with the
  current rotated value). This is mildly disruptive: SSM lookups by
  consumers between delete and recreate will fail. Backend tasks already
  running hold the value in their environment, but any task restart
  during the window will fail to start.
- Doing this *before* Commit 4 mint re-run adds an unnecessary outage
  window to a session that already has prod-mutating work. Doing it
  *after* Commit 4 lands keeps the rotation work atomic.

**Fix shape (when prioritized):**

1. Coordinate a low-traffic window (off-hours).
2. Read current parameter value via mint-operator-role into a
   SecureString.
3. `aws ssm delete-parameter --name /luciel/database-url`.
4. Verify history is gone:
   `aws ssm get-parameter-history --name /luciel/database-url`
   should return `ParameterNotFound`.
5. Recreate via
   `aws ssm put-parameter --name /luciel/database-url --value <secure-string-from-step-2> --type SecureString --key-id alias/aws/ssm`.
6. Force a backend service redeploy to confirm consumers can read v1
   of the recreated parameter.
7. Capture evidence; mark P3-L resolved.

**Estimated effort:** ~30 minutes operator time including the redeploy
verification window.
**Cross-references:** `docs/runbooks/step-28-p3-h-rotate-and-purge.md`
(§3 SSM update is what created the version-1 residual we are cleaning
here); P3-J (MFA gate that contains the residual risk).

---

## P3-I. Public ALB attracts opportunistic CVE scanners  *(P3 — informational)*

**Discovered:** 2026-05-03 (incidental observation during Phase 2
Commit 4 work).

**What's observed:** The public-facing ALB receives constant
opportunistic scanner traffic — PHPUnit RCE attempts, common-CVE
fingerprinting, generic admin-path probes. Backend 401s are holding
correctly; no actual breach surface.

**Why this is P3 (informational, not actionable yet):**
- The 401 layer is the correct first line of defense and is working.
- Adding WAF rules costs money per managed rule group and adds
  latency to every request.
- The traffic is noise, not signal — it's not Luciel-specific
  reconnaissance.

This item is logged so that, when Phase 4 hardening considers
edge-protection posture, we have a reference point for the baseline
scanner-noise volume.

**Fix shape (when prioritized):**
1. Enable AWS WAF on the public ALB.
2. Attach managed rule groups: `AWS-AWSManagedRulesKnownBadInputsRuleSet`,
   `AWS-AWSManagedRulesCommonRuleSet`, optionally
   `AWS-AWSManagedRulesSQLiRuleSet`.
3. Set rules to `Count` mode initially, monitor for false positives
   for 7 days, then switch to `Block`.
4. Document expected post-WAF 4xx rate as a baseline for the alarm
   thresholds in Phase 2 Commit 5.

**Estimated effort:** 1 commit (Terraform/IaC if applicable; manual
console otherwise) + 7-day observation window. ~2 hours operator
time spread over a week.
**Cross-references:** `docs/recaps/2026-05-03-mint-incident.md` §8
(Follow-up C).

---

## P3-M. PostgreSQL client tools (`psql`, `pg_dump`) not on operator PATH  *(P3 — hygiene)*

**Discovered:** 2026-05-04 during Pillar 13 A3 diagnosis. Surfaced
repeatedly across the session whenever a quick DB introspection was
needed; operator had to fall back to `Invoke-RestMethod` against
the admin API surface or to running queries via the FastAPI process,
neither of which is appropriate for low-level schema inspection.

**What's missing:** `psql` and `pg_dump` are not on the operator's
PowerShell `$Env:Path`.

**Why this is P3 (hygiene, not security):**
- No data integrity or compliance posture is affected.
- The workaround (admin-API + FastAPI) is functional, just slower.
- Adding the tools to PATH is a 5-minute one-time fix; defer until
  the next operator-environment refresh.

**Fix shape:**
1. Install PostgreSQL 16 client tools (Postgres.app on macOS or
   `winget install PostgreSQL.PostgreSQL.16` on Windows).
2. Add the binary directory to `$Env:Path` permanently via
   `setx PATH "$Env:Path;C:\Program Files\PostgreSQL\16\bin" /M`.
3. Verify: `psql --version` and `pg_dump --version` resolve.
4. Codify in `docs/runbooks/operator-patterns.md` operator-env section.

**Estimated effort:** 5 minutes operator time + 1 docs commit.
**Cross-references:** drift entry `D-pg-client-tools-not-on-operator-path-2026-05-04`.

---

## P3-N. Pre-flight ritual silently runs degraded with no Celery worker  *(P1)*

**Discovered:** 2026-05-04 during Pillar 13 A3 diagnosis. The 5-block
pre-flight passed cleanly while the underlying Pillar 13 A3 path was
silently broken because the sync fallback in `ChatService` took over
when `settings.memory_extraction_async = False` (the local default).
This is the same shape of risk as `D-celery-worker-not-running-locally-2026-05-02`,
lifted to enforceable status now that we have evidence the
degraded-mode behavior masks real customer-facing bugs.

**What's missing:** the pre-flight does not assert that
`celery -A app.celery_app inspect ping` returns at least one
responder, nor that `settings.memory_extraction_async` matches the
production default (`true` per `backend-td-rev17.json`).

**Why this is P1:**
- Silent integrity loss class. The customer-facing assistant reply
  ("I'll remember that") was a lie for an entire prod-parity gap
  between local sync mode and prod async mode.
- Pillar 11 already exercises the async path under happy conditions,
  but pre-flight is what gates the operator's mental model of "my
  local stack is production-shaped". Today it gives a false green.
- A correct pre-flight would have caught the Pillar 13 A3 silent
  failure on the first attempted repro instead of after
  instrumentation.

**Fix shape:**
1. Add a pre-flight block (or extend an existing block in
   `docs/runbooks/operator-patterns.md`) that:
   - Calls `celery -A app.celery_app inspect ping --timeout 5` and
     fails if the responder count is `0`.
   - Loads `app.config.settings` and asserts
     `settings.memory_extraction_async == True` (matching prod
     `MEMORY_EXTRACTION_ASYNC=true`) OR explicitly logs that the
     operator is running in dev-sync mode and the async path will
     not be exercised.
2. Wire the assertion into `scripts/preflight.ps1` (or equivalent).
3. Codify the failure mode in operator-patterns.md so the operator
   cannot start a verification run with a degraded queue.

**Estimated effort:** ~30 LOC in pre-flight script + ~15 lines in
operator-patterns.md.
**Cross-references:** drift entries
`D-preflight-degraded-without-celery-2026-05-04` (this entry's parent)
and `D-celery-worker-not-running-locally-2026-05-02` (superseded);
recap `docs/recaps/2026-05-04-pillar-13-a3-real-root-cause.md` §6.

---

## P3-O. Extractor failure observability — `extract_and_save` swallows save-time exceptions  *(P1)*

**Discovered:** 2026-05-04 during Pillar 13 A3 diagnosis. The actual
bug (auth-middleware typo binding `actor_user_id = None`) drove a
Postgres D11 NOT NULL violation on every legitimate `MemoryItem`
insert, but the IntegrityError was completely invisible in logs
because `app/services/memory_service.py` `extract_and_save` swallows
any save-time exception with a type-only warning:

```python
# extract_and_save:116-119 (current)
except Exception as exc:
    logger.warning("memory extraction failed: %s", type(exc).__name__)
    return 0
```

The `repr(exc)` would have surfaced the literal Postgres message
`null value in column "actor_user_id" violates not-null constraint`
and reduced the diagnosis from "add forensic instrumentation, push
diag commit, repro, observe, infer" (~2 hours) to a single log read.

**What's missing:**
1. `repr(exc)` in the warning so the actual exception message survives.
2. A drift `AdminAuditLog` row for save-time extractor exceptions so
   compliance has a durable record (the warning log line is not
   audit-grade).
3. Possibly a metric / queue-depth counter for `extractor_save_fail`
   so silent failures show up on dashboards.

**Why this is P1:**
- Same class as the original Pillar 13 bug: silent integrity loss
  driven by an `except Exception` that throws away information the
  customer would care about.
- Today, any future schema-constraint violation, FK violation, or
  pgvector-dimension mismatch will be similarly invisible.
- The fail-open contract (a down memory pipeline must not break the
  chat turn) is correct in principle and must be preserved — but
  fail-open is not the same as fail-silent.

**Fix shape:**
1. Change `extract_and_save:116-119` to:
   ```python
   except Exception as exc:
       logger.warning(
           "memory extraction save failed: type=%s exc_repr=%r "
           "session=%s message_id=%s",
           type(exc).__name__, exc, session_id, message_id,
       )
       try:
           audit_log.write(action="extractor_save_fail", 
                           detail={"exc_type": type(exc).__name__,
                                   "exc_repr": repr(exc),
                                   "session_id": session_id,
                                   "message_id": message_id})
       except Exception:
           pass  # never let the audit failure propagate
       return 0
   ```
2. Add a regression test in `tests/services/test_memory_service.py`
   that drives a deliberate IntegrityError through the path and
   asserts both the warning log shape and the audit-row write.
3. Sweep for sibling `except Exception` blocks across services that
   throw away `exc` and apply the same `repr` discipline.

**Estimated effort:** ~15 LOC change + ~80 LOC test + ~30 minutes
sweep audit.
**Cross-references:** drift entries
`D-extractor-failure-observability-2026-05-04` and
`D-pillar-13-a3-real-root-cause-2026-05-04`; recap
`docs/recaps/2026-05-04-pillar-13-a3-real-root-cause.md` §2.

---

## P3-P. Dev-key storage hygiene — `LUCIEL_PLATFORM_ADMIN_KEY` in operator Notepad  *(P2)*

**Discovered:** 2026-05-04 during Pillar 13 A3 diagnosis. The
platform admin key (a high-privilege secret — it bypasses
tenant scoping for diagnostic operations) lives in the operator's
Notepad rather than a credential manager.

**What's missing:** a credential-manager-backed retrieval pattern
for dev secrets. The current pattern — manually copying from a
Notepad window into a PowerShell environment variable — invites
shell-history leakage, accidental screenshot capture, and
cross-context paste errors.

**Why this is P2:**
- The key is dev-only (does not exist in prod — prod uses
  AssumeRole + KMS-encrypted SSM).
- The blast radius is the operator's own laptop; no customer data
  is reachable from this key.
- But the pattern itself is a habit-shape that, if not corrected,
  will at some point cross over to a prod-grade secret. Better to
  fix the habit while the stakes are small.

**Fix shape:**
1. Move the key into the OS keychain: macOS `security` /
   Windows `cmdkey` / Linux `secret-tool`.
2. Create a thin retrieval wrapper, e.g.
   `scripts/load-dev-secrets.ps1`, that reads from the keychain
   and exports into the current PowerShell session’s env vars.
3. Remove the Notepad copy.
4. Codify in `docs/runbooks/operator-patterns.md`.

**Estimated effort:** ~10 minutes operator time + ~50 LOC of
script + ~10 lines of runbook.
**Cross-references:** drift entry
`D-dev-key-storage-hygiene-2026-05-04`.

---

## P3-Q. `luciel-instance` admin DELETE returns 500 during teardown  *(P2)*

**Discovered:** 2026-05-04 during the post-Commit-A 19/19
verification run. Pillar 10 still passed (zero residue), so the
teardown was effective at the data layer, but one of the constituent
`DELETE /api/v1/admin/luciel-instances/{id}` calls returned HTTP 500
instead of 204.

**What's observed:** the 500 response is incorrect. Either the
endpoint is succeeding-then-erroring (commit + post-commit failure
leaking a 500 to the caller), or it is failing-but-cleanup-cascades
(the row is removed by a downstream cascade and the original DELETE
should have returned 204 from the start). Both shapes need
diagnosis.

**Why this is P2:**
- Non-fatal: Pillar 10 zero-residue assertion held, so the
  customer-facing behavior is correct.
- But returning 500 on a successful delete contradicts the API
  contract and trains operators to ignore 500s, which is the wrong
  habit.

**Fix shape:**
1. Reproduce against a fresh tenant: mint instance, DELETE it,
   capture status + body.
2. Walk the delete handler at `app/api/v1/admin/luciel_instances.py`
   (or wherever the route is mounted) for the failure path.
3. Either fix the underlying error or correct the handler to
   return 204 when the row is in fact gone.
4. Add a regression test in the relevant `tests/api/` module.

**Estimated effort:** ~30 minutes diagnosis + ~10 LOC fix +
~30 LOC regression test.
**Cross-references:** drift entry
`D-luciel-instance-admin-delete-returns-500-2026-05-04`;
verification report
`docs/verification-reports/step28_phase2_postA_sync_2026-05-04.json`
(see Pillar 10 detail — the residue assertion passed but the
teardown anomaly was logged inline).

---

## P3-S. Mint ceremony architectural rework — Pattern N variant for in-VPC execution  *(P0, blocks Phase 2 close)*

**Discovered:** 2026-05-04 ~20:12 UTC during the Commit 4 mint real-run
attempt. Mint script aborted at `psycopg.connect(admin_dsn)` (line 554)
with `ConnectionTimeout: connection timeout expired`. See
`docs/recaps/2026-05-04-mint-architectural-boundary-pause.md` for the
full narrative.

**Root cause:** the Option 3 ceremony's load-bearing assumption
("operator runs the ceremony from their laptop") is incompatible with
the production VPC posture ("RDS is in a private subnet with no public
ingress"). The boundary was never exercised by any prior smoke test
because `mint_worker_db_password_ssm.py --dry-run` returns at line 491
before reaching the DB connect at line 554. Drift entry
`D-option-3-ceremony-cannot-reach-private-rds-from-laptop-2026-05-04`.

**Why this is P0 for Phase 2 close:** Commit 4 (worker DB role swap)
cannot complete until the mint runs successfully. Commits 5-7 don't
strictly depend on Commit 4, but the Phase 2 close gate explicitly
requires `pg_stat_activity` to show zero worker connections as
`luciel_admin`, which means the worker must have rotated to
`luciel_worker` credentials — i.e. Commit 4 must be live.

**Two sub-options for the rework:**

### P3-S.a — Dedicated mint task (recommended)

Create a new ECS Fargate task definition `luciel-mint` with:
- New IAM task role `luciel-mint-task-role` holding the same 5
  statements that `luciel-mint-operator-role` has post-`e1154bd`
  (admin-DSN read + worker-SSM write + KMS via SSM, narrowly scoped).
- The mint operator role still exists for the AssumeRole-to-pass-
  credentials path on the laptop side. The task role is what the
  running task uses for AWS API calls.
- Container image: same as `luciel-migrate` (the backend image
  already has `psycopg`, `boto3`, and the mint script).
- Network: same private subnets as Pattern N. Inherits VPC route to
  RDS — the missing piece in Option 3.
- Single-purpose: not part of the `luciel-migrate:N` family. Audit
  story is unambiguous ("the mint task minted; the migrate task
  migrated").

Helper rewrite: `mint-with-assumed-role.ps1` becomes
`mint-via-fargate-task.ps1` (or v2). Same MFA + AssumeRole prelude
on the laptop, but the body becomes:

```
aws ecs run-task --cluster luciel-cluster --task-definition luciel-mint:1 ...
aws logs tail /ecs/luciel-mint --follow ...
```

Operator sees the mint script's stdout/stderr via CloudWatch Logs
tail. Pattern E discipline preserved — the task's stdout still goes
through the same `_redact_dsn_in_message` path. The script's
pre-flight, outer try/except, and atomicity defenses all still apply.

Estimated effort: 60-90 min for full design + apply + smoke + run.

### P3-S.b — Reuse `luciel-migrate:N` task with command override

Quicker. Use `aws ecs run-task --overrides` to invoke the mint
script inside the existing migrate task. No new task definition,
no new IAM role.

**Audit problem:** the migrate task role wasn't designed for minting
worker credentials. Reusing it creates a "why does the migrate role
have SSM PutParameter on the worker DSN?" question that ages badly
in compliance review. The task role would need new permissions, at
which point we've muddied two ceremonies into one.

Estimated effort: 30-45 min, but creates audit debt.

**Recommendation:** P3-S.a. Multi-tenant SaaS audit posture demands
single-purpose ceremonies. The 30-45 min savings of P3-S.b is not
worth the long-term audit muddiness for a brokerage compliance
audience.

**Smoke test requirement (critical):** whichever sub-option is
chosen, the smoke test MUST include a non-dry-run connection to RDS
before the first real mint. Every prior smoke test skipped this
layer (because dry-run returns before line 554), which is the
specific reason this gap survived to today. See sister entry
`D-mint-script-dry-run-skips-preflight-2026-05-04` for a related
~10 LOC patch that would have caught this earlier.

**Cross-references:** session recap
`docs/recaps/2026-05-04-mint-architectural-boundary-pause.md`;
operator patterns `docs/runbooks/operator-patterns.md` Pattern N;
live IAM policy `infra/iam/luciel-mint-operator-role-permission-policy.json`
(commit `e1154bd`).

---

## P3-R. MFA TOTP echoes in PowerShell terminal during mint ceremony  *(P2)*

**Discovered:** 2026-05-04 ~19:57 UTC during the Commit 4 mint dry-run.

**Symptom:** `scripts/mint-with-assumed-role.ps1` line that calls
`Read-Host` for the MFA code reads the input as a plain string,
which means PowerShell echoes the typed digits visibly in the
terminal (`Enter current MFA 6-digit code: 123351`). The code
appears in the operator's scrollback, in any transcript pasted
elsewhere, and in any window-recording capture.

**Why it's not P0:** TOTP codes are single-use and the 30-second
window expires within seconds of paste. Even if the displayed
code is shoulder-surfed or screenshotted, it cannot be replayed
after the next TOTP rotation. The `aws:MultiFactorAuthPresent`
condition on the mint role's trust policy still prevents reuse.

**Why it's still worth fixing:** defense in depth + good operator
habit. If the same operator pattern is later applied to a non-
TOTP shared secret, the muscle memory of "echo is fine, it's just
MFA" becomes a vulnerability. Better to never echo any prompted
secret regardless of its expiry semantics.

**Recommended fix:** change the helper to use
`Read-Host -AsSecureString` for the MFA prompt and then convert
to plain text only at the moment of `sts:AssumeRole` invocation
via `[Runtime.InteropServices.Marshal]::PtrToStringAuto(
[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))`,
with a `try/finally` that zeros the string immediately after
the AssumeRole call returns. ~10 LOC change in
`scripts/mint-with-assumed-role.ps1`.

**Forward-looking guard:** any future PowerShell helper that
reads a secret via `Read-Host` MUST use `-AsSecureString`. Add
a lint check or a CI grep for `Read-Host` without
`-AsSecureString` in `scripts/*.ps1`.

**Estimated effort:** ~10 LOC + ~5 LOC try/finally + manual
verification on next mint ceremony.

**Cross-references:** observed during the dry-run captured in
this session's transcript; helper script
`scripts/mint-with-assumed-role.ps1` (commit `9e48098`); operator
MFA device `arn:aws:iam::729005488042:mfa/Luciel-MFA` (P3-J).

---

## Sequencing

Updated 2026-05-03 evening after P3-G rescope and P3-J / P3-K addition.
The Commit 4 mint re-run is now blocked on P3-J + P3-K, NOT on P3-G.

**Phase 2 tail (must complete before Phase 2 closes):**

1. ~~**P3-J FIRST.**~~ MFA on `luciel-admin`. P0. ✅ Resolved 2026-05-03
   23:48:11 UTC.
2. ~~**P3-K next.**~~ `luciel-mint-operator-role`. ✅ Resolved 2026-05-04
   00:14:10 UTC.
3. ~~**P3-G** policy diff (`GetParameterHistory`).~~ ✅ Resolved
   2026-05-03 ~20:09 EDT.
4. ~~**P3-H** admin password rotation + log-stream delete.~~ ✅ Resolved
   2026-05-03 23:56:22 UTC.
5. **Commit 4 mint re-run** via the Option 3 ceremony — **NOW UNBLOCKED**.
6. **Commits 5–7** prod-touching: CloudWatch alarms, ECS auto-scaling,
   container healthchecks.
7. **P3-L** SSM parameter history cleanup — **deferred to post-Commit-4**
   per the rationale in the P3-L entry above.

**Phase 3 additions from 2026-05-04 Pillar 13 A3 diagnosis:**

- **P3-O (P1)** — extractor failure observability. Bundle with **P3-A**
  / **P3-B** as part of the audit-emission sweep; same class of
  problem (silent loss of provable signal) and the fix touches
  adjacent code paths.
- **P3-N (P1)** — pre-flight Celery / async-flag gate. Cheap, do
  before the next prod-touching pre-flight run.
- **P3-Q (P2)** — `luciel-instance` admin DELETE 500. Standalone
  diagnosis, no dependencies.
- **P3-M (P3)** and **P3-P (P2)** — operator-environment hygiene.
  Bundle into a single "operator-env refresh" commit at any
  convenient time; not blocking.

**Phase 3 proper (starts after Phase 2 prod-touching commits 4-7
stabilize for ≥ 7 days):**

5. **P3-A first within Phase 3.** Onboarding audit gap is the single
   P0 of Phase 3 audit work. Everything else is P1/P2.
6. **P3-B as a bundle with P3-A.** They share the audit-emission code
   path.
7. **P3-C** alongside the next canonical recap update (cheap,
   doc-only).
8. **P3-D** before any sales motion that markets cross-tenant
   isolation.
9. **P3-E and P3-F** before the first SOC 2 readiness assessment.
10. **P3-I** during Phase 4 edge-hardening (informational, no
    urgency).

**Estimated total:** ~10 commits, ~950 LOC of code + ~350 LOC of
docs, plus operator time for MFA setup, IAM role creation, SSM
rotations, and CloudWatch log cleanup. ~2-3 weeks of focused work,
with P3-J/P3-K/P3-G/P3-H bridging into the tail of Phase 2.

---

## How to update this file

When an item lands: move it to a `## Resolved` section at the bottom
with the resolving commit SHA, date, and a one-line summary of the
fix. Don't delete — the audit-of-the-audit-backlog is itself useful.

When a new gap is discovered during another step's work: add it as
P3-G, P3-H, etc. with the same severity / discovered / what's missing
/ why it matters / fix shape / effort structure used above.
