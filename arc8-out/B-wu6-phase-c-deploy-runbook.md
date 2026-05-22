# Arc 8 WU-6 Phase C — Deploy Runbook

**Status:** Code ready (commit `30b8af4` on `origin/main`); operator + agent execute together.

**Goal:** Deploy the SES feedback / suppression / sink stack to production, closing the code side of:
- `D-ses-feedback-loop-not-wired-2026-05-22`
- `D-ses-suppression-app-layer-not-implemented-2026-05-22`
- `D-ses-reply-to-monitored-inbox-not-confirmed-2026-05-22` (post-deploy mailbox confirmation)

The sandbox-exit drift `D-ses-sandbox-exit-pending-2026-05-22` is separately gated on AWS Support case `177948223100786`; this runbook does not depend on its outcome.

---

## Phase C scope (seven steps, in execution order)

1. **Run two Alembic migrations on prod RDS** — `a91c4d2e7f08` (email_send_event) + `b2e5f17a3d9c` (email_suppression).
2. **Set the SSM parameter `SES_SNS_TOPIC_ARN`** so the route's TopicArn trust gate enforces.
3. **Build + push backend image `luciel-backend:82`** with paranoid Stage-1b local `docker inspect` gate.
4. **Force-deploy `luciel-backend-service`** to image #82; smoke-verify `/api/v1/version`.
5. **Subscribe the route's public URL to the `luciel-ses-events` SNS topic.**
6. **Synthetic bounce test** — send to the SES simulator's `bounce@simulator.amazonses.com` and verify an `email_send_event` row + `email_suppression` row land.
7. **Mailbox confirmation** — ensure `support@vantagemind.ai` is reachable and operator-monitored.

---

## Step 1 — Alembic migrations on prod RDS

**Pre-flight check:**
```powershell
# Confirm we're pointed at prod
aws ssm get-parameter --name /luciel/database-url --with-decryption --region ca-central-1 --query "Parameter.Value" --output text
# (Should return the prod connection string; do NOT print the secret to scrollback if recording)
```

**Pull and stage the new migrations on the ECS task that already has the backend image:**

The cleanest path is to run Alembic from inside a one-shot ECS task that uses the already-deployed backend image (built from `c3d974f`, which carries both migration files). This avoids running Alembic from the operator's laptop with the SecureString DB URL on disk.

```powershell
# Run a one-shot exec into a stopped task definition for the backend.
# Skip if you prefer to run migrations from a temporary ECS task -- the
# inline-policy LucielSESSendEmail (rightshaped at Phase B) does not
# matter here; migrations only need DB access via the existing task IAM.

# First, find a running backend task:
aws ecs list-tasks --cluster luciel-cluster --service-name luciel-backend-service --region ca-central-1 --query "taskArns[0]" --output text

# Then exec into it and run Alembic:
aws ecs execute-command --cluster luciel-cluster --task <TASK_ARN> --container <CONTAINER_NAME> --interactive --command "/bin/bash" --region ca-central-1

# Inside the container:
alembic current
# Expected: b4d8a2e7c1f3 (head)

alembic upgrade head
# Expected progression:
#   b4d8a2e7c1f3 -> a91c4d2e7f08 (email_send_event), running upgrade
#   a91c4d2e7f08 -> b2e5f17a3d9c (email_suppression), running upgrade

alembic current
# Expected: b2e5f17a3d9c (head)

# Confirm the two tables exist:
psql "$DATABASE_URL" -c "\d email_send_event"
psql "$DATABASE_URL" -c "\d email_suppression"
```

**Rollback (if either migration fails mid-stream):**

```bash
# Inside the container:
alembic downgrade b4d8a2e7c1f3
# Confirm rollback worked:
alembic current
# Expected: b4d8a2e7c1f3 (head)
```

The migration scripts are at:
- `alembic/versions/a91c4d2e7f08_arc8_wu6_email_send_event.py`
- `alembic/versions/b2e5f17a3d9c_arc8_wu6_email_suppression.py`

---

## Step 2 — SSM parameter `SES_SNS_TOPIC_ARN`

Add the SSM SecureString (or String — the ARN is not secret) so the route's `settings.ses_sns_topic_arn` gate enforces:

```powershell
aws ssm put-parameter `
  --name "/luciel/production/ses-sns-topic-arn" `
  --type "String" `
  --value "arn:aws:sns:ca-central-1:729005488042:luciel-ses-events" `
  --overwrite `
  --region ca-central-1

