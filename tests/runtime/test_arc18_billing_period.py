"""Arc 18 — Free billing-period anchor (§3.4.1b edge case).

Free has no Stripe subscription, so the period_start is anchored to the
admin's signup day-of-month and rolls monthly WITHOUT a webhook. These
tests pin that the anchor is deterministic, signup-day based (NOT a flat
calendar-month-start), and clamps for short months.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.runtime.billing_period import free_period_start, period_start_iso


class TestFreePeriodStart(unittest.TestCase):

    def test_anchors_to_signup_day_of_month(self):
        signup = datetime(2026, 1, 12, tzinfo=timezone.utc)
        now = datetime(2026, 6, 20, tzinfo=timezone.utc)
        # Most recent 12th at-or-before June 20 → June 12.
        self.assertEqual(free_period_start(signup, now=now), "2026-06-12")

    def test_uses_prior_month_when_anchor_day_not_yet_reached(self):
        signup = datetime(2026, 1, 25, tzinfo=timezone.utc)
        now = datetime(2026, 6, 10, tzinfo=timezone.utc)
        # The 25th hasn't arrived in June → May 25.
        self.assertEqual(free_period_start(signup, now=now), "2026-05-25")

    def test_clamps_to_short_month_end(self):
        signup = datetime(2026, 1, 31, tzinfo=timezone.utc)
        now = datetime(2026, 2, 28, tzinfo=timezone.utc)
        # The 31st clamps to Feb 28 (2026 is not a leap year).
        self.assertEqual(free_period_start(signup, now=now), "2026-02-28")

    def test_is_not_a_flat_calendar_month_start(self):
        # A mid-month signup must NOT collapse to day=1 (that would be the
        # forbidden flat calendar anchor).
        signup = datetime(2025, 3, 17, tzinfo=timezone.utc)
        now = datetime(2026, 6, 20, tzinfo=timezone.utc)
        anchor = free_period_start(signup, now=now)
        self.assertTrue(anchor.endswith("-17"), anchor)

    def test_january_rollover_to_prior_december(self):
        signup = datetime(2025, 1, 28, tzinfo=timezone.utc)
        now = datetime(2026, 1, 10, tzinfo=timezone.utc)
        # Jan 28 not reached → Dec 28 of the prior year.
        self.assertEqual(free_period_start(signup, now=now), "2025-12-28")


class TestPeriodStartIso(unittest.TestCase):

    def test_normalises_datetime_to_utc_date(self):
        dt = datetime(2026, 6, 1, 13, 45, tzinfo=timezone.utc)
        self.assertEqual(period_start_iso(dt), "2026-06-01")

    def test_none_falls_back_to_epoch_date(self):
        self.assertEqual(period_start_iso(None), "1970-01-01")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
