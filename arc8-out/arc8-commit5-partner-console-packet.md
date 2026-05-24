# Arc 8 — Commit 5 Partner-Console Packet (WU-5)

**Purpose:** Single, copy-paste-ready operator runbook for the partner's next AWS Console session. Polishes the Arc 7 C9 ops runbook into the final Arc-8 packet: carries forward §A-D (still open, unchanged) and adds §E (NEW — Arc 8 C4 smoke-probe trigger) and §F (optional v2 upgrade).

**Doctrine:** Sandbox-agent IAM cannot perform any action in this packet. Each must be executed by the partner's IAM identity (root or admin).

---

## Status Table

| § | Surface | Drift | Action shape | Effort | Priority |
|---|---|---|---|---|---|
| A | Amplify apex SPA-rewrite | `D-amplify-apex-spa-rewrite-deep-path-404-2026-05-20` | Amplify Console → add 301 rule | 2 min | High (customer-visible 404 on apex deep links) |
| B | SES sandbox exit (case `177948223100786`) | `D-ses-sandbox-exit-pending-2026-05-22` | Wait for AWS Support approval → verify | 30 sec (after AWS replies) | High (blocks non-allowlisted email delivery) |
| C | SES reply-to monitored inbox | `D-ses-reply-to-monitored-inbox-not-confirmed-2026-05-22` | Pick path (a/b/c) → wire mailbox → verify | 5-30 min (depends on path) | Medium (deferred to dedicated mailbox-arc) |
| D | Orphan SSM `floor_annual` param | `D-arc7-ssm-orphan-floor-annual-pending-console-delete-2026-05-24` | SSM Console → Delete | 30 sec | Low (cosmetic — no behavior leak) |
| **E** | **Arc 8 C4 smoke-probe RunTask** | **`D-no-internal-smoke-path-for-direct-alb-2026-05-22`** | **`aws ecs run-task` (CLI or Console)** | **1 min per deploy** | **High (deploy-gate signal #4 — use on every prod deploy)** |
| F | (Optional v2) CloudWatch metric for deploy-gate | n/a (improvement) | IAM policy add to `luciel-ecs-web-role` | 2 min | Low (nice-to-have observability upgrade) |

---

## §A — Amplify apex SPA-rewrite deep-path fix

**Drift:** `D-amplify-apex-spa-rewrite-deep-path-404-2026-05-20`
**Surface:** `vantagemind.ai/<deep-path>` returns 404; `www.vantagemind.ai/<deep-path>` returns 200.
**App:** `d1xf2f9605mosw` (VantageMind site).

### Verify drift still real

```bash
curl -sSI https://vantagemind.ai/pricing
curl -sSI https://www.vantagemind.ai/pricing
```

Pre-fix: apex 404, www 200. If apex already returns `301` with `location: https://www.vantagemind.ai/pricing`, skip to verification — fix has landed already.

### Fix (Amplify Console)

1. AWS Console → Amplify → Apps → **`d1xf2f9605mosw`**
2. Sidebar → **Hosting → Rewrites and redirects**
3. **Add rule** with:
   - Source: `https://vantagemind.ai/<*>`
   - Target: `https://www.vantagemind.ai/<*>`
   - Type: `301 (Permanent redirect)`
   - Country code: blank
4. **Save** — applies immediately (edge config, no rebuild).
5. **Rule ordering:** this 301 must sit AFTER explicit asset-path rules (so `/assets/*.js` doesn't redirect) and BEFORE the SPA-fallback (`/<*>` → `/index.html` 200).

### Verify fix

```bash
curl -sSI https://vantagemind.ai/pricing
# Expect: HTTP/2 301, location: https://www.vantagemind.ai/pricing

curl -sSL https://vantagemind.ai/pricing -o /dev/null -w "final-status=%{http_code} final-url=%{url_effective}\n"
# Expect: final-status=200 final-url=https://www.vantagemind.ai/pricing
```

After verification, ping agent → strikethrough drift in `docs/DRIFTS.md`.

---

## §B — SES sandbox exit (case `177948223100786`)

**Drift:** `D-ses-sandbox-exit-pending-2026-05-22`
**Status:** SUBMITTED to AWS Support 2026-05-22 ~16:37 EDT
**Account:** `729005488042` (`ca-central-1`)

### Check approval

1. AWS Console → Support → **Your support cases**
2. Find case **`177948223100786`** ("SES: Production Access")
3. If **Resolved** with AWS approval → run verification. If **In progress** / **Pending customer action** → wait.

### Verification (partner CLI, partner creds — sandbox-agent lacks `ses:GetAccount`)

```bash
aws sesv2 get-account --region ca-central-1 --query "ProductionAccessEnabled"
# Expect: true
```

### Post-approval smoke

```bash
aws ses send-email \
  --region ca-central-1 \
  --from notifications@vantagemind.ai \
  --to <a-non-allowlisted-test-address> \
  --subject "Arc 8 C5 SES production access smoke" \
  --text "If you receive this, SES sandbox exit is live." \
  --configuration-set-name luciel-default
# Expect: MessageId returned, no AccessDeniedException
```

After smoke, ping agent → closure record + drift strikethrough.

---

## §C — SES reply-to monitored inbox

**Drift:** `D-ses-reply-to-monitored-inbox-not-confirmed-2026-05-22`
**Code leg LIVE** (`ReplyToAddresses=["support@vantagemind.ai"]` on every send_email).
What remains: confirm the mailbox actually delivers to a human-monitored inbox.

### Three mailbox paths

| Path | Cost | Effort | Risk | Notes |
|---|---|---|---|---|
| **(a) GoDaddy → Cloudflare + Email Routing** | Free | High (touches `api.vantagemind.ai`, `www.vantagemind.ai`, SES DKIM CNAMEs, Amplify CNAME) | High during cutover | Doctrine-strongest long-term |
| **(b) GoDaddy-native email forwarding** | ~$0 | ~5 min | Reliability known flaky | Fastest, GoDaddy-dependent |
| **(c) Improvmx / Forwardemail.net forwarder** | Free tier | ~15 min (one MX edit on GoDaddy) | Adds vendor dependency | Cleanest near-term — one DNS row changes |

Partner's 2026-05-22 ~21:40 EDT decision: defer to dedicated mailbox-arc rather than stack DNS risk against in-flight product surfaces. **Arc 8 does NOT close this drift** — outside the Arc 8 hardening envelope.

### Verification once wired

Partner sends a test transactional email via prod, replies from another mailbox, confirms reply arrives at `support@vantagemind.ai`. Evidence: screenshot or message-id chain.

---

## §D — Orphan SSM `floor_annual` parameter

**Drift:** `D-arc7-ssm-orphan-floor-annual-pending-console-delete-2026-05-24`
Sandbox lacks `ssm:DeleteParameter`. Partner deletes via console:

1. AWS Console → Systems Manager → Parameter Store
2. Filter on name = `/luciel/production/stripe_price_enterprise_floor_annual`
3. Select row → **Delete** → confirm

After deletion, ping agent → drift closure.

---

## §E — Arc 8 C4 smoke-probe RunTask  *(NEW — primary Arc 8 add)*

**Drift:** `D-no-internal-smoke-path-for-direct-alb-2026-05-22`
**Code leg LIVE** — `luciel-smoke-probe:1` task-def registered on prod ECS cluster.
What remains: invoke `RunTask` after each deploy (sandbox-agent denied `ecs:RunTask`, so this becomes a per-deploy partner step OR a one-time IAM unblock — see §E.2).

### §E.1 — One-shot invocation (after every prod backend deploy)

**Via CLI (recommended — single command):**

```bash
DEPLOYED_SHA="<the-7-char-sha-just-deployed>"   # e.g. ebab581

aws ecs run-task --cluster luciel-cluster --launch-type FARGATE \
  --task-definition luciel-smoke-probe:1 \
  --network-configuration 'awsvpcConfiguration={subnets=[subnet-0e54df62d1a4463bc,subnet-0e95d953fd553cbd1],securityGroups=[sg-0f2e317f987925601],assignPublicIp=ENABLED}' \
  --overrides "{\"containerOverrides\":[{\"name\":\"smoke-probe\",\"environment\":[{\"name\":\"EXPECTED_SHA\",\"value\":\"${DEPLOYED_SHA}\"}]}]}" \
  --started-by 'deploy-gate' \
  --query 'tasks[0].taskArn' --output text
```

Returns a task ARN like `arn:aws:ecs:ca-central-1:729005488042:task/luciel-cluster/<task-id>`.

**Watch the task:**

```bash
TASK_ID="<task-id-from-above>"
aws ecs describe-tasks --cluster luciel-cluster --tasks $TASK_ID \
  --query 'tasks[0].{status:lastStatus,exit:containers[0].exitCode,stop:stoppedReason}' --output json
# Re-run every 10s until status=STOPPED. Expect exit=0.
```

**Watch the logs (CloudWatch):**

```bash
aws logs tail /ecs/luciel-backend --since 5m --filter-pattern 'smoke-probe' --format short
```

Expected on PASS:
```
[smoke-probe] target=https://api.vantagemind.ai expected_sha=<deployed-sha>
[smoke-probe] /health -> HTTP 200: {"status":"ok","service":"Luciel Backend"}
[smoke-probe] /ready -> HTTP 200: {"status":"ready","checks":{"db":"ok","redis":"ok"}}
[smoke-probe] /api/v1/version -> HTTP 200: {"app":"Luciel Backend",...,"git_sha":"<deployed-sha>",...}
[smoke-probe] PASS: version git_sha matches expected <deployed-sha>
[smoke-probe] ALL PROBES PASSED
```

Exit 0 = deploy-gate signal #4 PASS.
Exit 1 = some probe failed OR git_sha mismatch — check logs, investigate.

**Via Console (alternative):**

1. ECS → Clusters → `luciel-cluster` → **Tasks** tab → **Run new task**
2. Launch type: `FARGATE`
3. Task definition: family `luciel-smoke-probe`, revision `1`
4. VPC: same as backend (auto-suggested) — subnets `subnet-0e54df62d1a4463bc`, `subnet-0e95d953fd553cbd1`; SG `sg-0f2e317f987925601`; Assign public IP: ENABLED
5. **Container overrides → smoke-probe** → Environment variables → add `EXPECTED_SHA=<deployed-sha>`
6. **Run task**
7. Watch the task page until "Stopped"; click into the task → **Logs** tab for output. Exit code visible on **Stop reason** field.

### §E.2 — (Optional one-time IAM unblock) Allow sandbox-agent to fire RunTask itself

If you'd like the agent to fire the probe automatically as part of every deploy (rather than requiring partner-Console invocation), attach this minimal inline policy to user `luciel-sandbox-agent`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RunSmokeProbeTaskdef",
      "Effect": "Allow",
      "Action": "ecs:RunTask",
      "Resource": "arn:aws:ecs:ca-central-1:729005488042:task-definition/luciel-smoke-probe:*"
    },
    {
      "Sid": "PassRolesForSmokeProbe",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": [
        "arn:aws:iam::729005488042:role/luciel-ecs-execution-role",
        "arn:aws:iam::729005488042:role/luciel-ecs-web-role"
      ],
      "Condition": {"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}}
    }
  ]
}
```

Path: IAM Console → Users → `luciel-sandbox-agent` → Permissions → Add permissions → Create inline policy → JSON tab → paste above → Name `LucielSandboxAgentRunSmokeProbe` → Create.

After this lands, the agent will append `RunTask` + describe-tasks polling to the standard deploy template in subsequent arcs.

---

## §F — (Optional v2) CloudWatch metric for deploy-gate  *(NEW — defer until needed)*

**Status:** NOT YET LIVE. v1 of the smoke probe uses log lines + exit codes; CloudWatch metric publishing was deferred from C4 because it requires a small IAM policy add to the task role.

### What it adds

A custom metric `Luciel/DeployGate/DeployGatePass` (value=1 on PASS, value=0 on FAIL) that can be dashboarded/alarmed independently from the raw exit code. Useful when scheduling the probe via EventBridge as a continuous health-monitor (vs. a per-deploy gate).

### How to wire

1. IAM Console → Roles → `luciel-ecs-web-role` → **Add permissions** → Attach inline policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "cloudwatch:PutMetricData",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "cloudwatch:namespace": "Luciel/DeployGate"
        }
      }
    }
  ]
}
```

2. Ping agent → agent registers `luciel-smoke-probe:2` task-def with an updated entrypoint that calls `aws cloudwatch put-metric-data --namespace Luciel/DeployGate ...` on each pass/fail leg. (Also requires `aws-cli` added to the Alpine image — agent will swap to `public.ecr.aws/aws-cli/aws-cli` base.)

Skip until partner wants alarms — v1 logs + exit codes are sufficient for manual deploy-gate use.

---

## Summary

Four carry-forward surfaces from Arc 7 C9 (§A-D), one primary Arc 8 add (§E — fire smoke probe after deploys), one optional v2 (§F — CloudWatch metric). All execute from partner's IAM identity via AWS Console or partner-CLI. Sandbox-agent cannot perform any of these without §E.2 unblock.

After §E.2 lands, every Arc 8+ deploy template will end with `aws ecs run-task ... luciel-smoke-probe ...` as an automatic 4th deploy-gate signal — matching the C10 runbook §6 closure evidence intent.
