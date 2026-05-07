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


def _broker_reachable() -> bool:
    """Best-effort Redis ping. ``False`` on any failure (import, conn, auth).

    Local dev uses Redis broker. Prod uses SQS (Step 27c-final). A healthy
    broker means tasks can be enqueued -- it does NOT prove a worker is
    subscribed and consuming. Pair with ``_worker_reachable()`` for the
    full mode-gate decision.
    """
    try:
        import redis  # noqa: WPS433 (intentional lazy import)
    except ImportError:
        return False
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    try:
        client = redis.Redis.from_url(
            url, socket_connect_timeout=1.0, socket_timeout=1.0,
        )
        return bool(client.ping())
    except Exception:
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
