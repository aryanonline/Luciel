"""Unit 13g — budget counter write-through (Redis hot + Postgres authoritative).

The §4.5 founder ruling makes Postgres the SOURCE OF TRUTH for the
conversation budget counter and Redis a hot cache. These tests prove the
load-bearing invariants of that write-through against a LIVE Postgres:

  (1) the Postgres counter increments EXACTLY ONCE per session across
      repeated fire attempts (idempotency — the no-double-charge invariant);
  (2) Redis + Postgres AGREE after a normal increment;
  (3) Redis-DOWN simulation: the gate reads the Postgres counter, the
      increment lands in Postgres, and there is NO double-count when Redis
      returns;
  (5) the at-cap path does NOT increment either store (the gate peeks
      pre-increment and short-circuits);
  (6) the human-controlled-before-model-call trigger fires the SINGLE
      increment, idempotent with the model-call path.

Cross-tenant isolation on the new tables (test 4) lives in
tests/isolation/test_unit13g_budget_counter_isolation.py as a NON-SUPERUSER
role test (the superuser SessionLocal here bypasses RLS).

These exercise the live Postgres counter store; Redis is driven by the
deterministic InMemoryBackend (no live Redis required), and a
``_DownBackend`` simulates a Redis outage by failing-open exactly as
``RedisBackend`` does.
"""
from __future__ import annotations

import os
import unittest
import uuid

os.environ.setdefault("MODERATION_PROVIDER", "null")

from app.billing.metering import (
    BudgetMeter,
    InMemoryBackend,
    PostgresCounterStore,
    _counter_key,
)

_DB_URL = os.environ.get("DATABASE_URL", "")
_LIVE = _DB_URL.startswith("postgresql+psycopg://") or bool(
    os.environ.get("LUCIEL_LIVE_POSTGRES_URL")
)


class _DownBackend:
    """A Redis backend that is DOWN — every op fails-open exactly like
    ``RedisBackend`` does on a connection error (get→None, incr→0,
    setnx→False, delete→no-op). Used to simulate a Redis outage at
    increment AND read time without a live Redis to kill."""

    def get(self, key):  # noqa: D401
        return None

    def delete(self, key) -> None:
        return None

    def incr_with_ttl(self, key, ttl_seconds) -> int:
        return 0

    def set_nx_with_ttl(self, key, value, ttl_seconds) -> bool:
        return False


