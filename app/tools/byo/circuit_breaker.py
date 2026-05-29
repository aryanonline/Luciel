"""Per-endpoint circuit breaker for BYO webhook dispatch — Arc 12 WU6.

Why this module exists
----------------------

§3.3.5 mandates a per-endpoint circuit breaker around BYO webhook
dispatch: open after 5 consecutive failures in a 60s window;
half-open after 60s; close on first success. The breaker state
keyed per registered endpoint must survive across worker
invocations and across worker processes — Redis is the natural
home given the codebase already runs Redis as the Celery broker
(``settings.redis_url``).

State model
-----------

Three states (§3.3.5):

  * ``closed``    — dispatch proceeds; transport errors increment a
                    rolling failure counter.
  * ``open``      — refuse the call at dispatch (raises
                    ``CircuitOpenError``); after 60s elapses since
                    the breaker tripped, the NEXT dispatch attempt
                    sees ``half_open``.
  * ``half_open`` — exactly one probe is allowed through; success
                    closes the breaker, failure re-opens it for
                    another 60s.

Key scheme (Redis)
------------------

Five keys per endpoint id (scoped per registered endpoint, not per
URL — admins may revoke+re-register the same URL and we want the
breaker to start fresh):

  * ``luciel:byo:cb:{eid}:state``       — current state string
                                          (``closed`` / ``open`` /
                                          ``half_open``).
  * ``luciel:byo:cb:{eid}:fails``       — INCR-ing failure counter,
                                          TTL 60s (the "60s window").
  * ``luciel:byo:cb:{eid}:opened_at``   — unix ts (str) the breaker
                                          last tripped open; readers
                                          compute ``now - opened_at``
                                          to detect 60s expiry.
  * ``luciel:byo:cb:{eid}:probe_lock``  — half-open dispatch lock so
                                          only one probe at a time
                                          (SET NX PX 5000).
  * ``luciel:byo:cb:{eid}:state_ttl``   — TTL on the state key itself
                                          (24h) so a stuck record
                                          cannot live forever in the
                                          cache.

All keys carry a 24h floor TTL so stale records expire if an
endpoint is removed and never reused.

Tunables
--------

* ``failure_threshold = 5`` (§3.3.5)
* ``failure_window_seconds = 60`` (§3.3.5)
* ``open_duration_seconds = 60`` (§3.3.5)

These are exposed as module constants and as constructor args so
tests can shorten them.

In-memory backend (tests)
-------------------------

The breaker is parameterised on a ``Backend`` protocol; a Redis
backend ships in this module and an in-memory backend is provided
for unit tests so we do not need to spin up a real Redis. Tests
inject the in-memory backend directly.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Public constants — §3.3.5
# ---------------------------------------------------------------------

FAILURE_THRESHOLD = 5
FAILURE_WINDOW_SECONDS = 60
OPEN_DURATION_SECONDS = 60

STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_HALF_OPEN = "half_open"

# Floor TTL on Redis keys so stale records don't linger forever.
_STATE_TTL_SECONDS = 24 * 60 * 60


def _redis_key(endpoint_id: int, suffix: str) -> str:
    return f"luciel:byo:cb:{endpoint_id}:{suffix}"


# ---------------------------------------------------------------------
# Backend protocol — Redis in production, InMemory in tests
# ---------------------------------------------------------------------


class Backend(Protocol):
    def get(self, key: str) -> Optional[str]: ...
    def set(self, key: str, value: str, ttl_seconds: int) -> None: ...
    def delete(self, key: str) -> None: ...
    def incr_with_ttl(self, key: str, ttl_seconds: int) -> int: ...
    def set_nx_with_ttl(
        self, key: str, value: str, ttl_seconds: int
    ) -> bool: ...


class InMemoryBackend:
    """Test backend — single-process dict. Not thread-safe; tests
    drive the breaker serially."""

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

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._store[key] = (value, time.monotonic() + ttl_seconds)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def incr_with_ttl(self, key: str, ttl_seconds: int) -> int:
        cur = self.get(key)
        n = int(cur) + 1 if cur is not None else 1
        self.set(key, str(n), ttl_seconds)
        return n

    def set_nx_with_ttl(
        self, key: str, value: str, ttl_seconds: int
    ) -> bool:
        if self.get(key) is not None:
            return False
        self.set(key, value, ttl_seconds)
        return True


class RedisBackend:
    """Production backend — wraps a ``redis.Redis`` client.

    Construct via ``RedisBackend.from_settings()`` so the rest of
    the codebase does not need to thread a client through. The
    underlying client uses the URL at ``settings.redis_url`` — same
    as the Celery broker + readiness probe.
    """

    def __init__(self, client) -> None:  # type: ignore[no-untyped-def]
        self._client = client

    @classmethod
    def from_settings(cls) -> "RedisBackend":
        import redis  # local import — module stays importable in tests

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
            logger.exception("CircuitBreaker Redis get(%s) failed", key)
            return None
        if val is None:
            return None
        # decode_responses=True returns str, but guard for older clients.
        return val if isinstance(val, str) else val.decode("utf-8")

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        try:
            self._client.set(key, value, ex=ttl_seconds)
        except Exception:  # noqa: BLE001
            logger.exception("CircuitBreaker Redis set(%s) failed", key)

    def delete(self, key: str) -> None:
        try:
            self._client.delete(key)
        except Exception:  # noqa: BLE001
            logger.exception(
                "CircuitBreaker Redis delete(%s) failed", key
            )

    def incr_with_ttl(self, key: str, ttl_seconds: int) -> int:
        try:
            pipe = self._client.pipeline()
            pipe.incr(key)
            pipe.expire(key, ttl_seconds)
            results = pipe.execute()
            return int(results[0])
        except Exception:  # noqa: BLE001
            logger.exception(
                "CircuitBreaker Redis incr(%s) failed", key
            )
            return 0  # fail-open on Redis outage — better to allow
            # the dispatch than block all BYO traffic.

    def set_nx_with_ttl(
        self, key: str, value: str, ttl_seconds: int
    ) -> bool:
        try:
            return bool(
                self._client.set(
                    key, value, nx=True, ex=ttl_seconds
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "CircuitBreaker Redis SETNX(%s) failed", key
            )
            return True  # fail-open: allow the probe rather than
            # block forever.


# ---------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------


class CircuitOpenError(Exception):
    """Raised by the dispatch path when the breaker is open and the
    probe lock is not available."""


# ---------------------------------------------------------------------
# Public breaker
# ---------------------------------------------------------------------


@dataclass
class BreakerSnapshot:
    """State seen at the dispatch attempt — recorded in the audit
    row."""

    state: str  # one of STATE_*
    failure_count: int


class CircuitBreaker:
    """Per-endpoint circuit breaker.

    Construct one per process; identify endpoints by ``endpoint_id``
    (the BYO row PK, NOT the URL). Three public verbs:

      * ``before_dispatch(endpoint_id)`` — call BEFORE each dispatch
        attempt. Returns a ``BreakerSnapshot`` (the state visible to
        the dispatch) OR raises ``CircuitOpenError`` if the breaker
        is open and no probe slot is available. When the breaker is
        ``half_open`` this acquires the probe lock so only one
        dispatch proceeds at a time.

      * ``record_success(endpoint_id)`` — call AFTER a successful
        dispatch (output schema validated, 2xx response). Resets
        the failure counter and closes the breaker.

      * ``record_failure(endpoint_id)`` — call AFTER a TRANSPORT
        failure (connect / timeout / TLS). Increments the rolling
        failure counter; trips the breaker open if the count
        reaches the threshold inside the window.

    Schema-validation failures and HTTP-4xx responses MUST NOT call
    ``record_failure`` — those are terminal tool failures and a 4xx
    is not an availability signal. The dispatch path enforces this.
    """

    def __init__(
        self,
        backend: Optional[Backend] = None,
        *,
        failure_threshold: int = FAILURE_THRESHOLD,
        failure_window_seconds: int = FAILURE_WINDOW_SECONDS,
        open_duration_seconds: int = OPEN_DURATION_SECONDS,
        now_fn=None,
    ) -> None:
        self._backend: Backend = backend or InMemoryBackend()
        self._failure_threshold = failure_threshold
        self._failure_window = failure_window_seconds
        self._open_duration = open_duration_seconds
        # ``time.time`` rather than ``time.monotonic`` because the
        # value is shared across processes via Redis.
        self._now = now_fn or time.time

    # ------------------------------------------------------------------
    # Read helpers (also useful for tests + audit)
    # ------------------------------------------------------------------

    def current_state(self, endpoint_id: int) -> str:
        """Best-effort read of the current state. Translates a stale
        ``open`` (now + open_duration past opened_at) into
        ``half_open`` lazily on read."""
        raw = self._backend.get(_redis_key(endpoint_id, "state"))
        if raw is None:
            return STATE_CLOSED
        if raw == STATE_OPEN:
            if self._open_window_expired(endpoint_id):
                return STATE_HALF_OPEN
            return STATE_OPEN
        return raw

    def failure_count(self, endpoint_id: int) -> int:
        raw = self._backend.get(_redis_key(endpoint_id, "fails"))
        if raw is None:
            return 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------------
    # Dispatch verbs
    # ------------------------------------------------------------------

    def before_dispatch(self, endpoint_id: int) -> BreakerSnapshot:
        """Gate the dispatch; raise ``CircuitOpenError`` if open."""
        state = self.current_state(endpoint_id)
        fail_count = self.failure_count(endpoint_id)

        if state == STATE_CLOSED:
            return BreakerSnapshot(
                state=STATE_CLOSED, failure_count=fail_count
            )

        if state == STATE_HALF_OPEN:
            # Acquire the probe lock — only one dispatch through at
            # a time. If another worker has the lock, we treat the
            # breaker as still open.
            got = self._backend.set_nx_with_ttl(
                _redis_key(endpoint_id, "probe_lock"),
                "1",
                ttl_seconds=max(5, self._open_duration),
            )
            if not got:
                raise CircuitOpenError(
                    f"BYO endpoint {endpoint_id} circuit half-open; "
                    "probe slot already in use."
                )
            return BreakerSnapshot(
                state=STATE_HALF_OPEN, failure_count=fail_count
            )

        # state == STATE_OPEN and window hasn't expired
        raise CircuitOpenError(
            f"BYO endpoint {endpoint_id} circuit is open."
        )

    def record_success(self, endpoint_id: int) -> None:
        """Successful dispatch — close the breaker, reset counters,
        release any probe lock."""
        self._backend.delete(_redis_key(endpoint_id, "fails"))
        self._backend.delete(_redis_key(endpoint_id, "opened_at"))
        self._backend.delete(_redis_key(endpoint_id, "probe_lock"))
        self._backend.set(
            _redis_key(endpoint_id, "state"),
            STATE_CLOSED,
            ttl_seconds=_STATE_TTL_SECONDS,
        )

    def record_failure(self, endpoint_id: int) -> None:
        """Transport failure — bump the counter; trip open if
        threshold reached inside the window."""
        new_count = self._backend.incr_with_ttl(
            _redis_key(endpoint_id, "fails"),
            ttl_seconds=self._failure_window,
        )
        # Release any probe lock so a half-open failure correctly
        # re-opens the breaker for the next attempt.
        self._backend.delete(_redis_key(endpoint_id, "probe_lock"))
        if new_count >= self._failure_threshold:
            self._trip_open(endpoint_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _trip_open(self, endpoint_id: int) -> None:
        now_ts = str(self._now())
        self._backend.set(
            _redis_key(endpoint_id, "state"),
            STATE_OPEN,
            ttl_seconds=_STATE_TTL_SECONDS,
        )
        self._backend.set(
            _redis_key(endpoint_id, "opened_at"),
            now_ts,
            ttl_seconds=_STATE_TTL_SECONDS,
        )

    def _open_window_expired(self, endpoint_id: int) -> bool:
        raw = self._backend.get(_redis_key(endpoint_id, "opened_at"))
        if raw is None:
            # No opened_at means we treat the open state as stale.
            return True
        try:
            opened_at = float(raw)
        except (TypeError, ValueError):
            return True
        return (self._now() - opened_at) >= self._open_duration


__all__ = [
    "Backend",
    "BreakerSnapshot",
    "CircuitBreaker",
    "CircuitOpenError",
    "InMemoryBackend",
    "RedisBackend",
    "FAILURE_THRESHOLD",
    "FAILURE_WINDOW_SECONDS",
    "OPEN_DURATION_SECONDS",
    "STATE_CLOSED",
    "STATE_HALF_OPEN",
    "STATE_OPEN",
]
