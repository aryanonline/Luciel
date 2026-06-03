"""Arc 18 — budget/overage entitlement resolution per (tier, cadence).

The founder ratified (2026-06-03): Free 200 (no overage), Pro monthly
2000 ($15/100), Pro annual 2500 ($10/100), Enterprise 10000 (per-contract,
no platform overage price). These tests pin those values and the
fail-closed behaviour for unknown tiers/cadences.
"""
from __future__ import annotations

import unittest

from app.policy.entitlements import (
    CADENCE_ANNUAL,
    CADENCE_MONTHLY,
    TIER_ENTERPRISE,
    TIER_FREE,
    TIER_PRO,
    budget_alert_channels,
    budget_csm_alert_at_80,
    budget_overage_billed,
    conversation_budget,
    overage_price_config_key,
    overage_rate_per_100_cents,
)


class TestConversationBudget(unittest.TestCase):

    def test_ratified_caps(self):
        self.assertEqual(conversation_budget(TIER_FREE, CADENCE_MONTHLY), 200)
        self.assertEqual(conversation_budget(TIER_FREE, CADENCE_ANNUAL), 200)
        self.assertEqual(conversation_budget(TIER_PRO, CADENCE_MONTHLY), 2000)
        self.assertEqual(conversation_budget(TIER_PRO, CADENCE_ANNUAL), 2500)
        self.assertEqual(conversation_budget(TIER_ENTERPRISE, CADENCE_MONTHLY), 10000)
        self.assertEqual(conversation_budget(TIER_ENTERPRISE, CADENCE_ANNUAL), 10000)

    def test_unknown_tier_fails_closed_to_free_cap(self):
        self.assertEqual(conversation_budget("mystery", CADENCE_ANNUAL), 200)

    def test_unknown_cadence_normalises_to_monthly(self):
        # Pro + garbage cadence → monthly cap (the conservative 2000).
        self.assertEqual(conversation_budget(TIER_PRO, "weekly"), 2000)
        self.assertEqual(conversation_budget(TIER_PRO, None), 2000)


class TestOverageRate(unittest.TestCase):

    def test_rates_in_cents_per_100(self):
        self.assertEqual(overage_rate_per_100_cents(TIER_PRO, CADENCE_MONTHLY), 1500)
        self.assertEqual(overage_rate_per_100_cents(TIER_PRO, CADENCE_ANNUAL), 1000)

    def test_free_and_enterprise_have_no_platform_rate(self):
        self.assertIsNone(overage_rate_per_100_cents(TIER_FREE, CADENCE_MONTHLY))
        self.assertIsNone(overage_rate_per_100_cents(TIER_ENTERPRISE, CADENCE_MONTHLY))
        self.assertIsNone(overage_rate_per_100_cents(TIER_ENTERPRISE, CADENCE_ANNUAL))

    def test_unknown_fails_closed_to_none(self):
        self.assertIsNone(overage_rate_per_100_cents("mystery", CADENCE_MONTHLY))

    def test_billed_predicate(self):
        self.assertFalse(budget_overage_billed(TIER_FREE))
        self.assertTrue(budget_overage_billed(TIER_PRO))
        self.assertTrue(budget_overage_billed(TIER_ENTERPRISE))


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

    def test_free_enterprise_have_no_fixed_price(self):
        self.assertIsNone(overage_price_config_key(TIER_FREE, CADENCE_MONTHLY))
        self.assertIsNone(overage_price_config_key(TIER_ENTERPRISE, CADENCE_MONTHLY))


class TestBudgetAlertChannels(unittest.TestCase):

    def test_free_only_emails_at_100(self):
        self.assertEqual(budget_alert_channels(TIER_FREE, 80), frozenset())
        self.assertEqual(budget_alert_channels(TIER_FREE, 100), frozenset({"email"}))

    def test_pro_email_at_80_email_sms_at_100(self):
        self.assertEqual(budget_alert_channels(TIER_PRO, 80), frozenset({"email"}))
        self.assertEqual(
            budget_alert_channels(TIER_PRO, 100), frozenset({"email", "sms"})
        )

    def test_enterprise_csm_flag_at_80(self):
        self.assertTrue(budget_csm_alert_at_80(TIER_ENTERPRISE))
        self.assertFalse(budget_csm_alert_at_80(TIER_PRO))
        self.assertFalse(budget_csm_alert_at_80(TIER_FREE))

    def test_unknown_tier_threshold_fails_closed_empty(self):
        self.assertEqual(budget_alert_channels("mystery", 80), frozenset())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
