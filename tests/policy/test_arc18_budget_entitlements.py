"""Arc 18 — budget/overage entitlement resolution per (tier, cadence).

Ratified tier matrix (Vision §7 + Open Decision #6 RESOLVED; Architecture
§3.4.1b + Locked Decision #15; Legal §A2; Customer Journey): Free 200
(hard cap, no overage), Pro Monthly 1,000 ($35/100), Pro Annual 1,200
($30/100). (Enterprise deferred -- Open Decision #8 -- excised in Unit 1.)
These tests pin those values and the fail-closed behaviour for unknown
tiers/cadences.
"""
from __future__ import annotations

import unittest

from app.policy.entitlements import (
    CADENCE_ANNUAL,
    CADENCE_MONTHLY,
    TIER_FREE,
    TIER_PRO,
    budget_alert_channels,
    budget_overage_billed,
    conversation_budget,
    overage_price_config_key,
    overage_rate_per_100_cents,
)


class TestConversationBudget(unittest.TestCase):

    def test_ratified_caps(self):
        # Enterprise removed (Unit 1 excision). Free/Pro only.
        self.assertEqual(conversation_budget(TIER_FREE, CADENCE_MONTHLY), 200)
        self.assertEqual(conversation_budget(TIER_FREE, CADENCE_ANNUAL), 200)
        self.assertEqual(conversation_budget(TIER_PRO, CADENCE_MONTHLY), 1000)
        self.assertEqual(conversation_budget(TIER_PRO, CADENCE_ANNUAL), 1200)

    def test_unknown_tier_fails_closed_to_free_cap(self):
        self.assertEqual(conversation_budget("mystery", CADENCE_ANNUAL), 200)

    def test_unknown_cadence_normalises_to_monthly(self):
        # Pro + garbage cadence → monthly cap (the conservative 1000).
        self.assertEqual(conversation_budget(TIER_PRO, "weekly"), 1000)
        self.assertEqual(conversation_budget(TIER_PRO, None), 1000)


class TestOverageRate(unittest.TestCase):

    def test_rates_in_cents_per_100(self):
        self.assertEqual(overage_rate_per_100_cents(TIER_PRO, CADENCE_MONTHLY), 3500)
        self.assertEqual(overage_rate_per_100_cents(TIER_PRO, CADENCE_ANNUAL), 3000)

    def test_free_has_no_platform_rate(self):
        # Enterprise removed (Unit 1 excision).
        self.assertIsNone(overage_rate_per_100_cents(TIER_FREE, CADENCE_MONTHLY))

    def test_unknown_fails_closed_to_none(self):
        self.assertIsNone(overage_rate_per_100_cents("mystery", CADENCE_MONTHLY))

    def test_billed_predicate(self):
        self.assertFalse(budget_overage_billed(TIER_FREE))
        self.assertTrue(budget_overage_billed(TIER_PRO))


class TestOveragePriceConfigKey(unittest.TestCase):

    def test_pro_keys(self):
        self.assertEqual(
            overage_price_config_key(TIER_PRO, CADENCE_MONTHLY),
            "stripe_price_overage_pro_monthly",
        )
        self.assertEqual(
            overage_price_config_key(TIER_PRO, CADENCE_ANNUAL),
            "stripe_price_overage_pro_annual",
        )

    def test_free_has_no_fixed_price(self):
        # Enterprise removed (Unit 1 excision).
        self.assertIsNone(overage_price_config_key(TIER_FREE, CADENCE_MONTHLY))


class TestBudgetAlertChannels(unittest.TestCase):

    def test_free_only_emails_at_100(self):
        self.assertEqual(budget_alert_channels(TIER_FREE, 80), frozenset())
        self.assertEqual(budget_alert_channels(TIER_FREE, 100), frozenset({"email"}))

    def test_pro_email_at_80_email_sms_at_100(self):
        self.assertEqual(budget_alert_channels(TIER_PRO, 80), frozenset({"email"}))
        self.assertEqual(
            budget_alert_channels(TIER_PRO, 100), frozenset({"email", "sms"})
        )

    def test_unknown_tier_threshold_fails_closed_empty(self):
        self.assertEqual(budget_alert_channels("mystery", 80), frozenset())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
