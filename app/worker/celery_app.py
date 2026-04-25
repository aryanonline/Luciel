"""
Celery app factory for the Luciel worker (Step 27b).

Broker:         Redis (SSM /luciel/production/REDIS_URL on prod; localhost on dev)
Result backend: disabled (task_ignore_result=True) — no user content in Redis
Queue:          luciel-memory-tasks (main), luciel-memory-dlq (dead-letter)
TLS:            auto-enabled when REDIS_URL scheme is `rediss://`

Retries:        3 attempts, exponential backoff (2s/4s/8s, jittered)
Visibility:     30s
Acks late:      True (task must complete before ack; crash = redelivery)
Prefetch:       1 (embedding calls are CPU-bound; no head-of-line blocking)

Logs:           task_args / task_kwargs deliberately omitted from log format
                to prevent payload leakage into CloudWatch. Worker code
                logs only opaque ids + exception class names.
"""
from __future__ import annotations

import os
import ssl

from celery import Celery

# ---------- broker URL resolution ----------
# Precedence: explicit REDIS_URL env > default local dev URL.
# Prod ECS task-def injects REDIS_URL from SSM /luciel/production/REDIS_URL.
# ---------- broker URL resolution ----------
# Two supported broker modes:
#   1. SQS (prod):  CELERY_BROKER_URL=sqs://  + AWS creds via task role
#   2. Redis (dev): CELERY_BROKER_URL=redis://localhost:6379/0
#                   (or omit entirely; local default below)
#
# We deliberately do NOT use ElastiCache Redis in cluster mode as a broker:
# Celery's kombu Redis transport uses multi-key MULTI/EXEC pipelines that
# violate the ClusterCrossSlot constraint. SQS is the right primitive for
# our prod async-extraction workload anyway (already provisioned in Phase 1
# of the Step 27b deploy runbook).
BROKER_URL: str = os.environ.get(
    "CELERY_BROKER_URL",
    os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
)

# ---------- broker_transport_options ----------
# Per-broker config. SQS prod uses an explicit region + queue name prefix
# so the worker only sees its own queues. Redis dev passes through any
# rediss:// TLS hint. Visibility timeout matches the per-task budget.
_broker_transport_options: dict = {
    "visibility_timeout": 30,
}

if BROKER_URL.startswith("sqs://"):
    _broker_transport_options.update({
        "region": os.environ.get("AWS_REGION", "ca-central-1"),
        "predefined_queues": {
            "luciel-memory-tasks": {
                "url": f"https://sqs.{os.environ.get('AWS_REGION', 'ca-central-1')}.amazonaws.com/729005488042/luciel-memory-tasks",
            },
            "luciel-memory-dlq": {
                "url": f"https://sqs.{os.environ.get('AWS_REGION', 'ca-central-1')}.amazonaws.com/729005488042/luciel-memory-dlq",
            },
        },
        "polling_interval": 1.0,  # seconds between SQS long-poll batches
    })
elif BROKER_URL.startswith("rediss://"):
    _broker_transport_options["ssl_cert_reqs"] = ssl.CERT_NONE
# plain redis:// needs no extra options

# ---------- Celery app ----------
celery_app = Celery(
    "luciel",
    broker=BROKER_URL,
    include=["app.worker.tasks.memory_extraction"],
)

celery_app.conf.update(
    # ----- serialization -----
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,

    # ----- reliability -----
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="luciel-memory-tasks",

    # ----- results: DISABLED (no user content in backend) -----
    task_ignore_result=True,
    result_backend=None,

    # ----- retry defaults -----
    task_default_retry_delay=2,
    task_max_retries=3,

    # ----- broker timeouts -----
    broker_transport_options=_broker_transport_options,
    
    # ----- log hygiene: OMIT task_args / task_kwargs -----
    # Default Celery format includes %(args)s %(kwargs)s which leaks payload.
    worker_log_format=(
        "[%(asctime)s: %(levelname)s/%(processName)s] %(message)s"
    ),
    worker_task_log_format=(
        "[%(asctime)s: %(levelname)s/%(processName)s]"
        "[%(task_name)s(%(task_id)s)] %(message)s"
    ),
    worker_hijack_root_logger=False,
    worker_redirect_stdouts=False,

    # ----- worker runtime -----
    worker_send_task_events=True,
    task_send_sent_event=True,
)