"""Per-instance conversation budget counter (Arc 18, §3.4.1b).

The budget meter counts CONVERSATIONS (sessions), not LLM calls. A
session that runs a multi-iteration PLAN→ACT→REFLECT loop is ONE
conversation; it is counted exactly once, at the first LLM call, via a
per-session idempotency marker so REFLECT-loop iterations never
double-count (spec §23).

The counter key is ``(admin_id, instance_id, billing_period_start)`` per
§3.4.1b. ``billing_period_start`` is the Stripe cycle anchor for paying
tiers (advances on the invoice.paid / subscription.renewed reset) and a
signup-anchored monthly window for Free (Free never bills, so the anchor
only needs to be deterministic and to roll monthly without a webhook —
see ``app.runtime.billing_period`` for the Free fallback).

Storage discipline mirrors ``app.tools.byo.circuit_breaker``: a
``Backend`` Protocol with a Redis production backend and an in-memory
test backend, so unit tests drive the meter deterministically without a
live Redis. Redis is the same instance already used as the Celery broker
(``settings.redis_url``).

Redis is EPHEMERAL — the durable billing record of a closed period's
overage is the Postgres ``conversation_overage_ledger`` table written at
cycle close. The counter carries a long TTL so a missed reset webhook
self-heals; the reset webhook is the primary advance mechanism.
"""

from __future__ import annotations

import logging
import time
from typing import Optional, Protocol

logger = logging.getLogger(__name__)

# Long TTL so a counter survives a full billing cycle plus slack; a
# missed reset webhook self-heals when the key's period_start advances.
_COUNTER_TTL_SECONDS = 70 * 24 * 60 * 60  # 70 days
# Per-session "already counted" marker TTL — a single conversation never
# spans days; one day is generous and bounds stale markers.
_SESSION_MARKER_TTL_SECONDS = 24 * 60 * 60
# Alert "already fired" marker — lives for the billing period.
_ALERT_MARKER_TTL_SECONDS = _COUNTER_TTL_SECONDS


def _counter_key(admin_id: str, instance_id: int, period_start: str) -> str:
    return f"luciel:budget:count:{admin_id}:{instance_id}:{period_start}"


def _session_marker_key(session_id: str) -> str:
    return f"luciel:budget:counted:{session_id}"


def _alert_marker_key(
    admin_id: str, instance_id: int, period_start: str, threshold: int
) -> str:
    return f"luciel:budget:alert:{admin_id}:{instance_id}:{period_start}:{threshold}"


# ---------------------------------------------------------------------
# Backend protocol — Redis in production, InMemory in tests
# ---------------------------------------------------------------------


class Backend(Protocol):
    def get(self, key: str) -> Optional[str]: ...
    def delete(self, key: str) -> None: ...
    def incr_with_ttl(self, key: str, ttl_seconds: int) -> int: ...
    def set_nx_with_ttl(self, key: str, value: str, ttl_seconds: int) -> bool: ...


