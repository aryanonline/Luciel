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

Write-through (Unit 13g, §4.5 founder ruling)
--------------------------------------------
The §4.5 ruling makes Postgres the SOURCE OF TRUTH and Redis a cache. A
``PostgresCounterStore`` (below) holds the authoritative per-period count
(``conversation_budget_counter``) plus a per-session idempotency row
(``conversation_counted_sessions``). When a ``BudgetMeter`` is built with
a ``counter_store``:

* WRITE: the Postgres per-session idempotency row (inserted ``ON CONFLICT
  DO NOTHING`` in the same transaction as the counter increment) is the
  EXACTLY-ONCE COMMIT POINT. Only when that row is newly inserted does the
  meter mirror the increment into the Redis hot counter. A re-fire of the
  same session — a REFLECT-loop iteration, or a Redis-outage retry after
  a prior Redis-path attempt — collides on the unique row and increments
  NEITHER store a second time. This is the no-double-charge guarantee.
* READ: Redis first (fast). If the Redis read fails/unavailable, fall back
  to the authoritative Postgres counter. The gate decision is identical
  whichever store served it.
* If the Postgres write fails it is a HARD error (source of truth) — it
  propagates; the meter does NOT silently fall back to Redis-only, because
  a Redis-only increment would not be durable and could double-count on a
  later Postgres-path retry.

The durable billing record of a closed period's overage remains the
Postgres ``conversation_overage_ledger`` table written at cycle close; the
write-through counter is the MID-PERIOD authoritative count. The Redis
counter carries a long TTL so a missed reset webhook self-heals.
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


