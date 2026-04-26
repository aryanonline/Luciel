#!/usr/bin/env bash
# Step 27c — async memory activation rollout
set -euo pipefail

AWS_REGION="ca-central-1"
ACCOUNT_ID="729005488042"
ECR_REPO="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/luciel"
CLUSTER="luciel-cluster"
WEB_SERVICE="luciel-backend-service"
WORKER_SERVICE="luciel-worker-service"
MAIN_QUEUE_URL="https://sqs.${AWS_REGION}.amazonaws.com/${ACCOUNT_ID}/luciel-memory-tasks"
DLQ_URL="https://sqs.${AWS_REGION}.amazonaws.com/${ACCOUNT_ID}/luciel-memory-dlq"

SHA="$(git rev-parse --short HEAD)"
TAG="step27.1-${SHA}"
IMAGE="${ECR_REPO}:${TAG}"

echo "==> Step 27c rollout starting"
echo "    git sha:   ${SHA}"
echo "    image:     ${IMAGE}"
echo "    cluster:   ${CLUSTER}"
echo

echo "==> [0/9] Preflight: confirm main is 1c3b058 or descendant"
git merge-base --is-ancestor 1c3b058 HEAD || {
  echo "ERROR: HEAD is not a descendant of the SQS broker fix commit."
  exit 1
}
echo "==> [0/9] Preflight: prod RDS migration check is operator-run via ECS exec"

echo "==> [1/9] Building image"
docker build -t "${IMAGE}" .

echo "==> [1/9] ECR login + push"
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

echo "==> [2/9] Registering luciel-backend:10 (async OFF)"
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
        | .environment = ((.environment // []) | map(
            select(.name != "MEMORY_EXTRACTION_ASYNC")
          ) + [{"name":"MEMORY_EXTRACTION_ASYNC","value":"false"}])
      else . end)
' /tmp/backend-current.json > /tmp/backend-10.json

BACKEND_10_ARN="$(aws ecs register-task-definition \
  --cli-input-json file:///tmp/backend-10.json \
  --region "${AWS_REGION}" \
  --query 'taskDefinition.taskDefinitionArn' --output text)"
echo "    registered: ${BACKEND_10_ARN}"

echo "==> [3/9] Registering luciel-worker:3"
aws ecs describe-task-definition \
  --task-definition luciel-worker \
  --region "${AWS_REGION}" \
  --query 'taskDefinition' > /tmp/worker-current.json

jq --arg img "${PINNED_IMAGE}" '
  {family, taskRoleArn, executionRoleArn, networkMode, containerDefinitions,
   volumes, placementConstraints, requiresCompatibilities, cpu, memory,
   runtimePlatform}
  | .containerDefinitions |= map(
      if .name == "worker" then
        .image = $img
        | .command = [
            "celery","-A","app.worker.celery_app","worker",
            "--loglevel=info",
            "-Q","luciel-memory-tasks",
            "--concurrency=2",
            "--without-gossip","--without-mingle","--without-heartbeat"
          ]
        | .environment = ((.environment // []) | map(
            select(.name != "CELERY_BROKER_URL" and .name != "REDIS_URL")
          ) + [{"name":"CELERY_BROKER_URL","value":"sqs://"}])
      else . end)
' /tmp/worker-current.json > /tmp/worker-3.json

WORKER_3_ARN="$(aws ecs register-task-definition \
  --cli-input-json file:///tmp/worker-3.json \
  --region "${AWS_REGION}" \
  --query 'taskDefinition.taskDefinitionArn' --output text)"
echo "    registered: ${WORKER_3_ARN}"

echo "==> [4/9] Rolling web to backend:10"
aws ecs update-service \
  --cluster "${CLUSTER}" --service "${WEB_SERVICE}" \
  --task-definition "${BACKEND_10_ARN}" \
  --region "${AWS_REGION}" >/dev/null
aws ecs wait services-stable \
  --cluster "${CLUSTER}" --services "${WEB_SERVICE}" --region "${AWS_REGION}"
echo "    web stable on :10 (sync still ON, async still OFF)"

read -p "==> [5/9] Scale luciel-worker-service to desired=1 against :3? [y/N] " ans
[[ "${ans}" == "y" ]] || { echo "Aborted."; exit 1; }

aws ecs update-service \
  --cluster "${CLUSTER}" --service "${WORKER_SERVICE}" \
  --task-definition "${WORKER_3_ARN}" \
  --desired-count 1 \
  --region "${AWS_REGION}" >/dev/null
aws ecs wait services-stable \
  --cluster "${CLUSTER}" --services "${WORKER_SERVICE}" --region "${AWS_REGION}"