class InMemoryBackend:
    """Test backend — single-process dict. Not thread-safe; tests drive
    the meter serially."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, float]] = {}

    def _expired(self, key: str) -> bool:
        entry = self._store.get(key)
        if entry is None:
            return True
        _, expires = entry
        return expires < time.monotonic()

    def get(self, key: str) -> Optional[str]:
        if self._expired(key):
            self._store.pop(key, None)
            return None
        return self._store[key][0]

    def _set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._store[key] = (value, time.monotonic() + ttl_seconds)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def incr_with_ttl(self, key: str, ttl_seconds: int) -> int:
        cur = self.get(key)
        n = int(cur) + 1 if cur is not None else 1
        self._set(key, str(n), ttl_seconds)
        return n

    def set_nx_with_ttl(self, key: str, value: str, ttl_seconds: int) -> bool:
        if self.get(key) is not None:
            return False
        self._set(key, value, ttl_seconds)
        return True


class RedisBackend:
    """Production backend — wraps a ``redis.Redis`` client.

    Construct via ``RedisBackend.from_settings()`` so callers do not need
    to thread a client through. Uses ``settings.redis_url`` — the same
    instance as the Celery broker + readiness probe.
    """

    def __init__(self, client) -> None:  # type: ignore[no-untyped-def]
        self._client = client

    @classmethod
    def from_settings(cls) -> "RedisBackend":
        import redis  # local import — module stays importable without redis

        from app.core.config import settings

        client = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
            decode_responses=True,
        )
        return cls(client)

    def get(self, key: str) -> Optional[str]:
        try:
            val = self._client.get(key)
        except Exception:  # noqa: BLE001
            logger.exception("BudgetMeter Redis get(%s) failed", key)
            return None
        if val is None:
            return None
        return val if isinstance(val, str) else val.decode("utf-8")

    def delete(self, key: str) -> None:
        try:
            self._client.delete(key)
        except Exception:  # noqa: BLE001
            logger.exception("BudgetMeter Redis delete(%s) failed", key)

    def incr_with_ttl(self, key: str, ttl_seconds: int) -> int:
        try:
            pipe = self._client.pipeline()
            pipe.incr(key)
            pipe.expire(key, ttl_seconds)
            results = pipe.execute()
            return int(results[0])
        except Exception:  # noqa: BLE001
            logger.exception("BudgetMeter Redis incr(%s) failed", key)
            # Fail-OPEN on Redis outage: return 0 so the gate treats the
            # session as within budget rather than blocking a paying
            # customer or wrongly capping Free on infra failure. The
            # mid-conversation guarantee (Vision §2) forbids cutting off
            # service on transient infra errors.
            return 0

    def set_nx_with_ttl(self, key: str, value: str, ttl_seconds: int) -> bool:
        try:
            return bool(self._client.set(key, value, nx=True, ex=ttl_seconds))
        except Exception:  # noqa: BLE001
            logger.exception("BudgetMeter Redis SETNX(%s) failed", key)
            # Fail-OPEN: report "already counted / already alerted" so a
            # Redis outage never double-counts a session or spams alerts.
            return False


class BudgetMeter:
    """Counts conversations per (admin, instance, billing period).

    Construct one per process. Identifiers are the runtime request's
    ``admin_id`` (tenant), ``luciel_instance_id`` (per-instance scope),
    ``session_id`` (conversation), and the resolved ``period_start``
    (ISO string anchor).
    """

    def __init__(self, backend: Optional[Backend] = None) -> None:
        self._backend = backend or RedisBackend.from_settings()

    def count_session_once(
        self,
        *,
        admin_id: str,
        instance_id: int,
        period_start: str,
        session_id: str,
    ) -> int:
        """Increment the conversation counter once per session.

        Sets a per-session marker with SETNX; only increments the period
        counter when the marker was newly set. A multi-iteration loop
        calls this on every iteration but increments exactly once
        (idempotency, spec §23). Returns the current period count
        (post-increment on the first call; the unchanged count on
        subsequent calls within the same session).
        """
        newly = self._backend.set_nx_with_ttl(
            _session_marker_key(session_id), "1", _SESSION_MARKER_TTL_SECONDS
        )
        if newly:
            return self._backend.incr_with_ttl(
                _counter_key(admin_id, instance_id, period_start),
                _COUNTER_TTL_SECONDS,
            )
        return self.current_count(
            admin_id=admin_id, instance_id=instance_id, period_start=period_start
        )

    def current_count(
        self, *, admin_id: str, instance_id: int, period_start: str
    ) -> int:
        cur = self._backend.get(_counter_key(admin_id, instance_id, period_start))
        return int(cur) if cur is not None else 0

    def reset(self, *, admin_id: str, instance_id: int, period_start: str) -> None:
        """Reset the counter for a closed billing period.

        Called at cycle close (invoice.paid / subscription.renewed). The
        NEW period uses a NEW ``period_start`` in the key, so deleting the
        old key is belt-and-suspenders cleanup rather than the reset
        mechanism itself.
        """
        self._backend.delete(_counter_key(admin_id, instance_id, period_start))

    def mark_alert_fired_once(
        self,
        *,
        admin_id: str,
        instance_id: int,
        period_start: str,
        threshold: int,
    ) -> bool:
        """Return True the FIRST time a threshold (80/100) alert fires for
        this (instance, period); False thereafter. Backs idempotent
        alerting so an admin is notified once per threshold per period.
        """
        return self._backend.set_nx_with_ttl(
            _alert_marker_key(admin_id, instance_id, period_start, threshold),
            "1",
            _ALERT_MARKER_TTL_SECONDS,
        )