class PostgresCounterStore:
    """Authoritative per-period conversation counter (Unit 13g, §4.5).

    Postgres is the source of truth (§4.5 line 1360); Redis is a cache. This
    store owns the two write-through tables:

    * ``conversation_budget_counter`` — UNIQUE(admin_id, instance_id,
      billing_period_start), the authoritative count.
    * ``conversation_counted_sessions`` — UNIQUE(admin_id, session_id), the
      per-session idempotency row that is the exactly-once commit point.

    Every method runs in ONE transaction that first sets the ``app.admin_id``
    RLS GUC to the operating tenant (``set_config(..., true)`` — transaction-
    scoped, clears on commit/rollback), so the store is self-contained: it
    does not depend on the request ContextVar having been set, and it can
    never touch another tenant's rows (the WITH CHECK policy rejects a
    mismatched insert; the USING policy hides a mismatched read).

    Construct via ``from_session_factory()`` so callers do not thread a
    sessionmaker through. The factory defaults to the app ``SessionLocal``.
    """

    def __init__(self, session_factory) -> None:  # type: ignore[no-untyped-def]
        self._session_factory = session_factory

    @classmethod
    def from_session_factory(cls, session_factory=None) -> "PostgresCounterStore":
        if session_factory is None:
            from app.db.session import SessionLocal

            session_factory = SessionLocal
        return cls(session_factory)

    @staticmethod
    def _bind_tenant(session, admin_id: str) -> None:
        from sqlalchemy import text

        session.execute(
            text("SELECT set_config('app.admin_id', :a, true)"),
            {"a": str(admin_id)},
        )

    def count_session_once(
        self,
        *,
        admin_id: str,
        instance_id: int,
        period_start: str,
        session_id: str,
    ) -> tuple[int, bool]:
        """Atomically count a session exactly once.

        Returns ``(count, newly_counted)`` where ``count`` is the
        authoritative post-state period count and ``newly_counted`` is True
        only on the first call for this (admin, session).

        In a single transaction:
          1. Bind the tenant GUC.
          2. INSERT the per-session idempotency row ``ON CONFLICT DO NOTHING``.
             A returned row means this session is NEWLY counted.
          3. Only if newly counted: upsert the period counter, incrementing
             ``conversation_count`` by 1.
          4. Read back and return the authoritative count.

        The whole thing commits atomically, so the idempotency row and the
        increment land together or not at all — a crash between them cannot
        leave a counted-but-not-incremented (or vice-versa) state. A re-fire
        of the same session collides on the unique row in step 2, skips the
        increment, and returns ``(unchanged_count, False)``. The
        ``newly_counted`` flag is the AUTHORITATIVE signal the meter uses to
        decide whether to mirror into Redis — never a racy before/after
        count comparison.
        """
        from sqlalchemy import text

        session = self._session_factory()
        try:
            self._bind_tenant(session, admin_id)
            inserted = session.execute(
                text(
                    "INSERT INTO conversation_counted_sessions "
                    "(admin_id, instance_id, billing_period_start, session_id, "
                    " created_at, updated_at) "
                    "VALUES (:admin_id, :instance_id, :period_start, :session_id, "
                    " now(), now()) "
                    "ON CONFLICT (admin_id, session_id) DO NOTHING "
                    "RETURNING id"
                ),
                {
                    "admin_id": admin_id,
                    "instance_id": instance_id,
                    "period_start": period_start,
                    "session_id": session_id,
                },
            ).first()

            newly = inserted is not None
            if newly:
                row = session.execute(
                    text(
                        "INSERT INTO conversation_budget_counter "
                        "(admin_id, instance_id, billing_period_start, "
                        " conversation_count, created_at, updated_at) "
                        "VALUES (:admin_id, :instance_id, :period_start, 1, "
                        " now(), now()) "
                        "ON CONFLICT (admin_id, instance_id, billing_period_start) "
                        "DO UPDATE SET conversation_count = "
                        "  conversation_budget_counter.conversation_count + 1, "
                        "  updated_at = now() "
                        "RETURNING conversation_count"
                    ),
                    {
                        "admin_id": admin_id,
                        "instance_id": instance_id,
                        "period_start": period_start,
                    },
                ).first()
                count = int(row[0])
            else:
                count = self._read_count(
                    session, admin_id, instance_id, period_start
                )

            session.commit()
            return count, newly
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _read_count(
        session, admin_id: str, instance_id: int, period_start: str
    ) -> int:
        from sqlalchemy import text

        row = session.execute(
            text(
                "SELECT conversation_count FROM conversation_budget_counter "
                "WHERE admin_id = :admin_id AND instance_id = :instance_id "
                "AND billing_period_start = :period_start"
            ),
            {
                "admin_id": admin_id,
                "instance_id": instance_id,
                "period_start": period_start,
            },
        ).first()
        return int(row[0]) if row is not None else 0

    def current_count(
        self, *, admin_id: str, instance_id: int, period_start: str
    ) -> int:
        session = self._session_factory()
        try:
            self._bind_tenant(session, admin_id)
            count = self._read_count(session, admin_id, instance_id, period_start)
            session.commit()
            return count
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def reset(self, *, admin_id: str, instance_id: int, period_start: str) -> None:
        """Delete the authoritative counter row for a closed period.

        Mirrors the Redis ``reset``: the NEW period uses a NEW period_start
        anchor, so dropping the old row is belt-and-suspenders cleanup. The
        per-session idempotency rows are NOT deleted here — they are bounded
        by the conversation lifetime and a closed period's sessions never
        re-fire, so they age out naturally (a retention sweep, if added, can
        prune by period).
        """
        from sqlalchemy import text

        session = self._session_factory()
        try:
            self._bind_tenant(session, admin_id)
            session.execute(
                text(
                    "DELETE FROM conversation_budget_counter "
                    "WHERE admin_id = :admin_id AND instance_id = :instance_id "
                    "AND billing_period_start = :period_start"
                ),
                {
                    "admin_id": admin_id,
                    "instance_id": instance_id,
                    "period_start": period_start,
                },
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


class BudgetMeter:
    """Counts conversations per (admin, instance, billing period).

    Construct one per process. Identifiers are the runtime request's
    ``admin_id`` (tenant), ``luciel_instance_id`` (per-instance scope),
    ``session_id`` (conversation), and the resolved ``period_start``
    (ISO string anchor).

    When a ``counter_store`` (``PostgresCounterStore``) is supplied, the
    meter is WRITE-THROUGH (Unit 13g, §4.5): Postgres is authoritative for
    both the exactly-once decision and the count; Redis is the hot cache.
    When ``counter_store`` is None the meter is Redis/InMemory-only — the
    pre-Unit-13g behavior, preserved for the unit tests that drive the meter
    deterministically without a live Postgres.
    """

    def __init__(
        self,
        backend: Optional[Backend] = None,
        counter_store: Optional[PostgresCounterStore] = None,
    ) -> None:
        self._backend = backend or RedisBackend.from_settings()
        self._counter_store = counter_store

    def count_session_once(
        self,
        *,
        admin_id: str,
        instance_id: int,
        period_start: str,
        session_id: str,
    ) -> int:
        """Increment the conversation counter once per session.

        Write-through mode (counter_store present): the Postgres store's
        per-session idempotency row is the exactly-once commit point. The
        store returns the authoritative post-increment count (or the
        unchanged count on a re-fire). On a NEWLY-counted session the meter
        mirrors the increment into the Redis hot counter so the fast read
        path stays warm; a Redis outage at mirror time is swallowed (the
        Redis incr fails-open) and Postgres remains correct — Redis
        self-heals on the next read-through. If the Postgres write raises,
        it propagates (hard error — source of truth).

        Redis-only mode (no counter_store): SETNX per-session marker then
        incr, exactly as pre-Unit-13g (idempotency, spec §23).

        Returns the current period count (post-increment on the first call;
        the unchanged count on subsequent calls within the same session).
        """
        if self._counter_store is not None:
            # Postgres is the authority for BOTH the idempotency decision
            # and the count. The store reports `newly` from its unique-row
            # INSERT (not a racy before/after comparison), so the mirror
            # fires exactly once per session even under concurrency.
            count, newly = self._counter_store.count_session_once(
                admin_id=admin_id,
                instance_id=instance_id,
                period_start=period_start,
                session_id=session_id,
            )
            if newly:
                # Mirror into the Redis hot cache. Best-effort: a Redis
                # outage here is swallowed by the fail-open backend and
                # Postgres stays correct — the cache self-heals on the next
                # read-through (Redis miss → authoritative Postgres read).
                self._backend.set_nx_with_ttl(
                    _session_marker_key(session_id),
                    "1",
                    _SESSION_MARKER_TTL_SECONDS,
                )
                self._backend.incr_with_ttl(
                    _counter_key(admin_id, instance_id, period_start),
                    _COUNTER_TTL_SECONDS,
                )
            return count

        # Redis/InMemory-only mode (pre-Unit-13g behavior).
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
        """Read the period count.

        Write-through mode: Redis first (fast). If the Redis read fails or
        returns nothing while Postgres holds a value, fall back to the
        authoritative Postgres counter (§4.5 line 1360). The gate decision
        is identical whichever store served it.

        Redis-only mode: read Redis (0 when absent).
        """
        cur = self._backend.get(_counter_key(admin_id, instance_id, period_start))
        if cur is not None:
            return int(cur)
        if self._counter_store is not None:
            # Redis miss/outage → authoritative Postgres read.
            return self._counter_store.current_count(
                admin_id=admin_id, instance_id=instance_id, period_start=period_start
            )
        return 0

    def reset(self, *, admin_id: str, instance_id: int, period_start: str) -> None:
        """Reset the counter for a closed billing period.

        Called at cycle close (invoice.paid / subscription.renewed). The
        NEW period uses a NEW ``period_start`` in the key, so deleting the
        old key is belt-and-suspenders cleanup rather than the reset
        mechanism itself. Write-through mode also drops the authoritative
        Postgres counter row for the closed period.
        """
        self._backend.delete(_counter_key(admin_id, instance_id, period_start))
        if self._counter_store is not None:
            self._counter_store.reset(
                admin_id=admin_id, instance_id=instance_id, period_start=period_start
            )

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
