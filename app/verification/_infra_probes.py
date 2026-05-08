"""Shared infrastructure probes for the verification harness.

Best-effort liveness checks for the broker (SQS in prod / Redis in dev) and
Celery worker, used by pillars that need to choose between MODE=full and
MODE=degraded paths at runtime. Both helpers are designed to NEVER raise --
any exception (import failure, connection refused, auth failure, timeout)
collapses to ``False``. This is the right default for verification-time use
because:

  - The harness must be able to run in environments where the broker or
    worker are intentionally absent (e.g. CI image without the broker
    container started), in which case every pillar that depends on async
    memory should fall back to its degraded checks rather than crash the
    whole run.
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
duplication itself was deferred to a later cleanup commit. C.6 was that
cleanup (the helpers moved here so both pillars import them). Step 29.y
Commit C32 then SQS-rewrote ``_worker_reachable`` -- see the function
docstring for the architectural change.

Both pillars now import from here. The helpers stay private (leading
underscore) because they are not part of any stable public verification
contract -- they are implementation details of mode selection, and the
authoritative semantic is "if MODE=full we ran the worker-dependent
assertions, if MODE=degraded we did not". Callers should never inspect the
helpers' return values for any reason other than that selection.

Why is this NOT in ``http_client.py``? Because broker/worker liveness is
infrastructure orthogonal to plain HTTP -- ``http_client`` talks to the
FastAPI backend and knows nothing about Redis, SQS, or Celery. Mixing the
two would muddle the module boundaries; ``_infra_probes.py`` keeps the seam
clean. ``_worker_reachable`` is the one helper here that DOES go through
HTTP (since C32) -- it does so because the only honest worker-liveness
check over SQS+predefined_queues is "ask the backend to enqueue and poll
audit_logs", and that path is exposed as a platform_admin POST.
"""

from __future__ import annotations

import os
from typing import Any

from app.core.config import settings


# Default queue name matches app/worker/celery_app.py task_default_queue.
# Hardcoded here rather than imported so this probe stays cheap and stays
# usable from CI environments where the celery_app import would fail.
_BROKER_DEFAULT_QUEUE = "luciel-memory-tasks"


# C32 cache: a single verify run has both P11 and P13 calling
# _worker_reachable(state). The probe round-trip costs ~1-3s (one SQS
# enqueue + audit-log poll), so we cache the per-run result keyed by
# id(state). RunState is a dataclass that lives for the duration of one
# `python -m app.verification` invocation, so id-based keying is safe:
# a fresh run instantiates a fresh RunState, getting a fresh cache slot.
# The cache is intentionally module-level rather than a RunState attribute
# to keep the dataclass shape clean (the C.6 invariant test pins which
# function names live where; adding RunState fields would scope-creep).
_WORKER_PROBE_CACHE: dict[int, bool] = {}


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
        That patch made the probe SQS-aware so P11 can run in FULL
        mode in prod -- but the WORKER probe was still control.ping(),
        which doesn't work over SQS+predefined_queues. C32 fixed
        ``_worker_reachable`` (see below) to close that loop.

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


