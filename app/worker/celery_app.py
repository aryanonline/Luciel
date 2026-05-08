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

from app.core.config import settings

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
# Precedence (highest to lowest):
#   1. CELERY_BROKER_URL  -- explicit broker, e.g. `sqs://` in prod
#   2. REDIS_URL          -- shared cache/limit-store URL (dev fallback)
#   3. "redis://localhost:6379/0" -- local dev default
#
# Step 29.y close (D-redis-url-centralize-via-settings-2026-05-08):
# Read through `settings.redis_url` so this module shares the single
# source of truth defined in `app.core.config`. CELERY_BROKER_URL is
# kept as a direct env read because it is broker-selection state
# (sqs:// vs redis://...) that does NOT belong in `Settings.redis_url`.
# See `docs/architecture/broker-and-limiter.md` for the full split.
#
# Two supported broker modes:
#   1. SQS (prod):  CELERY_BROKER_URL=sqs://  + AWS creds via task role
#   2. Redis (dev): CELERY_BROKER_URL=redis://localhost:6379/0
#                   (or omit entirely; falls back to settings.redis_url)
#
# We deliberately do NOT use ElastiCache Redis in cluster mode as a broker:
# Celery's kombu Redis transport uses multi-key MULTI/EXEC pipelines that
# violate the ClusterCrossSlot constraint. SQS is the right primitive for
# our prod async-extraction workload anyway (already provisioned in Phase 1
# of the Step 27b deploy runbook). ElastiCache Redis IS used in prod, but
# only as the rate-limit storage backend (see app/middleware/rate_limit.py
# and docs/architecture/broker-and-limiter.md).
BROKER_URL: str = os.environ.get(
    "CELERY_BROKER_URL",
    settings.redis_url,
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

# Step 29.y gap-fix C17 (D-celery-app-set-default-or-import-order-2026-05-08):
# Force this Celery instance to be the process-wide default_app, so that
# `@shared_task`-decorated functions resolve to OUR app (Redis/SQS broker)
# regardless of which module imports them first. C13's import-on-boot in
# `app/main.py` is necessary but NOT sufficient: `Celery()`'s constructor
# only registers itself as default if no default has been set, and under
# uvicorn another import path can touch `celery.current_app` before our
# module loads, leaving the default at Celery's stock `Celery()` instance
# whose broker is `amqp://guest@localhost//`. `set_default()` is
# unconditional and idempotent -- safe to call at module import.
#
# Symptom this fixes: probe (and any chat turn) raises
#   kombu.exceptions.OperationalError [WinError 10061] amqp/5672
# on the FIRST `extract_memory_from_turn.apply_async(...)` call, despite
# `from app.worker.celery_app import celery_app` running on uvicorn boot.
# A fresh `python -c` shell does not reproduce because import order is
# different there (no FastAPI/middleware touches current_app first).
celery_app.set_default()

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

# Step 28 P3-E.2 / Pillar 23: install the audit-log hash-chain
# before_flush event on the global ORM Session. The worker writes
# AdminAuditLog rows from memory_extraction.py via
# AdminAuditRepository.record(); without the event, those rows would
# have NULL row_hash / prev_row_hash and Pillar 23 would FAIL on the
# next verify run. Installed at module-import time so every worker
# process picks it up before the first task runs.
from app.repositories.audit_chain import install_audit_chain_event  # noqa: E402

install_audit_chain_event()


# ---------------------------------------------------------------------
# Step 29.y: defensive producer-pool warmup at module import.
# ---------------------------------------------------------------------
#
# Background.
#   During step-29.y rollout, the previously-running backend task
#   (luciel-backend:30, taskId c6225b8776ac, alive ~hour-plus) was
#   observed to fail extract_memory_from_turn.apply_async() with
#   `botocore.exceptions.ClientError: AccessDenied` on
#   `sqs:ListQueues`. The traceback originated inside
#   `kombu/transport/SQS.py:_update_queue_cache`, which only calls
#   `list_queues` when `Channel.predefined_queues` is empty -- and
#   `Channel.predefined_queues` reads from
#   `connection.client.transport_options['predefined_queues']`. So
#   the producer pool's cached Connection had EMPTY transport_options
#   despite `app.conf.broker_transport_options` being correct (verified
#   in-process via diagnostic instrumentation: app.conf had the right
#   value, but apply_async constructed a Channel that didn't see it).
#
#   On a freshly-redeployed task we could not reproduce the failure:
#   apply_async published cleanly, predefined_queues was populated on
#   the publish-time Channel, no ListQueues call happened. Mechanism
#   for the original drift (long-running task -> producer Connection
#   loses transport_options) is not characterized.
#
# What this guard does.
#   At module import, after `conf.update(...)` has installed
#   `broker_transport_options`, we read
#   `celery_app.amqp.producer_pool.connections.connection.transport_options`
#   exactly once. This forces eager construction of the producer
#   Connection from the just-set conf, BEFORE any FastAPI thread
#   pool worker can race a lazy init. Subsequent apply_async calls
#   reuse the same Connection through the producer pool, so the
#   Channel they build sees `predefined_queues` and skips ListQueues.
#
#   The eager warmup was the ONLY structural difference between v1
#   of the diag (in admin_forensics.py) which made the route start
#   passing immediately, and v2 (pure read-only) which we used to
#   confirm a fresh redeploy alone was also sufficient. Both observed
#   green; we ship the warmup permanently because it is the cheapest
#   defense against the observed failure mode and protects EVERY
#   producer site (chat path, verify probe, future async producers),
#   not just the verify route.
#
# What this guard does NOT do.
#   It does not explain why the prior task drifted. It is a defensive
#   eager-init, not a root-cause fix. The investigation is documented
#   in docs/CANONICAL_RECAP.md (recap v3.5) as open verify-debt:
#   "Producer-pool transport_options drift on aged uvicorn tasks --
#   mechanism unknown; warmup guard added; if drift recurs the
#   warning log below will fire on the next probe call."
#
# Detection on recurrence.
#   If the warmup itself reads back a Connection whose transport_options
#   are missing predefined_queues, we log a structured WARNING. Verify
#   would then catch the broken state via Pillar 25.
import logging as _step29y_log  # noqa: E402

_step29y_logger = _step29y_log.getLogger("luciel.celery.step29y")
try:
    _pp_conn = celery_app.amqp.producer_pool.connections.connection
    _pp_to = getattr(_pp_conn, "transport_options", {}) or {}
    if BROKER_URL.startswith("sqs://") and "predefined_queues" not in _pp_to:
        _step29y_logger.warning(
            "STEP29Y_PRODUCER_DRIFT producer Connection transport_options "
            "lacks 'predefined_queues' at module import; apply_async will "
            "hit SQS ListQueues. transport_options=%s",
            _pp_to,
        )
    else:
        _step29y_logger.info(
            "step29y producer-pool warmup OK (predefined_queues present=%s)",
            "predefined_queues" in _pp_to,
        )
except Exception as _exc:
    _step29y_logger.warning(
        "STEP29Y_PRODUCER_WARMUP_FAILED at import: %r", _exc,
    )
