"""Arc 18 — BudgetMeter unit tests (InMemoryBackend, no live Redis).

Covers the load-bearing invariants of the conversation counter:

  * A multi-iteration session is counted EXACTLY ONCE (idempotency across
    the PLAN→ACT→REFLECT loop, spec §23).
  * Two instances under the same admin count INDEPENDENTLY (per-instance
    scope, §3.4.1b).
  * Two billing periods count independently (the period_start is part of
    the key, so a reset/advance starts a fresh count).
  * ``reset`` zeroes the open period; ``mark_alert_fired_once`` is a
    once-per-(instance, period, threshold) latch.
"""
from __future__ import annotations

import unittest

from app.billing.metering import BudgetMeter, InMemoryBackend


def _meter() -> BudgetMeter:
    return BudgetMeter(backend=InMemoryBackend())


class TestBudgetMeterIdempotency(unittest.TestCase):

    def test_session_counted_exactly_once_across_iterations(self):
        meter = _meter()
        first = meter.count_session_once(
            admin_id="a1", instance_id=7, period_start="2026-06-01", session_id="s1"
        )
        self.assertEqual(first, 1)
        # The REFLECT loop calls again for the SAME session — no double count.
        for _ in range(5):
            again = meter.count_session_once(
                admin_id="a1", instance_id=7, period_start="2026-06-01",
                session_id="s1",
            )
            self.assertEqual(again, 1)
        self.assertEqual(
            meter.current_count(admin_id="a1", instance_id=7, period_start="2026-06-01"),
            1,
        )

    def test_distinct_sessions_each_increment(self):
        meter = _meter()
        for i in range(3):
            meter.count_session_once(
                admin_id="a1", instance_id=7, period_start="2026-06-01",
                session_id=f"s{i}",
            )
        self.assertEqual(
            meter.current_count(admin_id="a1", instance_id=7, period_start="2026-06-01"),
            3,
        )


class TestBudgetMeterIsolation(unittest.TestCase):

    def test_two_instances_independent(self):
        meter = _meter()
        meter.count_session_once(
            admin_id="a1", instance_id=7, period_start="2026-06-01", session_id="s1"
        )
        meter.count_session_once(
            admin_id="a1", instance_id=8, period_start="2026-06-01", session_id="s2"
        )
        self.assertEqual(
            meter.current_count(admin_id="a1", instance_id=7, period_start="2026-06-01"),
            1,
        )
        self.assertEqual(
            meter.current_count(admin_id="a1", instance_id=8, period_start="2026-06-01"),
            1,
        )

    def test_two_periods_independent(self):
        meter = _meter()
        meter.count_session_once(
            admin_id="a1", instance_id=7, period_start="2026-05-01", session_id="s1"
        )
        # New period anchor → fresh count (this is how a reset "zeroes").
        self.assertEqual(
            meter.current_count(admin_id="a1", instance_id=7, period_start="2026-06-01"),
            0,
        )


class TestBudgetMeterResetAndAlerts(unittest.TestCase):

    def test_reset_zeroes_open_period(self):
        meter = _meter()
        meter.count_session_once(
            admin_id="a1", instance_id=7, period_start="2026-06-01", session_id="s1"
        )
        meter.reset(admin_id="a1", instance_id=7, period_start="2026-06-01")
        self.assertEqual(
            meter.current_count(admin_id="a1", instance_id=7, period_start="2026-06-01"),
            0,
        )

    def test_alert_latch_fires_once_per_threshold(self):
        meter = _meter()
        kw = dict(admin_id="a1", instance_id=7, period_start="2026-06-01")
        self.assertTrue(meter.mark_alert_fired_once(threshold=80, **kw))
        self.assertFalse(meter.mark_alert_fired_once(threshold=80, **kw))
        # 100 is a distinct latch.
        self.assertTrue(meter.mark_alert_fired_once(threshold=100, **kw))
        self.assertFalse(meter.mark_alert_fired_once(threshold=100, **kw))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