echo "==> [6/9] Verifying worker readiness"
sleep 15
LOG_GROUP="/ecs/luciel-worker"
LATEST_STREAM="$(aws logs describe-log-streams \
  --log-group-name "${LOG_GROUP}" \
  --order-by LastEventTime --descending --max-items 1 \
  --region "${AWS_REGION}" \
  --query 'logStreams[0].logStreamName' --output text)"

aws logs filter-log-events \
  --log-group-name "${LOG_GROUP}" \
  --log-stream-names "${LATEST_STREAM}" \
  --region "${AWS_REGION}" \
  --query 'events[].message' --output text \
  | tee /tmp/worker-boot.log

grep -q "celery@.* ready" /tmp/worker-boot.log || {
  echo "ERROR: worker did not reach 'ready'. Aborting."; exit 1; }
grep -Ei "ClusterCrossSlot|ListQueues|CreateQueue" /tmp/worker-boot.log && {
  echo "ERROR: forbidden broker call detected. Aborting."; exit 1; } || true

DEPTH_BEFORE="$(aws sqs get-queue-attributes \
  --queue-url "${MAIN_QUEUE_URL}" \
  --attribute-names ApproximateNumberOfMessages \
  --region "${AWS_REGION}" \
  --query 'Attributes.ApproximateNumberOfMessages' --output text)"
echo "    main queue depth: ${DEPTH_BEFORE}"
DLQ_DEPTH="$(aws sqs get-queue-attributes \
  --queue-url "${DLQ_URL}" \
  --attribute-names ApproximateNumberOfMessages \
  --region "${AWS_REGION}" \
  --query 'Attributes.ApproximateNumberOfMessages' --output text)"
echo "    dlq depth:        ${DLQ_DEPTH}"
[[ "${DLQ_DEPTH}" == "0" ]] || { echo "ERROR: DLQ non-empty pre-flip."; exit 1; }

echo "==> [7/9] Registering luciel-backend:11 (async ON)"
jq --arg img "${PINNED_IMAGE}" '
  {family, taskRoleArn, executionRoleArn, networkMode, containerDefinitions,
   volumes, placementConstraints, requiresCompatibilities, cpu, memory,
   runtimePlatform}
  | .containerDefinitions |= map(
      if .name == "web" then
        .image = $img
        | .environment = ((.environment // []) | map(
            select(.name != "MEMORY_EXTRACTION_ASYNC")
          ) + [{"name":"MEMORY_EXTRACTION_ASYNC","value":"true"}])
      else . end)
' /tmp/backend-current.json > /tmp/backend-11.json

BACKEND_11_ARN="$(aws ecs register-task-definition \
  --cli-input-json file:///tmp/backend-11.json \
  --region "${AWS_REGION}" \
  --query 'taskDefinition.taskDefinitionArn' --output text)"
echo "    registered: ${BACKEND_11_ARN}"

read -p "==> [8/9] Roll web :10 -> :11 to flip MEMORY_EXTRACTION_ASYNC=true? [y/N] " ans
[[ "${ans}" == "y" ]] || { echo "Aborted."; exit 1; }

aws ecs update-service \
  --cluster "${CLUSTER}" --service "${WEB_SERVICE}" \
  --task-definition "${BACKEND_11_ARN}" \
  --region "${AWS_REGION}" >/dev/null
aws ecs wait services-stable \
  --cluster "${CLUSTER}" --services "${WEB_SERVICE}" --region "${AWS_REGION}"
echo "    web stable on :11 — async path now live"

echo "==> [9/9] Post-flip verification"
sleep 30

for i in 1 2 3 4 5 6; do
  D="$(aws sqs get-queue-attributes \
    --queue-url "${MAIN_QUEUE_URL}" \
    --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
    --region "${AWS_REGION}" \
    --query 'Attributes' --output json)"
  echo "    t+${i}0s queue: ${D}"
  sleep 10
done

DLQ_AFTER="$(aws sqs get-queue-attributes \
  --queue-url "${DLQ_URL}" \
  --attribute-names ApproximateNumberOfMessages \
  --region "${AWS_REGION}" \
  --query 'Attributes.ApproximateNumberOfMessages' --output text)"
[[ "${DLQ_AFTER}" == "0" ]] || {
  echo "ERROR: DLQ has ${DLQ_AFTER} messages post-flip. Investigate before tagging."
  exit 1; }

TASK_ARN="$(aws ecs list-tasks \
  --cluster "${CLUSTER}" --service-name "${WEB_SERVICE}" \
  --region "${AWS_REGION}" \
  --query 'taskArns[0]' --output text)"

aws ecs execute-command \
  --cluster "${CLUSTER}" --task "${TASK_ARN}" \
  --container web --interactive \
  --command "python -m app.verification --mode=full" \
  --region "${AWS_REGION}"

echo "==> If 11/11 green, tag the release:"
echo "    git tag -a step-27-20260425 -m 'Step 27c: async memory live (11/11 MODE=full)'"
echo "    git push origin step-27-20260425"
