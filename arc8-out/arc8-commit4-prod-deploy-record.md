# Arc 8 — Commit 4 Prod Deploy Record (WU-4 Observability)

**Status:** ✅ TASK-DEF REGISTERED & SCRIPT-VALIDATED (RunTask execution deferred to partner-Console packet — sandbox-agent lacks `ecs:RunTask`)

**Date:** 2026-05-24 (Sunday)
**Drift closed (code-side):** `D-no-internal-smoke-path-for-direct-alb-2026-05-22`
**Resolution path:** Option B from `docs/runbooks/arc7-internal-alb-smoke-path.md` (Fargate one-shot in-cluster probe)

---

## 1. What landed

A **Fargate one-shot ECS task-definition** (`luciel-smoke-probe:1`) that, when invoked via `aws ecs run-task`, spins up a 0.25 vCPU / 0.5 GB Alpine container in the same VPC + subnets as the backend service and curls the deploy-gate triplet:

- `https://api.vantagemind.ai/health`
- `https://api.vantagemind.ai/ready` (NEW — Arc 8 C1 endpoint, gives DB+Redis readiness)
- `https://api.vantagemind.ai/api/v1/version`

Exits **0 only if all three return 200**; if `EXPECTED_SHA` env var is set, also verifies `/api/v1/version`'s `git_sha` field matches (covers the rolling-deploy mid-flight false-positive case described in C10 runbook §4).

Every probe result printed to stdout → CloudWatch `/ecs/luciel-backend` log group with stream prefix `smoke-probe/...` (reused existing log group; sandbox-agent IAM denies `logs:CreateLogGroup` for a dedicated one).

## 2. Resources created

| Resource | Identifier | Notes |
|---|---|---|
| ECS task-def | `arn:aws:ecs:ca-central-1:729005488042:task-definition/luciel-smoke-probe:1` | Family `luciel-smoke-probe`, rev 1 |
| Execution role | `arn:aws:iam::729005488042:role/luciel-ecs-execution-role` | Reused — image pull + log delivery |
| Task role | `arn:aws:iam::729005488042:role/luciel-ecs-web-role` | Reused — no extra perms needed for v1 (no CloudWatch put-metric-data) |
| Log group | `/ecs/luciel-backend` (stream prefix `smoke-probe`) | Reused — partner-Console packet may add dedicated `/ecs/luciel-smoke-probe` group |
| Image | `public.ecr.aws/docker/library/alpine:3.20` | Public ECR, no auth, no build, ~7 MB |
| CPU/Memory | 256 / 512 | Fargate smallest supported size |
| Network mode | `awsvpc` | Required for Fargate |

## 3. Resources NOT created (deferred / partner-Console / future arcs)

- **EventBridge schedule** — left to operator; v1 is on-demand `RunTask` only (matches C10 design intent: "Run as an ECS Run-Task fired by the deploy script")
- **CloudWatch metric `Luciel/DeployGate/DeployGatePass`** — not pushed in v1; exit code + log lines suffice for the operator-axis gate. Adding metric publish requires task-role `cloudwatch:PutMetricData` permission; partner-Console packet will surface this as an optional upgrade.
- **Dedicated log group `/ecs/luciel-smoke-probe`** — sandbox-agent denied `logs:CreateLogGroup`; reused `/ecs/luciel-backend` instead. Cosmetic, not blocking.

## 4. Files committed

- `arc8-out/arc8-c4-smoke-probe-taskdef.json` — final task-def input
- `arc8-out/arc8-commit4-prod-deploy-record.md` — this file

## 5. Validation

### 5a. Task-def registration

```
$ aws ecs register-task-definition --cli-input-json file://arc8-out/arc8-c4-smoke-probe-taskdef.json
{
  "family": "luciel-smoke-probe",
  "rev": 1,
  "arn": "arn:aws:ecs:ca-central-1:729005488042:task-definition/luciel-smoke-probe:1",
  "cpu": "256",
  "mem": "512"
}
```

✅ Registered cleanly.

### 5b. Script logic validated against live prod (run locally, identical to container entrypoint)

