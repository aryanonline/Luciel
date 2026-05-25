# Arc 9 C7 — Per-Admin Observability Runbook

**Branch:** `arc-9-c7-observability` (PR #76)
**Stack:** `luciel-prod-alarms` (extends, does not replace, the Step 28
alarm pack)
**Region:** `ca-central-1` · **Account:** `729005488042`
**Status:** ⏳ deploy pending — code merged via PR #76, dev validation
listed in §3, prod rollout in §4.

This runbook is the operational layer for the four new alarms
introduced by Arc 9 C7. The doctrine it operationalises:

- **D7.1** — Every grant the ops role exercises emits a CloudWatch
  event. *(realised by the OpsRoleConnect metric filter + velocity
  alarm.)*
- **D7.2** — Every denial by an immutability policy emits a
  High-severity alarm. *(realised by the AuditLogIntegrityBreach
  filter + alarm — single-occurrence pages.)*

Arc 9 C7 does **not** touch the Vision or the canonical ARC9_RUNBOOK
in Drive. This file is repo-resident operational tooling, not a
strategy document.

---

## 0 — What C7 adds, at a glance

| # | Resource | Type | Severity | Routes to |
|---|---|---|---|---|
| 1 | `luciel-audit-log-integrity-breach` | Alarm | **High** | SNS → email · SNS → PagerDuty |
| 2 | `luciel-guc-leak-guard-violation` | Alarm | **High** | SNS → email · SNS → PagerDuty |
| 3 | `luciel-ops-role-connect-velocity` | Alarm | Medium | SNS → email |
| 4 | `luciel-admin-audit-write-velocity` | Alarm | Medium | SNS → email |

Each alarm is fed by a `Logs::MetricFilter` scoped to the backend log
group `/ecs/luciel-backend`. Filters publish counts into the
`Luciel/Backend` namespace.

The two High-severity alarms additionally publish to a second SNS
topic `luciel-prod-pagerduty` whose only subscriber is a PagerDuty
events-v2 HTTPS endpoint. The PD integration URL is **not** in code —
it is resolved at deploy time from SSM Parameter Store via
`{{resolve:ssm:/luciel/prod/pagerduty/integration_url}}`.

---

## 1 — Pre-deploy checklist

Before `aws cloudformation deploy`, all four of these must be true:

- [ ] **Branch merged** to `main` (PR #76).
- [ ] **PagerDuty service** created (or chosen) in your PD org and a
  CloudWatch-integration **Events API v2** integration URL captured.
  The URL has the shape
  `https://events.pagerduty.com/integration/<key>/enqueue`.
- [ ] **SSM parameter** written:
  ```bash
  aws ssm put-parameter \
    --name /luciel/prod/pagerduty/integration_url \
    --type SecureString \
    --value "https://events.pagerduty.com/integration/<KEY>/enqueue" \
    --region ca-central-1
  ```
  Use `--overwrite` if the parameter already exists. The CFN resolver
  reads at stack-update time, so the parameter MUST exist before the
  stack is updated.
- [ ] **Existing alarm pack version** confirmed:
  ```bash
  aws cloudformation describe-stacks \
    --stack-name luciel-prod-alarms \
    --query "Stacks[0].Outputs[?OutputKey=='AlarmsCreated'].OutputValue" \
    --output text
  ```
  Should return `7` pre-C7. Post-deploy it returns `11`.

If you do not have a PagerDuty account yet, you may temporarily write
a placeholder URL into SSM (`https://events.pagerduty.com/integration/placeholder/enqueue`)
to unblock the deploy — the HTTPS subscription will still create but
no events will be delivered. Replace the parameter value and re-run
`update-stack` (no template change required, just a parameter
refresh) once the real key is provisioned.

---

## 2 — Deploy command

```bash
aws cloudformation deploy \
  --stack-name luciel-prod-alarms \
  --template-file cfn/luciel-prod-alarms.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region ca-central-1 \
  --parameter-overrides \
    AlertEmail=alerts@luciel.example \
    PagerDutyServiceKeySsmParam=/luciel/prod/pagerduty/integration_url \
    BackendLogGroupName=/ecs/luciel-backend
```

Deploy is idempotent. The four C7 resources will create on first run
and update in place on subsequent runs.

**Email subscription:** The `AlertEmail` subscriber receives every
alarm transition (both Medium and High). After the first deploy, the
recipient must click the SNS confirmation link mailed by AWS. Until
confirmation, alarms still fire but the email path is silent (the
PagerDuty path is independent and starts working immediately for the
two High-severity alarms).

---

## 3 — Dev validation (BEFORE prod)

Each of the four alarms has a manual trigger that exercises the full
log-line → metric-filter → metric → alarm path in dev. Run these in
order against a dev-account `luciel-prod-alarms` stack first (rename
the stack to `luciel-dev-alarms` in dev, same template).

### 3.1 OpsRoleConnect (Medium)

Emit five connect events in two minutes to clear the velocity
threshold (default = 4 over 5 min):

```bash
# from a dev backend container, with both env vars set
python -c "
import os
os.environ['LUCIEL_OPS_DB_URL'] = os.environ['LUCIEL_OPS_DB_URL_DEV']
os.environ.setdefault('AUDIT_LOG_IMMUTABILITY_ENABLED', 'true')
from app.db.session import get_ops_db_session
for _ in range(5):
    with get_ops_db_session() as s: s.execute('SELECT 1')
"
```

Watch the alarm state transition in `aws cloudwatch describe-alarms
--alarm-names luciel-ops-role-connect-velocity`. Expected: `ALARM`
within ~6 minutes (5-min period + evaluation lag).

### 3.2 AdminAuditWriteVelocity (Medium)

Trigger any admin endpoint that writes audit rows in a loop (e.g.
`POST /api/v1/admin/users/{id}/disable` 25× in 5 minutes if the
default threshold is 20). Alarm clears after one period of silence.

### 3.3 GUCLeakGuardViolation (High → pages)

This alarm should be **structurally impossible** to hit in normal
operation. To validate the wiring, inject the literal phrase via a
manual log emission in a dev container shell:

```bash
aws logs put-log-events \
  --log-group-name /ecs/luciel-backend-dev \
  --log-stream-name <pick-any-active-stream> \
  --log-events "timestamp=$(($(date +%s%N) / 1000000)),message=OpsSessionLocal must not carry app.admin_id"
```

Expected: Medium-state metric appears within 1 minute, alarm
transitions to ALARM within 2 minutes, PD incident opens. **Close
the PD incident immediately** after confirming receipt — this is a
drill.

### 3.4 AuditLogIntegrityBreach (High → pages)

The C6.2 RESTRICTIVE policies make this physically impossible to
trigger via any application code path. Validate the wiring by
manually injecting the Postgres denial message:

```bash
aws logs put-log-events \
  --log-group-name /ecs/luciel-backend-dev \
  --log-stream-name <pick-any-active-stream> \
  --log-events 'timestamp='$(($(date +%s%N) / 1000000))',message=new row violates row-level security policy for table "admin_audit_logs"'
```

Expected: alarm to ALARM within 2 minutes, PD page. Close the drill
incident.

---

## 4 — Prod rollout

Prod deploy is a CloudFormation update only — no ECS rolling deploy
is required for C7 because the application change (the connect
listener in `app/db/session.py`) is flag-gated on
`audit_log_immutability_enabled`, which remains **False** in prod
until C9.

Order of operations:

1. **Write the real PD integration URL to SSM** (overwrites the dev
   placeholder if one was used):
   ```bash
   aws ssm put-parameter \
     --name /luciel/prod/pagerduty/integration_url \
     --type SecureString \
     --overwrite \
     --value "https://events.pagerduty.com/integration/<PROD_KEY>/enqueue" \
     --region ca-central-1
   ```
2. **Deploy the stack** (same command as §2, against the prod
   account).
3. **Confirm the email subscription** (one-time per email address).
4. **Smoke**: re-run §3.4 (AuditLogIntegrityBreach manual log
   injection) once against the prod log group, confirm a PD page
   lands, close the drill. Do **not** run §3.1 in prod — it would
   require connecting the ops role to prod five times, which itself
   creates audit-log noise.
5. **Update the ECS task definition** to set
   `AUDIT_LOG_IMMUTABILITY_ENABLED=true` in env. This step is
   deferred to C9 — leave the flag False at C7 close.

---

## 5 — On-call triage

When one of the four alarms pages, follow this playbook before
escalating.

### 5.1 `luciel-audit-log-integrity-breach` (High)

**What it means:** Something tried to UPDATE or DELETE
`admin_audit_logs` and the C6.2 RESTRICTIVE policy denied it. The
policy did its job — this page exists so we learn about the attempt
in real time regardless of actor.

**First action:** Query the backend log group for the offending
record. The CFN filter pattern matches both denial-message variants:

```
fields @timestamp, @message, @logStream
| filter @message like /row violates row-level security policy for table "admin_audit_logs"/
| sort @timestamp desc
| limit 50
```

The matching log lines carry the SQL statement and bind parameters
that caused the denial — that gives you the actor (app role vs ops
role vs superuser), the target row, and the call stack.

**Escalate to:** Aryan (founder) — every hit on this alarm is
treated as a security incident until proven a drill.

### 5.2 `luciel-guc-leak-guard-violation` (High)

**What it means:** The C6.4 in-process guard ("OpsSessionLocal must
not carry app.admin_id") fired. This indicates a code-level bug
where a developer reused an ops session in a context that had a
tenant GUC set in the in-process ContextVar.

**First action:** Same Logs Insights query, swap the filter to
`/OpsSessionLocal must not carry app.admin_id/`. The traceback in the
preceding log lines points at the call site.

**Escalate to:** Aryan. Holds the release until the offending PR is
reverted.

### 5.3 `luciel-ops-role-connect-velocity` (Medium)

**What it means:** The luciel_ops role connected more than
`OpsRoleConnectVelocityThreshold` times in a 5-minute window.
Either a forensic operation is running (expected, expected logged
elsewhere) or someone is brute-forcing ops connections.

**First action:** Query for `arc9.c7.ops_role_connect` over the
last 30 minutes and check the call sites in the surrounding stack
traces. Cross-reference with the change ticket queue — a scheduled
forensic export is the most common benign cause.

**Escalate if:** No ticketed forensic operation explains the spike.

### 5.4 `luciel-admin-audit-write-velocity` (Medium)

**What it means:** More than `AdminAuditWriteVelocityThreshold`
audit rows landed in a 5-minute window. Either a bulk operation is
running (expected) or an admin endpoint is being abused.

**First action:** Same query, filter on `admin_audit_logs chain row=`.
The hashed row metadata includes the admin_id — look for skew toward
a single admin.

**Escalate if:** The volume is concentrated on a single admin
account that does not match a scheduled bulk operation.

---

## 6 — Parameter tuning

The two velocity thresholds are exposed as stack parameters so we can
ratchet them without code changes. Defaults are conservative:

| Parameter | Default | Where to ratchet |
|---|---|---|
| `OpsRoleConnectVelocityThreshold` | 4 / 5min | Raise after we have a baseline; expect 1-2 connects/day in prod under normal load. |
| `AdminAuditWriteVelocityThreshold` | 20 / 5min | Likely too low once Pro tier ships — revisit after first Pro customer onboard. |

Ratchet via `aws cloudformation update-stack --parameters` — no
template change needed.

---

## 7 — Rollback

The C7 resources are additive. To roll back without deleting the
existing Step 28 alarms:

1. Revert PR #76 on `main`.
2. `aws cloudformation deploy` the reverted template — CFN computes
   the delta and deletes only the 4 new MetricFilters + 4 new Alarms
   + PagerDutyTopic + PagerDutySubscription.
3. The SSM parameter `/luciel/prod/pagerduty/integration_url` is
   safe to leave in place; it is not referenced by anything else.

The application change in `app/db/session.py` is flag-gated, so
reverting CFN without reverting the code is also safe — the listener
will fire but the log lines will not be captured by any metric
filter.

---

## 8 — Contract: what NOT to change

The literal string
`arc9.c7.ops_role_connect role=luciel_ops event=connect`
appears in three places and they MUST stay in sync:

| Site | Form |
|---|---|
| `app/db/session.py` listener | full string in `logger.info(...)` |
| `cfn/luciel-prod-alarms.yaml` | substring `"arc9.c7.ops_role_connect"` as FilterPattern |
| `tests/db/test_c7_1_ops_connect_log_format.py` | both forms asserted |

The test is the lockbox. Any PR that touches one of the three sites
without updating the others will fail the C7.1 contract test in CI.
That is by design — if these drift in prod, the alarm goes silently
dark and we lose the D7.1 guarantee.

---

## 9 — Decision log (C7)

| Decision | Value | Rationale |
|---|---|---|
| Paging channel | SNS → PagerDuty for High; SNS → email for Medium | Two-tier severity matches the two-tier doctrine (D7.2 pages, D7.1 logs). |
| PD URL storage | SSM SecureString resolved at stack-update | Keeps secret out of repo + IAC parameter-overrides logs. |
| Velocity defaults | 4 (ops connect) / 20 (audit write) per 5 min | Conservative starting points; tune in §6 after baseline. |
| Flag gating of connect log | `audit_log_immutability_enabled` | Avoids dev/CI noise that would skew velocity baseline. |
| Single stack vs new stack | Extend `luciel-prod-alarms` | Keeps the alarm pack a single artifact for `describe-stacks` audits. |

---

## 10 — References

- PR #76 — Arc 9 C7 CloudWatch + PD observability
- `cfn/luciel-prod-alarms.yaml` — the IAC artifact extended by this
  arc
- `tests/db/test_c7_1_ops_connect_log_format.py` — the contract
  test
- `docs/runbooks/step-28-phase-2-deploy.md` — the Step 28 alarm pack
  that C7 extends
- ARC9_RUNBOOK.pdf (Drive, file ID `1PmZjnmm2IGapdHcvOljZ1C-qyiORWNWX`)
  — Arc 9 operational anchor (immutable). C7 is the realisation of
  the C7 row in that runbook's sub-arc table.
- ARC9_C7_ENVELOPE_CLOSE.pdf (Drive, to be authored after PR #76 +
  PR #77 merge) — per-arc execution record.
