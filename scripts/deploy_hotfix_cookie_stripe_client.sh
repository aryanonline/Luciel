#!/usr/bin/env bash
# Hotfix deploy — D-cookie-middleware-billingservice-missing-stripe-client-2026-05-16
#
# Backend-only image bump. No migration, no worker change, no SSM change.
#
# What the bug was
# ----------------
# app/middleware/session_cookie_auth.py constructed BillingService(db)
# with only one positional arg. BillingService.__init__ requires
# (db, stripe_client). Every cookied request to /api/v1/admin/* and
# /api/v1/dashboard/* raised TypeError inside the middleware's `try`,
# got caught by the broad `except Exception`, logged ERROR, and fell
# through to ApiKeyAuthMiddleware which returned its "Missing Bearer"
# 401 to the browser. T9 leg 5 (live closure test) surfaced it.
#
# What this deploys
# -----------------
#   1. New backend image with the one-line fix:
#        BillingService(db, get_stripe_client())
#      plus a new regression test pinning the call-site signature at AST
#      level (tests/api/test_cookie_middleware_billingservice_construction.py).
#
# What this does NOT touch
# ------------------------
#   - Worker service (no code change)
#   - SSM (no config change)
#   - Alembic (no schema change)
#   - Marketing site / CloudFront (no asset change)
#
# Idempotency: re-running with the same SHA is a no-op for ECR push and
# task-def registration (identical revision). The service-update at the
# end is what actually triggers the rolling restart.
set -euo pipefail

AWS_REGION="ca-central-1"
ACCOUNT_ID="729005488042"
ECR_REPO="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/luciel-backend"
CLUSTER="luciel-cluster"
WEB_SERVICE="luciel-backend-service"

SHA="$(git rev-parse --short HEAD)"
TAG="hotfix-cookie-stripe-client-${SHA}"
IMAGE="${ECR_REPO}:${TAG}"

echo "==> [1/4] Building image: ${IMAGE}"
docker build -t "${IMAGE}" .

echo "==> [2/4] ECR login + push"
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${ECR_REPO}"
docker push "${IMAGE}"

DIGEST="$(aws ecr describe-images \
  --repository-name luciel-backend \
  --image-ids imageTag="${TAG}" \
  --region "${AWS_REGION}" \
  --query 'imageDetails[0].imageDigest' --output text)"
PINNED_IMAGE="${ECR_REPO}@${DIGEST}"
echo "    pinned: ${PINNED_IMAGE}"

echo "==> [3/4] Registering luciel-backend:NEW (image bump only)"
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
' /tmp/backend-current.json > /tmp/backend-hotfix.json

BACKEND_NEW_ARN="$(aws ecs register-task-definition \
  --cli-input-json file:///tmp/backend-hotfix.json \
  --region "${AWS_REGION}" \
  --query 'taskDefinition.taskDefinitionArn' --output text)"
echo "    registered: ${BACKEND_NEW_ARN}"

echo "==> [4/4] Updating backend service to ${BACKEND_NEW_ARN}"
aws ecs update-service \
  --cluster "${CLUSTER}" \
  --service "${WEB_SERVICE}" \
  --task-definition "${BACKEND_NEW_ARN}" \
  --region "${AWS_REGION}" \
  --query 'service.taskDefinition' --output text

echo ""
echo "==> DEPLOY DISPATCHED"
echo "    Wait ~3-5 minutes for the new task to reach RUNNING / steady state."
echo "    Watch: aws ecs describe-services --cluster ${CLUSTER} --services ${WEB_SERVICE} \\"
echo "             --region ${AWS_REGION} --query 'services[0].deployments'"
echo ""
echo "    Smoke test once steady:"
echo "      1. Hard-reload the Dashboard in the browser (Ctrl+Shift+R)"
echo "      2. Confirm /api/v1/dashboard/tenant returns 200, not 401"
echo "      3. Confirm /api/v1/admin/agents returns 200"
echo ""
echo "    If smoke passes, resume T9 closure test legs 6-7 (self-serve refund + cascade)."