def _worker_reachable(state: Any) -> bool:
    """SQS-aware worker liveness gate via the P25 backend probe route.

    Returns True iff the verify task can confirm a Celery worker process
    is actually consuming tasks from the broker. ``False`` on any failure
    (missing state, route 4xx/5xx, network error, route timeout 504).

    Why HTTP and not ``celery_app.control.ping()``
    ----------------------------------------------
    Step 27b's original probe used Celery's control plane (``ping()``),
    which over the Redis transport works because Redis broadcasts
    control messages. After the Step 27c-final SQS migration the worker
    runs with ``predefined_queues`` -- a Celery configuration that
    EXPLICITLY disables the control-plane fanout queue (it would require
    ListQueues + the auto-created reply queues, which the least-privilege
    worker role cannot create). With ``predefined_queues`` set,
    ``control.ping()`` returns ``[]`` UNCONDITIONALLY, regardless of
    whether a worker is actually running. The result: pre-C32, this
    function returned False on every prod verify run, dropping P11 to
    MODE=degraded for the entire SQS rollout (and silently masking
    whatever the worker was actually doing -- see the C32 commit
    message and drift token D-verify-worker-reachable-not-sqs-aware-
    2026-05-08).

    The ONLY honest SQS+predefined_queues worker-liveness check is:
    "enqueue a task, wait for the worker to commit an audit row that
    proves it consumed the message." P25 already exposes this contract
    as ``POST /api/v1/admin/forensics/worker_pipeline_probe_step29y``
    (mode=malformed, the default). A 200 response from that route IS
    proof that within 30s the worker process ran, Gate 1 fired on the
    malformed payload, AdminAuditRepository.record() committed a row,
    and the row became visible to the API process. A 504 response
    means at least one of those links is broken.

    C32 makes ``_worker_reachable`` call that exact route. The verify
    task itself remains pure-HTTP (no broker dependency from the verify
    container), and the probe runs on the backend (which IS the Celery
    producer in prod). This is the same architectural posture P25 holds.

    Producer-side exemption (Step 29 Commit B.3) is unaffected. P11 F1's
    direct ``svc.enqueue_extraction()`` and P11 F4's
    ``apply_async`` callsite remain direct -- they assert properties OF
    the producer path itself (latency, malformed-payload Gate 1
    behavior). C32 only changes the GATE that decides whether to enter
    those FULL-mode assertions; the assertions inside are untouched.

    Caching
    -------
    Each verify run typically calls this twice (P11 and P13). The probe
    round-trip is 1-3s on success, up to 30s on timeout. Result is
    cached per RunState instance via ``id(state)`` so the second pillar
    pays no additional cost. The cache is invalidated implicitly: a new
    verify run instantiates a new RunState, getting a fresh ``id(...)``.

    Inputs (read from ``state``)
    ----------------------------
      - ``state.platform_admin_key`` (env-loaded by RunState default)
      - ``state.tenant_id``           (set by P1)
      - ``state.instance_agent``      (set by P2)
      - ``state.chat_keys``           (set by P4; we read the agent-bound
                                       chat key prefix as actor_key_prefix)

    Any of these missing -> False (the probe route would 4xx anyway, and
    a False here drops P11/P13 to DEGRADED with their existing fallback
    rather than mid-pillar crashing on a missing-state assertion).
    """
    cache_key = id(state)
    cached = _WORKER_PROBE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    # Pull the inputs the probe route requires. Any missing field collapses
    # to False -- safer than letting the route 400 and propagating an
    # exception up through the pillar's mode-gate.
    pa = getattr(state, "platform_admin_key", None)
    tenant_id = getattr(state, "tenant_id", None)
    instance_agent = getattr(state, "instance_agent", None)
    chat_key_for = getattr(state, "chat_key_for", None)
    if not pa or not tenant_id or instance_agent is None or chat_key_for is None:
        _WORKER_PROBE_CACHE[cache_key] = False
        return False

    try:
        agent_ck = chat_key_for(instance_agent)
    except Exception:
        _WORKER_PROBE_CACHE[cache_key] = False
        return False
    if not isinstance(agent_ck, dict):
        _WORKER_PROBE_CACHE[cache_key] = False
        return False
    raw_key = agent_ck.get("key")
    if not isinstance(raw_key, str) or len(raw_key) < 12:
        _WORKER_PROBE_CACHE[cache_key] = False
        return False
    actor_key_prefix = raw_key[:12]

    # Lazy import of the HTTP client so a missing httpx in some odd
    # environment can't take out import-time module load. (The verify
    # task always has httpx; this is belt-and-suspenders.)
    try:
        from app.verification.http_client import pooled_client
    except ImportError:
        _WORKER_PROBE_CACHE[cache_key] = False
        return False

    # Wall-clock budget mirrors P25's: route's own deadline is 30s, we
    # allow a small grace window for ALB latency + the route's own poll
    # cadence. Float, in seconds.
    timeout_s = 40.0
    try:
        with pooled_client(timeout=timeout_s) as c:
            r = c.post(
                "/api/v1/admin/forensics/worker_pipeline_probe_step29y",
                headers={"Authorization": f"Bearer {pa}"},
                json={
                    "tenant_id": tenant_id,
                    "actor_key_prefix": actor_key_prefix,
                },
            )
    except Exception:
        _WORKER_PROBE_CACHE[cache_key] = False
        return False

    # 200 == worker is alive AND consuming AND committing audit rows.
    # Anything else (504 timeout, 401 auth misconfig, 404 route missing,
    # 5xx backend unhealthy) means we should NOT enter MODE=full.
    ok = r.status_code == 200
    _WORKER_PROBE_CACHE[cache_key] = ok
    return ok


__all__ = ["_broker_reachable", "_worker_reachable"]