# Verify:
aws ssm get-parameter --name "/luciel/production/ses-sns-topic-arn" --region ca-central-1 --query "Parameter.Value" --output text
```

**Task-def env-var wiring:** the backend task definition must inject this SSM parameter as the env var `SES_SNS_TOPIC_ARN` (pydantic-settings maps `SES_SNS_TOPIC_ARN` env to `settings.ses_sns_topic_arn`). Confirm the task definition's `containerDefinitions[].secrets` array includes:

```json
{
  "name": "SES_SNS_TOPIC_ARN",
  "valueFrom": "arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/ses-sns-topic-arn"
}
```

If the task def does not yet carry this env-var binding, register a new task-def revision with it before Step 4's force-deploy. The agent will produce the new task-def JSON; partner reviews + applies via `aws ecs register-task-definition`.

---

## Step 3 — Build + push `luciel-backend:82`

Use the existing build script with the paranoid Stage-1b local `docker inspect` gate (Standard #11 + #13):

```powershell
# From the repo root, on a clean working tree at origin/main HEAD (commit 30b8af4 or later):
git pull origin main
git rev-parse --short HEAD
# Should be 30b8af4 (or whatever HEAD is at deploy time)

# Build:
$buildSha = git rev-parse --short HEAD
docker buildx build `
  --platform linux/amd64 `
  --provenance=false `
  --sbom=false `
  --build-arg BUILD_GIT_SHA=$buildSha `
  -t luciel-backend:82 `
  -t 729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend:82 `
  --load `
  .

# Stage 1b paranoid gate (Standard #13):
docker inspect luciel-backend:82 --format "USER={{.Config.User}} GIT_SHA={{range .Config.Env}}{{println .}}{{end}}" | Select-String "USER=|BUILD_GIT_SHA"
# Expected: USER=luciel (uid 10001), BUILD_GIT_SHA=<sha-matching-current-HEAD>

# Push:
aws ecr get-login-password --region ca-central-1 | docker login --username AWS --password-stdin 729005488042.dkr.ecr.ca-central-1.amazonaws.com
docker push 729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend:82

# Capture digest for the deploy step:
aws ecr describe-images --repository-name luciel-backend --image-ids imageTag=82 --region ca-central-1 --query "imageDetails[0].imageDigest" --output text
# Write this down -- it's the immutable identifier for the deploy verification.
```

---

## Step 4 — Force-deploy `luciel-backend-service`

```powershell
# If task-def revision was updated for SES_SNS_TOPIC_ARN (Step 2), update the service to point at the new revision first:
# aws ecs update-service --cluster luciel-cluster --service luciel-backend-service --task-definition luciel-backend:<NEW_REV> --region ca-central-1

# Force a new deployment (pulls the :82 tag fresh):
aws ecs update-service `
  --cluster luciel-cluster `
  --service luciel-backend-service `
  --force-new-deployment `
  --region ca-central-1

# Watch the deployment:
aws ecs describe-services --cluster luciel-cluster --services luciel-backend-service --region ca-central-1 --query "services[0].deployments"

# Wait for desired==running on the new deployment and the old deployment's running count to hit 0.

# Smoke-verify /version:
curl -s https://api.vantagemind.ai/api/v1/version | jq
# Expected:
# {
#   "app":     "Luciel Backend",
#   "version": "0.1.0",
#   "git_sha": "30b8af4"   <-- or whatever HEAD was at build time
#   "status":  "ok"
# }
```

**Rollback** (if `/version` returns the old SHA or 5xx for >2 min):

```powershell
# Force the service back to the previous task-def revision (luciel-backend:81 digest sha256:5983a2af...):
aws ecs update-service --cluster luciel-cluster --service luciel-backend-service --task-definition luciel-backend:81 --force-new-deployment --region ca-central-1
```

---

## Step 5 — Subscribe the route to the SNS topic

```powershell
# The route's public URL (assuming api.vantagemind.ai is the public ALB):
$endpointUrl = "https://api.vantagemind.ai/api/v1/ses-events"

aws sns subscribe `
  --topic-arn "arn:aws:sns:ca-central-1:729005488042:luciel-ses-events" `
  --protocol https `
  --notification-endpoint $endpointUrl `
  --region ca-central-1

# This returns a SubscriptionArn that is initially "PendingConfirmation".

# AWS SNS will POST a SubscriptionConfirmation to our endpoint. The route
# fetches the SubscribeURL automatically and confirms. Wait ~5 seconds
# and verify:
aws sns list-subscriptions-by-topic --topic-arn "arn:aws:sns:ca-central-1:729005488042:luciel-ses-events" --region ca-central-1
# Expected: the new subscription's SubscriptionArn now has a real ARN (no longer "PendingConfirmation").

