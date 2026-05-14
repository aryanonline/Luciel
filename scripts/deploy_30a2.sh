#!/usr/bin/env bash
# Step 30a.2 — paid-intro trial + retention worker rollout
#
# What this deploys:
#   1. New backend image (with billing_service.py first-time gate +
#      Stripe Option A intro fee plumbing).
#   2. New worker image with embedded Celery beat (--beat flag in the
#      ECS task-def command override). Beat schedule lives in
#      app/worker/celery_app.py and fires run_retention_purge nightly
#      at 08:00 UTC.
#   3. Alembic migration dfea1a04e037 (deactivated_at columns on
#      tenant_configs, conversations, identity_claims + composite index
#      on (deactivated_at, tenant_id)). Migration is operator-run via
#      ECS exec into the backend service AFTER the new task-def is
#      registered but BEFORE the service is updated — same pattern as
#      deploy_27c.sh §0/9 preflight.
#
# What this does NOT deploy:
#   - The 6 production Stripe Price IDs for the recurring tiers and
#     the 1 intro-fee Price ID. Those are SSM-side and require the
#     Stripe live-account activation to complete first (D-stripe-live-
#     account-not-yet-activated-2026-05-13). Operator runs the 7
#     `aws ssm put-parameter` calls after activation, then triggers a
#     forced backend redeploy with `--force-new-deployment`.
#   - CloudFront invalidation. The marketing site sees no code change
#     in this rollout, so /* invalidation is operator-discretion at the
#     end of the Stripe price wire-up phase.
#
# Single-replica beat coupling:
#   The worker service runs at desiredCount=1 today. Beat is embedded
#   in the worker container via `--beat`. If we ever scale worker > 1
#   we MUST migrate to redbeat or split beat into its own service
#   (D-celery-beat-single-replica-coupling-2026-05-14).
#
# Idempotency:
#   Re-running this script with the same git SHA is a no-op for the
#   ECR push (tag already exists) and re-registers identical task-defs
#   (creates new revision numbers but otherwise idempotent). The
#   service-update at the end is what actually triggers a deploy.
set -euo pipefail

AWS_REGION="ca-central-1"
ACCOUNT_ID="729005488042"
ECR_REPO="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/luciel"
CLUSTER="luciel-cluster"
WEB_SERVICE="luciel-backend-service"
WORKER_SERVICE="luciel-worker-service"

SHA="$(git rev-parse --short HEAD)"
TAG="step30a2-${SHA}"
IMAGE="${ECR_REPO}:${TAG}"

echo "==> Step 30a.2 rollout starting"
echo "    git sha:   ${SHA}"
echo "    image:     ${IMAGE}"
echo "    cluster:   ${CLUSTER}"
echo

echo "==> [0/6] Preflight: confirm workspace is clean"
if [[ -n "$(git status --porcelain)" ]]; then
  echo "ERROR: workspace has uncommitted changes. Commit or stash first."
  git status --short
  exit 1
fi

echo "==> [0/6] Preflight: confirm alembic head is dfea1a04e037 in tree"
HEAD_REV="$(cd "$(git rev-parse --show-toplevel)" && \
  grep -lE 'down_revision\s*=\s*' alembic/versions/*.py | \
  xargs grep -l "step30a_2_deactivated_at_and_retention" | head -1)"
if [[ -z "${HEAD_REV}" ]]; then
  echo "ERROR: step30a_2 migration file not found in alembic/versions/."
  exit 1
fi
echo "    migration:  ${HEAD_REV}"

echo "==> [1/6] Building image"
docker build -t "${IMAGE}" .

echo "==> [1/6] ECR login + push"
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${ECR_REPO}"
docker push "${IMAGE}"

DIGEST="$(aws ecr describe-images \
  --repository-name luciel \
  --image-ids imageTag="${TAG}" \
  --region "${AWS_REGION}" \
  --query 'imageDetails[0].imageDigest' --output text)"
PINNED_IMAGE="${ECR_REPO}@${DIGEST}"
echo "    pinned: ${PINNED_IMAGE}"

echo "==> [2/6] Registering luciel-backend:NEW (code-only; SSM picks up new stripe_price_intro_fee at first read)"
aws ecs describe-task-definition \
  --task-definition luciel-backend \
  --region "${AWS_REGION}" \
  --query 'taskDefinition' > /tmp/backend-current.json

jq --arg img "${PINNED_IMAGE}" '
  {family, taskRoleArn, executionRoleArn, networkMode, containerDefinitions,
   volumes, placementConstraints, requiresCompatibilities, cpu, memory,
   runtimePlatform}
  | .containerDefinitions |= map(
      if .name == "web" then
        .image = $img
      else . end)
