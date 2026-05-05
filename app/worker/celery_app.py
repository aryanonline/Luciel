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

import logging
import os
import ssl
import threading

from celery import Celery
from celery.signals import worker_ready, worker_shutdown

_log = logging.getLogger(__name__)

# ---------- container HEALTHCHECK heartbeat (rev 11) ----------
# Earlier rollouts (rev 7..10) tried celery-inspect and process-existence
# probes; all four failed in production for distinct reasons documented
# in scripts/healthcheck_worker.py. The structural problem is that
# HEALTHCHECK CMD-SHELL stdout is captured in Docker's per-container
# health buffer, NOT awslogs, so we cannot observe probe failures from
# CloudWatch. Rev 11 inverts the relationship: the worker process
# itself emits an observable heartbeat (touch a file + log a line)
# every HEARTBEAT_INTERVAL_SECONDS, and the probe just stat()s the
# file. Producer-side logs go to CloudWatch, so we can verify the
# heartbeat is firing independently of whether the probe passes.
#
# Liveness semantics: this reports "the worker process is alive and
# its Python interpreter is scheduling daemon threads." It does NOT
# detect a wedged broker connection -- but a wedged broker cascades to
# a process exit within seconds via kombu's error handler, at which
# point the heartbeat thread also dies and the file goes stale.
HEARTBEAT_PATH = "/tmp/celery_alive"
HEARTBEAT_INTERVAL_SECONDS = 15

_heartbeat_stop = threading.Event()
_heartbeat_thread: threading.Thread | None = None


def _heartbeat_loop() -> None:
    """Touch HEARTBEAT_PATH every HEARTBEAT_INTERVAL_SECONDS until stopped."""
    while not _heartbeat_stop.is_set():
        try:
            # Open + close to update mtime atomically; touch() doesn't exist
            # in stdlib, and os.utime requires the file to exist already.
            with open(HEARTBEAT_PATH, "a"):
                os.utime(HEARTBEAT_PATH, None)
            # Heartbeat log line is the OBSERVABLE signal for this probe.
            # If CloudWatch shows these every ~15s, the producer is healthy.
            # If they stop, either the worker is dead or this thread wedged.
            _log.info("healthcheck heartbeat: touched %s", HEARTBEAT_PATH)
        except OSError as exc:
            # Log loudly but don't crash the worker. Probe will fail on
            # stale mtime, ECS will replace the task.
            _log.error(
                "healthcheck heartbeat FAILED to touch %s: %s",
                HEARTBEAT_PATH, exc,
            )
        # Use Event.wait so shutdown is responsive (no need to sleep full
        # interval before noticing _heartbeat_stop has been set).
        _heartbeat_stop.wait(HEARTBEAT_INTERVAL_SECONDS)


@worker_ready.connect
def _start_heartbeat(sender=None, **kwargs):  # noqa: ARG001
    """Start the heartbeat daemon thread once the worker is ready."""
    global _heartbeat_thread
    # Initial touch BEFORE starting the loop, so the file exists for the
    # very first probe attempt (which can happen as early as startPeriod
    # = 60s after container start, possibly before the first 15s tick).
    try:
        with open(HEARTBEAT_PATH, "a"):
            os.utime(HEARTBEAT_PATH, None)
        _log.info("healthcheck heartbeat: initial touch of %s", HEARTBEAT_PATH)
    except OSError as exc:
        _log.error(
            "healthcheck heartbeat: initial touch FAILED %s: %s",
            HEARTBEAT_PATH, exc,
        )
    _heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        name="luciel-healthcheck-heartbeat",
        daemon=True,
    )
    _heartbeat_thread.start()
    _log.info(
        "healthcheck heartbeat thread started (interval=%ss)",
        HEARTBEAT_INTERVAL_SECONDS,
    )


@worker_shutdown.connect
def _stop_heartbeat(sender=None, **kwargs):  # noqa: ARG001
    """Signal the heartbeat thread to exit on graceful shutdown."""
    _heartbeat_stop.set()
    _log.info("healthcheck heartbeat: shutdown signal sent")

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