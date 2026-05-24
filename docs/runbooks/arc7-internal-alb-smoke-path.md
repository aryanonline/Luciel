# Arc 7 C10 — Internal ALB Smoke Path Runbook (WU-4)

**Status:** RESOLUTION-PATH DOCUMENT (not yet executed). Arc 7 envelope produces this runbook as the operator-axis closure plan for `D-no-internal-smoke-path-for-direct-alb-2026-05-22`. Actual implementation deferred to Arc 8 observability sub-arc, paired naturally with the planned `/ready` endpoint work.

**Anchor:** `docs/DRIFTS.md` `D-no-internal-smoke-path-for-direct-alb-2026-05-22` §3 OPEN; Arc 7 WU-4 (this commit).

**Not a canonical document.** Posture B operational satellite. When this runbook and the three canonical documents (`CANONICAL_RECAP.md`, `ARCHITECTURE.md`, `DRIFTS.md`) disagree on any fact, **canon wins automatically.** Update the satellite to match.

---

## 1. Problem statement

Today's deploy-gate after an ECS rolling update relies on three signals, none of which prove "the application returned a 200 to a real HTTP request":

1. ECS task-state — `runningCount == desiredCount`, deployment `COMPLETED`.
2. Target-group health-check — registered targets `Healthy`.
3. Customer-domain probe — `curl https://www.vantagemind.ai/health` returns `200 OK`.

(1) and (2) are necessary but not sufficient — they prove the container is up and answers the TG health check, but the TG health check itself is shallow (HTTP 200 on `/health` from inside the VPC). (3) is end-to-end but depends on the entire Amplify DNS + custom-domain TLS chain + apex→www rewrite, so a failure mode in any of those layers gets attributed to "the deploy is broken" when in fact the application is healthy and the customer-domain layer is the broken link.

We need a fourth signal: an HTTP probe that hits the ALB target group directly (bypassing Amplify) with a trusted TLS chain, fast enough to gate a rolling-deploy completion event.

## 2. Constraints

- ALB DNS name is the raw `luciel-targets-XXXXX.ca-central-1.elb.amazonaws.com` form. AWS does not issue a publicly-trusted cert for this hostname; the ACM cert attached to the ALB listener is scoped to `vantagemind.ai` / `*.vantagemind.ai`. Probing the ALB DNS name directly fails the cert SAN match.
- Sandbox agent IAM (`luciel-sandbox-agent`) is denied `elasticloadbalancing:Describe*` and all elbv2 actions — operator-side configuration only.
- The deploy-gate must be cheap enough to run on every rolling deploy (≤30s, ≤$0.001/probe).
- The probe must not require an inbound rule on any production SG that widens the customer-traffic blast radius.

## 3. Resolution options

### Option A — Internal hostname + second ACM cert (the DRIFTS resolution-path Option a)

Create a private Route 53 hosted zone `luciel.internal` (or reuse if one exists), add an `A` ALIAS record `alb.luciel.internal → luciel-targets ALB`, request an ACM cert in `ca-central-1` for `alb.luciel.internal`, attach it as a second cert on the ALB :443 listener via SNI. Then any in-VPC probe hits `https://alb.luciel.internal/health` with a trusted chain.

**Cost:** Free (ACM private cert is free; Route 53 private zone is $0.50/mo).
**Pro:** Symmetric with the customer-facing path; one extra cert, no extra compute.
**Con:** Requires Route 53 private zone resolution from the probe origin — works for in-cluster probes, does not work for on-laptop manual probes.

### Option B — In-cluster Fargate probe (the DRIFTS resolution-path Option b — preferred)

A tiny scheduled ECS task (Fargate, 0.25 vCPU / 0.5 GB) on the same VPC + subnets that runs a 30-line shell:

