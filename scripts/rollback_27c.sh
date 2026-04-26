#!/usr/bin/env bash
# Step 27c — emergency rollback
# Flips async OFF by rolling web back to backend:10 and drains the worker.
# Code stays on :11 in ECR so we can investigate without redeploying.
set -euo pipefail

AWS_REGION="ca-central-1"
CLUSTER="luciel-cluster"
WEB_SERVICE="luciel-backend-service"
WORKER_SERVICE="luciel-worker-service"

echo "==> Rolling web back to luciel-backend:10 (async OFF)"
aws ecs update-service \
  --cluster "${CLUSTER}" --service "${WEB_SERVICE}" \
  --task-definition luciel-backend:10 \
  --region "${AWS_REGION}" >/dev/null
aws ecs wait services-stable \
  --cluster "${CLUSTER}" --services "${WEB_SERVICE}" --region "${AWS_REGION}"
echo "    web stable on :10 — sync extraction restored"

echo "==> Draining worker service to desired=0"
aws ecs update-service \
  --cluster "${CLUSTER}" --service "${WORKER_SERVICE}" \
  --desired-count 0 \
  --region "${AWS_REGION}" >/dev/null
aws ecs wait services-stable \
  --cluster "${CLUSTER}" --services "${WORKER_SERVICE}" --region "${AWS_REGION}"
echo "    worker drained"

echo "==> Rollback complete."
echo "    Investigate: CloudWatch /ecs/luciel-worker, SQS DLQ depth, recent memory_items writes."