' /tmp/backend-current.json > /tmp/backend-30a2.json

BACKEND_NEW_ARN="$(aws ecs register-task-definition \
  --cli-input-json file:///tmp/backend-30a2.json \
  --region "${AWS_REGION}" \
  --query 'taskDefinition.taskDefinitionArn' --output text)"
echo "    registered: ${BACKEND_NEW_ARN}"

echo "==> [3/6] Registering luciel-worker:NEW (--beat flag added)"
aws ecs describe-task-definition \
  --task-definition luciel-worker \
  --region "${AWS_REGION}" \
  --query 'taskDefinition' > /tmp/worker-current.json

# Worker command: same as 27c plus --beat. Beat schedule (08:00 UTC
# nightly run_retention_purge) is defined in app/worker/celery_app.py
# conf.beat_schedule, so adding --beat here is the only deploy-side
# change required to start the cron firing.
jq --arg img "${PINNED_IMAGE}" '
  {family, taskRoleArn, executionRoleArn, networkMode, containerDefinitions,
   volumes, placementConstraints, requiresCompatibilities, cpu, memory,
   runtimePlatform}
  | .containerDefinitions |= map(
      if .name == "worker" then
        .image = $img
        | .command = [
            "celery","-A","app.worker.celery_app","worker",
            "--beat",
            "--loglevel=info",
            "-Q","luciel-memory-tasks",
            "--concurrency=2",
            "--without-gossip","--without-mingle","--without-heartbeat"
          ]
        | .environment = ((.environment // []) | map(
            select(.name != "CELERY_BROKER_URL" and .name != "REDIS_URL")
          ) + [{"name":"CELERY_BROKER_URL","value":"sqs://"}])
      else . end)
' /tmp/worker-current.json > /tmp/worker-30a2.json

WORKER_NEW_ARN="$(aws ecs register-task-definition \
  --cli-input-json file:///tmp/worker-30a2.json \
  --region "${AWS_REGION}" \
  --query 'taskDefinition.taskDefinitionArn' --output text)"
echo "    registered: ${WORKER_NEW_ARN}"

echo "==> [4/6] Alembic migration (OPERATOR-RUN)"
echo
echo "    PAUSE: run the migration via ECS exec BEFORE updating services."
echo "    Command (operator runs on laptop):"
echo
echo "      aws ecs execute-command \\"
echo "        --cluster ${CLUSTER} \\"
echo "        --task <existing-backend-task-arn> \\"
echo "        --container web --interactive \\"
echo "        --command 'alembic upgrade head' \\"
echo "        --region ${AWS_REGION}"
echo
echo "    Expected: 'Running upgrade c2a1b9f30e15 -> dfea1a04e037'."
echo "    Resume this script (or run the remaining steps manually) after success."
echo
read -p "    Press ENTER after alembic upgrade head reports the new revision (or Ctrl-C to abort)..."

echo "==> [5/6] Updating backend service to ${BACKEND_NEW_ARN}"
aws ecs update-service \
  --cluster "${CLUSTER}" \
  --service "${WEB_SERVICE}" \
  --task-definition "${BACKEND_NEW_ARN}" \
  --region "${AWS_REGION}" \
  --query 'service.taskDefinition' --output text

echo "==> [5/6] Updating worker service to ${WORKER_NEW_ARN}"
aws ecs update-service \
  --cluster "${CLUSTER}" \
  --service "${WORKER_SERVICE}" \
  --task-definition "${WORKER_NEW_ARN}" \
  --region "${AWS_REGION}" \
  --query 'service.taskDefinition' --output text

echo "==> [6/6] Waiting for both services to stabilize"
aws ecs wait services-stable \
  --cluster "${CLUSTER}" \
  --services "${WEB_SERVICE}" "${WORKER_SERVICE}" \
  --region "${AWS_REGION}"

echo
echo "==> Step 30a.2 rollout COMPLETE"
echo "    backend task-def: ${BACKEND_NEW_ARN}"
echo "    worker task-def:  ${WORKER_NEW_ARN}"
echo
echo "    Next: tail worker CloudWatch logs and confirm a 'beat: Starting...'"
echo "    line appears within 30s of task placement. Beat will fire its"
echo "    first run_retention_purge at the next 08:00 UTC."
echo
echo "    After Stripe activation completes:"
echo "      - aws ssm put-parameter --name /luciel/prod/stripe/price_id/intro_fee ..."
echo "      - aws ssm put-parameter ... (6 recurring Price IDs, if not yet seeded)"
echo "      - aws ecs update-service --force-new-deployment (backend only) to reload SSM"
echo "      - smoke 3 checkout flows (first-time + repeat per primitive)"
echo "      - CloudFront /* invalidation (only if marketing pages changed)"