@unittest.skipUnless(
    _LIVE,
    "Requires DATABASE_URL=postgresql+psycopg://... or LUCIEL_LIVE_POSTGRES_URL",
)
class TestBudgetWriteThrough(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.db.session import SessionLocal

        cls.SessionLocal = SessionLocal

    def setUp(self) -> None:
        from app.models.admin import Admin

        self.period = "2026-06-01"
        self.instance_id = 4242
        self.admin_id = f"u13g-wt-{uuid.uuid4().hex[:10]}"
        db = self.SessionLocal()
        try:
            db.add(
                Admin(id=self.admin_id, name="u13g wt", tier="pro", active=True)
            )
            db.commit()
        finally:
            db.close()

    def tearDown(self) -> None:
        self._purge(self.admin_id)

    def _purge(self, admin_id: str) -> None:
        from sqlalchemy import text

        db = self.SessionLocal()
        try:
            for tbl in (
                "conversation_counted_sessions",
                "conversation_budget_counter",
            ):
                db.execute(
                    text(f"DELETE FROM {tbl} WHERE admin_id = :a"),
                    {"a": admin_id},
                )
            db.execute(
                text("DELETE FROM admins WHERE id = :a"), {"a": admin_id}
            )
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

    def _store(self) -> PostgresCounterStore:
        return PostgresCounterStore.from_session_factory(self.SessionLocal)

    def _pg_count(self) -> int:
        return self._store().current_count(
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            period_start=self.period,
        )

    # ------------------------------------------------------------------
    # (1) Postgres counter increments EXACTLY ONCE per session across
    #     repeated fire attempts.
    # ------------------------------------------------------------------
    def test_postgres_counter_increments_exactly_once_per_session(self):
        meter = BudgetMeter(
            backend=InMemoryBackend(), counter_store=self._store()
        )
        session_id = f"s-{uuid.uuid4().hex}"
        first = meter.count_session_once(
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            period_start=self.period,
            session_id=session_id,
        )
        self.assertEqual(first, 1)
        # The REFLECT loop re-fires for the SAME session — no double count.
        for _ in range(5):
            again = meter.count_session_once(
                admin_id=self.admin_id,
                instance_id=self.instance_id,
                period_start=self.period,
                session_id=session_id,
            )
            self.assertEqual(again, 1)
        # The AUTHORITATIVE Postgres count is exactly 1.
        self.assertEqual(self._pg_count(), 1)

    # ------------------------------------------------------------------
    # (2) Redis + Postgres AGREE after a normal increment.
    # ------------------------------------------------------------------
    def test_redis_and_postgres_agree_after_increment(self):
        backend = InMemoryBackend()
        meter = BudgetMeter(backend=backend, counter_store=self._store())
        for i in range(3):
            meter.count_session_once(
                admin_id=self.admin_id,
                instance_id=self.instance_id,
                period_start=self.period,
                session_id=f"s-{i}-{uuid.uuid4().hex}",
            )
        redis_val = backend.get(
            _counter_key(self.admin_id, self.instance_id, self.period)
        )
        self.assertEqual(int(redis_val), 3)
        self.assertEqual(self._pg_count(), 3)
        # The meter's read-through returns the same value (Redis-served here).
        self.assertEqual(
            meter.current_count(
                admin_id=self.admin_id,
                instance_id=self.instance_id,
                period_start=self.period,
            ),
            3,
        )

    # ------------------------------------------------------------------
    # (3) Redis-DOWN: gate reads Postgres, increment lands in Postgres, no
    #     double-count when Redis returns.
    # ------------------------------------------------------------------
    def test_redis_down_falls_back_to_postgres_no_double_count(self):
        # Phase 1 — Redis is DOWN. The increment must still land in
        # Postgres, and the read must fall back to the Postgres counter.
        down_meter = BudgetMeter(
            backend=_DownBackend(), counter_store=self._store()
        )
        session_id = f"s-{uuid.uuid4().hex}"
        count = down_meter.count_session_once(
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            period_start=self.period,
            session_id=session_id,
        )
        self.assertEqual(count, 1)  # Postgres authoritative count.
        # Gate read with Redis still down → authoritative Postgres fallback.
        self.assertEqual(
            down_meter.current_count(
                admin_id=self.admin_id,
                instance_id=self.instance_id,
                period_start=self.period,
            ),
            1,
        )
        self.assertEqual(self._pg_count(), 1)

        # Phase 2 — Redis is BACK (fresh empty cache). A re-fire of the SAME
        # session must NOT increment Postgres a second time (the per-session
        # idempotency row is the cross-store authority). And because the
        # session was already counted, the mirror does NOT fire either, so
        # Redis is NOT bumped for an already-counted session.
        back_backend = InMemoryBackend()
        back_meter = BudgetMeter(
            backend=back_backend, counter_store=self._store()
        )
        recount = back_meter.count_session_once(
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            period_start=self.period,
            session_id=session_id,
        )
        self.assertEqual(recount, 1)  # still 1 — no double count.
        self.assertEqual(self._pg_count(), 1)

        # A genuinely NEW session under the recovered Redis increments both.
        new_count = back_meter.count_session_once(
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            period_start=self.period,
            session_id=f"s-{uuid.uuid4().hex}",
        )
        self.assertEqual(new_count, 2)
        self.assertEqual(self._pg_count(), 2)

    # ------------------------------------------------------------------
    # (5) At-cap path does NOT increment either store.
    # ------------------------------------------------------------------
    def test_at_cap_path_does_not_increment(self):
        # The gate peeks via current_count and short-circuits BEFORE calling
        # count_session_once. Model the at-cap decision: a pure read must
        # not mutate either store.
        backend = InMemoryBackend()
        meter = BudgetMeter(backend=backend, counter_store=self._store())
        # current_count is a pure peek — call it repeatedly, nothing counts.
        for _ in range(3):
            self.assertEqual(
                meter.current_count(
                    admin_id=self.admin_id,
                    instance_id=self.instance_id,
                    period_start=self.period,
                ),
                0,
            )
        self.assertEqual(self._pg_count(), 0)
        self.assertIsNone(
            backend.get(
                _counter_key(self.admin_id, self.instance_id, self.period)
            )
        )

    # ------------------------------------------------------------------
    # (6) Human-controlled-before-model-call fires the SINGLE increment,
    #     idempotent with the model-call path.
    # ------------------------------------------------------------------
    def test_human_controlled_then_model_call_counts_once(self):
        meter = BudgetMeter(
            backend=InMemoryBackend(), counter_store=self._store()
        )
        session_id = f"s-{uuid.uuid4().hex}"
        # Trigger (b): human_controlled-before-model-call counts the session.
        c1 = meter.count_session_once(
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            period_start=self.period,
            session_id=session_id,
        )
        self.assertEqual(c1, 1)
        # If the SAME session later reaches a model call (trigger a), the
        # session_id idempotency means it does NOT double-count.
        c2 = meter.count_session_once(
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            period_start=self.period,
            session_id=session_id,
        )
        self.assertEqual(c2, 1)
        self.assertEqual(self._pg_count(), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