```
[smoke-probe] target=https://api.vantagemind.ai expected_sha=unset
[smoke-probe] /health -> HTTP 200: {"status":"ok","service":"Luciel Backend"}
[smoke-probe] /ready -> HTTP 200: {"status":"ready","checks":{"db":"ok","redis":"ok"}}
[smoke-probe] /api/v1/version -> HTTP 200: {"app":"Luciel Backend","version":"0.1.0","git_sha":"unknown","status":"ok"}
[smoke-probe] ALL PROBES PASSED
```

Exit 0. ✅

### 5c. Failure path validated (EXPECTED_SHA=deadbeef mismatch)

```
[smoke-probe] FAIL: version git_sha=unknown != expected=deadbeef
FINAL_FAILED=1
```

Exit 1. ✅

### 5d. RunTask execution

❌ Sandbox-agent IAM denies `ecs:RunTask`. **Will be invoked from partner-Console** as part of C5 packet:

```
aws ecs run-task \
  --cluster luciel-cluster \
  --launch-type FARGATE \
  --task-definition luciel-smoke-probe:1 \
  --network-configuration 'awsvpcConfiguration={subnets=[subnet-0e54df62d1a4463bc,subnet-0e95d953fd553cbd1],securityGroups=[sg-0f2e317f987925601],assignPublicIp=ENABLED}' \
  --overrides '{"containerOverrides":[{"name":"smoke-probe","environment":[{"name":"EXPECTED_SHA","value":"<just-deployed-sha>"}]}]}' \
  --started-by 'deploy-gate'
```

Console alternative: ECS → clusters → luciel-cluster → Tasks → Run new task → Family `luciel-smoke-probe`, rev 1, override env `EXPECTED_SHA`.

After invocation, observe CloudWatch log group `/ecs/luciel-backend`, stream `smoke-probe/smoke-probe/<task-id>`; exit code visible on the ECS Tasks page (Stop reason → essential container exited).

## 6. Drift closure evidence

- `D-no-internal-smoke-path-for-direct-alb-2026-05-22`: code-side resolved by `luciel-smoke-probe:1` task-def. Closure stanza will be appended to `docs/DRIFTS.md` in C7 (envelope close) alongside other Arc 8 strikethroughs.
- C10 runbook §6 evidence rows:
  - ✅ Documented Fargate task-def (this file + the JSON)
  - ⚠️ CloudWatch metric `Luciel/DeployGate/DeployGatePass` — DEFERRED (v2 — needs task-role IAM update)
  - ✅ Updated deploy runbook (this file is gate signal #4 reference)
  - ⏳ Strikethrough on drift heading — C7

## 7. Sandbox boundaries hit

- `ecs:TagResource` denied — removed `tags:[...]` from task-def, registered successfully untagged.
- `ecs:RunTask` denied — execution deferred to partner-Console (C5 packet item).
- `logs:CreateLogGroup` + `logs:PutRetentionPolicy` denied — reused existing `/ecs/luciel-backend` log group.
- `iam:List*RolePolicies` denied — could not introspect role policies (low impact, roles are known-good from Arc 5).

## 8. Operator handoff snippet (for C5 partner-Console packet)

> **C5 §E (NEW): Trigger the deploy-gate smoke probe**
>
> After every prod backend deploy:
>
> ```bash
> aws ecs run-task --cluster luciel-cluster --launch-type FARGATE \
>   --task-definition luciel-smoke-probe:1 \
>   --network-configuration 'awsvpcConfiguration={subnets=[subnet-0e54df62d1a4463bc,subnet-0e95d953fd553cbd1],securityGroups=[sg-0f2e317f987925601],assignPublicIp=ENABLED}' \
>   --overrides '{"containerOverrides":[{"name":"smoke-probe","environment":[{"name":"EXPECTED_SHA","value":"<deployed-sha>"}]}]}' \
>   --started-by 'deploy-gate' --query 'tasks[0].taskArn' --output text
> ```
>
> Watch ECS Tasks page; exit 0 = deploy-gate signal #4 passed. Exit 1 = investigate per CloudWatch log lines.

---

**Commit:** code-side closure of `D-no-internal-smoke-path-for-direct-alb-2026-05-22`. Operator invocation paired into C5 partner-Console packet.
