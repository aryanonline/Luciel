"""Shared infrastructure probes for the verification harness.

Best-effort liveness checks for the broker (Redis) and Celery worker, used by
pillars that need to choose between MODE=full and MODE=degraded paths at
runtime. Both helpers are designed to NEVER raise -- any exception (import
failure, connection refused, auth failure, timeout) collapses to ``False``.
This is the right default for verification-time use because:

  - The harness must be able to run in environments where Redis or Celery are
    intentionally absent (e.g. CI image without the broker container started),
    in which case every pillar that depends on async memory should fall back
    to its degraded checks rather than crash the whole run.
  - A noisy traceback from a transient broker hiccup would mask the real
    pillar-level failures the harness is trying to surface.
  - The cost of a false negative (running degraded when full was possible)
    is one missed full-mode pillar; the cost of a false positive (running
    full when no worker is actually consuming) is a silent FAIL of an
    assertion that depends on the worker -- the asymmetry justifies the
    fail-closed default.

History
-------
These helpers were originally inlined verbatim in ``pillar_11_async_memory``
(introduced in Step 27b alongside the async-memory pillar itself). When P13
(cross-tenant worker-identity spoofing, Step 28 Phase 2) needed the same
mode-selection logic, the helpers were duplicated inline rather than shared,
and the broker-only mode gate at the time silently flagged P13 FAIL on A2
when a local worker wasn't running. That bug was fixed in Step 29 Commit B.1
(`e4b03a4`) by mirroring P11's ``_worker_reachable`` into P13 -- but the
duplication itself was deferred to a later cleanup commit. This module is
that cleanup, landed as part of Step 29 Commit C.6.

Both pillars now import from here. The helpers stay private (leading
underscore) because they are not part of any stable public verification
contract -- they are implementation details of mode selection, and the
authoritative semantic is "if MODE=full we ran the worker-dependent
assertions, if MODE=degraded we did not". Callers should never inspect the
helpers' return values for any reason other than that selection.

Why is this NOT in ``http_client.py``? Because broker/worker liveness is
infrastructure orthogonal to HTTP -- ``http_client`` talks to the FastAPI
backend and knows nothing about Redis or Celery. Mixing the two would
muddle the module boundaries; ``_infra_probes.py`` keeps the seam clean
for any future probe (e.g. RDS-replica liveness, SQS queue depth).
"""

from __future__ import annotations

import os

from app.core.config import settings


# Default queue name matches app/worker/celery_app.py task_default_queue.
# Hardcoded here rather than imported so this probe stays cheap and stays
# usable from CI environments where the celery_app import would fail.
_BROKER_DEFAULT_QUEUE = "luciel-memory-tasks"


def _broker_reachable() -> bool:
    """Best-effort broker liveness. ``False`` on any failure.

    Mode gate. Returns True iff this process can reach the broker that
    Celery is configured against. The probe transport-switches on the
    URL scheme:

      - ``sqs://``  : ``boto3.get_queue_url(QueueName=...)`` against the
        default task queue. Strict per-queue read; does NOT require
        ``sqs:ListQueues`` -- safe to run from a least-privilege backend
        role that only has GetQueueUrl + SendMessage on the one queue.
      - ``redis://``, ``rediss://`` : ``redis.from_url(...).ping()`` (the
        original Step 27b path; preserved for local dev and any
        environment that still uses the Redis transport).
      - anything else : False (no probe path defined).

    History.
        Originally redis-only (Step 27b). After the prod SQS migration
        (Step 27c-final) the probe still pinged Redis, which silently
        DEGRADED Pillar 11 in prod for the entire SQS rollout because
        the redis ping always succeeded against the local-dev default
        URL. Step 29.y (Pillar 25 root-cause investigation) discovered
        that P11 had been DEGRADED-mode for weeks and the actual SQS
        producer path was never exercised by verify until P25 landed.
        This patch makes the probe SQS-aware so P11 can run in FULL
        mode in prod.

    A healthy broker means tasks can be enqueued -- it does NOT prove a
    worker is subscribed and consuming. Pair with ``_worker_reachable()``
    for the full mode-gate decision.
    """
    # Step 29.y close (D-redis-url-centralize-via-settings-2026-05-08):
    # CELERY_BROKER_URL is broker-selection state and stays a direct env
    # read; the REDIS_URL fallback is read via the central `settings`
    # source of truth so this probe agrees with what the worker actually
    # uses (see app/worker/celery_app.py and docs/architecture/broker-and-limiter.md).
    broker_url = os.environ.get(
        "CELERY_BROKER_URL",
        settings.redis_url,
    )
    if broker_url.startswith("sqs://"):
        try:
            import boto3  # noqa: WPS433 (intentional lazy import)
        except ImportError:
            return False
        try:
            region = os.environ.get("AWS_REGION", "ca-central-1")
            client = boto3.client("sqs", region_name=region)
            # GetQueueUrl is the cheapest auth+reach probe SQS exposes.
            # It is one request that returns the queue URL or raises
            # AWS.SimpleQueueService.NonExistentQueue / a permissions
            # error. It does NOT require ListQueues.
            client.get_queue_url(QueueName=_BROKER_DEFAULT_QUEUE)
            return True
        except Exception:
            return False
    if broker_url.startswith("redis://") or broker_url.startswith("rediss://"):
        try:
            import redis  # noqa: WPS433 (intentional lazy import)
        except ImportError:
            return False
        try:
            client = redis.Redis.from_url(
                broker_url, socket_connect_timeout=1.0, socket_timeout=1.0,
            )
            return bool(client.ping())
        except Exception:
            return False
    # Unknown scheme -- fail closed.
    return False


def _worker_reachable() -> bool:
    """Inspect Celery worker liveness via ``control.ping()``.

    Empty reply list = no workers are subscribed and consuming, so any
    ``apply_async`` will sit in the broker queue with no consumer. The
    full-mode pillars (P11 async memory; P13 cross-tenant worker-identity
    spoofing) require an actual worker because their assertions depend on
    Gate 6 / Gate 3 / Gate 4 actually firing inside the worker process,
    not merely on the task being enqueueable.

    Returns ``False`` if the celery_app import itself fails (e.g. a CI
    image that doesn't ship the Celery deps), which correctly drops both
    pillars to degraded mode in that environment.
    """
    try:
        from app.worker.celery_app import celery_app
    except ImportError:
        return False
    try:
        replies = celery_app.control.ping(timeout=1.0)
        return bool(replies)
    except Exception:
        return False


__all__ = ["_broker_reachable", "_worker_reachable"]