```bash
#!/bin/sh
set -e
HEALTH_URL="${HEALTH_URL:-https://www.vantagemind.ai/health}"
VERSION_URL="${VERSION_URL:-https://www.vantagemind.ai/version}"
EXPECTED_SHA="${EXPECTED_SHA:?must be set to the just-deployed git_sha}"

health=$(curl -sf -w '%{http_code}' -o /tmp/h "$HEALTH_URL")
version=$(curl -sf "$VERSION_URL" | jq -r .git_sha)

if [ "$health" != "200" ]; then
  aws cloudwatch put-metric-data --namespace Luciel/DeployGate \
    --metric-name HealthProbeFailure --value 1
  exit 1
fi
if [ "$version" != "$EXPECTED_SHA" ]; then
  aws cloudwatch put-metric-data --namespace Luciel/DeployGate \
    --metric-name VersionMismatch --value 1
  exit 2
fi
aws cloudwatch put-metric-data --namespace Luciel/DeployGate \
  --metric-name DeployGatePass --value 1
```

**How it probes the ALB directly:** the task is registered in the same VPC as the ALB; the ALB's customer-facing :443 listener accepts the customer hostname via SNI; the task hits `https://www.vantagemind.ai/health` resolving via the public DNS but the TCP socket terminates on the same internal target group. This exercises the ALB target group and the application together without depending on Amplify (because `/health` is served by the ECS backend, not by Amplify).

**Cost:** ~$0.0001 per invocation (≤5s of 0.25 vCPU Fargate). Run as an ECS Run-Task fired by the deploy script.
**Pro:** Exercises the same path customer traffic exercises, no new ACM cert, no Route 53 work; the probe origin is in-VPC so VPC routing is also exercised.
**Con:** Slightly slower than a localhost probe; requires a one-time task-def + IAM role for the probe.

### Decision (Arc 7 closing-record posture)

**Defer the implementation choice to Arc 8.** Both options are valid. The doctrine point Arc 7 records is: **the existing 3-signal gate is not sufficient and a 4th signal must be added before declaring deploy-gate maturity.** Arc 8 picks A or B at execution time based on the `/ready` endpoint design (if `/ready` lands as a separate route from `/health` to expose DB+Redis+Stripe-API readiness, the in-cluster Fargate probe of Option B is the cleaner consumer).

## 4. Interim posture (until Arc 8 implementation lands)

The deploy-gate per Arc 7 close uses this explicit checklist:

1. ECS `describe-services` shows `deployments[*].rolloutState=COMPLETED` for both `luciel-backend-service` and `luciel-worker-service`.
2. ECS `describe-tasks` shows both tasks `lastStatus=RUNNING` and the application container `healthStatus=HEALTHY`.
3. `curl -sf https://www.vantagemind.ai/health` returns `200`.
4. `curl -sf https://www.vantagemind.ai/version | jq -r .git_sha` matches the just-deployed `BUILD_GIT_SHA`.

A failure of (3) or (4) without a failure of (1) or (2) means the application is up but the Amplify/DNS/TLS chain is broken — investigate Amplify first, not the deploy.

A failure of (4) with a success of (3) means an old task is still serving and the rolling deploy is mid-flight — wait, then re-probe.

A success of all four is the deploy-gate-pass condition under the interim posture.

## 5. Doctrine note

The internal ALB smoke path is an **observability hygiene** drift, not a **security** drift. It does not gate Arc 7 close because:

- Arc 7's scope is tier-shape symmetry + abuse boundaries (C4 rate-limit, C6 signup IP gate). The deploy-gate observability gap predates Arc 7 and is independent of the Arc 7 vision.
- The 3-signal interim gate has shipped six prod deploys clean across Arc 5, Arc 6, and Arc 7 with no false-positives or false-negatives traced to the missing 4th signal.
- Arc 8 is the observability arc; this drift is in its natural envelope.

## 6. Closure evidence (when Arc 8 lands this)

- Documented Fargate task-def (or new ACM cert + Route 53 record).
- CloudWatch metric `Luciel/DeployGate/DeployGatePass` pinging on every deploy with a value timestamp matching the deploy event.
- Updated deploy runbook citing this as gate signal #4 with explicit pass/fail thresholds.
- Strikethrough on `D-no-internal-smoke-path-for-direct-alb-2026-05-22` heading in `docs/DRIFTS.md`.

---

**Cross-refs:** `D-no-internal-smoke-path-for-direct-alb-2026-05-22` (the parent drift); `D-health-endpoint-shallow-no-db-readiness-check-2026-05-22` (natural-fit `/ready` sibling); `docs/runbooks/operator-patterns.md` (deploy-gate checklist living document); CANONICAL_RECAP §17 Arc 7 entry (this commit's deploy gate evidence).
