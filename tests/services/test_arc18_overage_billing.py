"""Arc 18 — overage billing helpers: rounding + EXACT invoice line format.

``overage_units`` rounds raw overage UP to whole hundreds (the metered
Price is per-100, spec §34). ``overage_line_item_description`` must match
the spec string EXACTLY, including the em-dash (U+2014) and the
multiplication sign (U+00D7) — a byte-level assertion guards against an
ASCII-hyphen / lowercase-x regression.
"""
from __future__ import annotations

import unittest

from app.services.overage_billing import (
    OVERAGE_UNIT_SIZE,
    overage_count,
    overage_line_item_description,
    overage_units,
    rate_string_from_cents,
)


class TestOverageCount(unittest.TestCase):

    def test_under_cap_is_zero(self):
        self.assertEqual(overage_count(conversations_used=150, budget_cap=200), 0)
        self.assertEqual(overage_count(conversations_used=200, budget_cap=200), 0)

    def test_over_cap_is_raw_difference(self):
        self.assertEqual(overage_count(conversations_used=355, budget_cap=200), 155)


class TestOverageUnitsRounding(unittest.TestCase):

    def test_unit_size_is_100(self):
        self.assertEqual(OVERAGE_UNIT_SIZE, 100)

    def test_ceil_to_hundreds(self):
        self.assertEqual(overage_units(0), 0)
        self.assertEqual(overage_units(1), 1)
        self.assertEqual(overage_units(100), 1)
        self.assertEqual(overage_units(101), 2)
        self.assertEqual(overage_units(155), 2)
        self.assertEqual(overage_units(200), 2)
        self.assertEqual(overage_units(201), 3)


class TestRateString(unittest.TestCase):

    def test_formats_cents_per_100(self):
        # Ratified Pro rates (Locked Decision #15): $35/100 monthly, $30/100 annual.
        self.assertEqual(rate_string_from_cents(3500), "$35.00/100")
        self.assertEqual(rate_string_from_cents(3000), "$30.00/100")


class TestLineItemDescription(unittest.TestCase):

    def test_exact_format(self):
        desc = overage_line_item_description(
            instance_name="Acme Bot", additional=155, rate_str="$15.00/100"
        )
        self.assertEqual(
            desc,
            "Conversation overage — Acme Bot: 155 additional conversations "
            "× $15.00/100",
        )

    def test_uses_em_dash_and_times_sign_not_ascii(self):
        desc = overage_line_item_description(
            instance_name="X", additional=1, rate_str="$10.00/100"
        )
        self.assertIn("—", desc)  # em-dash
        self.assertIn("×", desc)  # multiplication sign
        # The raw-count Z (NOT the rounded units) is what appears.
        self.assertIn("1 additional conversations", desc)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