# If confirmation fails (SubscriptionArn stays "PendingConfirmation"):
#   - Check CloudWatch logs: aws logs tail /ecs/luciel-backend --since 5m | Select-String "ses_events"
#   - Look for "SubscriptionConfirmation" log lines and any failure cause.
#   - If our route is healthy but the confirm failed, delete the subscription and re-subscribe.
```

---

## Step 6 — Synthetic bounce test

SES provides a simulator that always bounces. Send a real message to it and verify the feedback loop closes end-to-end:

```powershell
# From an ECS exec session into the backend container (so we send via the same SES IAM as production):
aws ecs execute-command --cluster luciel-cluster --task <TASK_ARN> --container <CONTAINER_NAME> --interactive --command "/bin/bash" --region ca-central-1

# Inside the container -- send a transactional email to the SES bounce simulator:
python -c "
import boto3
ses = boto3.client('sesv2', region_name='ca-central-1')
resp = ses.send_email(
    FromEmailAddress='support@vantagemind.ai',
    Destination={'ToAddresses': ['bounce@simulator.amazonses.com']},
    Content={
        'Simple': {
            'Subject': {'Data': 'Phase C synthetic bounce test'},
            'Body': {'Text': {'Data': 'This is a synthetic bounce test for Arc 8 WU-6 Phase C closure verification.'}},
        }
    },
    ConfigurationSetName='luciel-default',
)
print('MessageId:', resp['MessageId'])
"
# Capture the MessageId.

# Wait ~30 seconds for SES to bounce and SNS to deliver. Then verify:
psql \"\$DATABASE_URL\" -c \"SELECT event_id, event_type, address, received_at FROM email_send_event ORDER BY received_at DESC LIMIT 5\"
# Expected: one row with event_type='Bounce', address='bounce@simulator.amazonses.com'

psql \"\$DATABASE_URL\" -c \"SELECT address, reason, first_suppressed_at FROM email_suppression WHERE address='bounce@simulator.amazonses.com'\"
# Expected: one row with reason='HardBounce'

# Confirm the audit chain captured it:
psql \"\$DATABASE_URL\" -c \"SELECT action, resource_natural_id, created_at FROM admin_audit_log WHERE action='EMAIL_SUPPRESSION_RECORDED' ORDER BY created_at DESC LIMIT 5\"
# Expected: one row with resource_natural_id='bounce@simulator.amazonses.com'
```

**Cleanup:** the simulator's address is now suppressed. To re-test later, clear it:
```sql
DELETE FROM email_suppression WHERE address = 'bounce@simulator.amazonses.com';
```

(Or use the `complaint@simulator.amazonses.com` and `success@simulator.amazonses.com` simulators for additional path coverage.)

---

## Step 7 — Mailbox confirmation (closes `D-ses-reply-to-monitored-inbox-not-confirmed`)

Verify that the configured reply-to address `support@vantagemind.ai` is:
1. **Reachable** — send a test email TO it from an external address; confirm receipt.
2. **Operator-monitored** — confirm the partner has the inbox open / forwarded / on an alert path.

Once both confirmed, the drift closes.

---

## Closure verification (post-Phase-C)

DRIFTS.md updates after all seven steps complete:

- `D-ses-feedback-loop-not-wired-2026-05-22` → **CLOSED** (synthetic bounce produced `email_send_event` row)
- `D-ses-suppression-app-layer-not-implemented-2026-05-22` → **CLOSED** (synthetic bounce produced `email_suppression` row + audit row)
- `D-ses-reply-to-monitored-inbox-not-confirmed-2026-05-22` → **CLOSED** (mailbox confirmation step)

`D-ses-sandbox-exit-pending-2026-05-22` remains **SUBMITTED-AWAITING-AWS** until case `177948223100786` is approved.

---

## Cross-refs

- Code commit: `30b8af4` (route + tests + wiring + settings)
- Phase A code commit: `c3d974f` (migrations + service + 47 tests)
- Phase B AWS infra: SNS topic + config set + event destination + IAM rightshape (landed 2026-05-22, no commit; AWS-side)
- Doctrine truthification commit: `ebd3849` (DRIFTS + ARCHITECTURE + arc-record updates)
- Phase A+B execution record: `arc8-out/A-arc8-security-hardening-arc-record.md` §3.6.X
