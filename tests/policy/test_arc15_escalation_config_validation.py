"""Arc 15 WU3 — escalation-CONTACT config validation (Vision §3.4).

Covers ``app.policy.escalation_config``: the tier-conditional contact /
routing / chain rules AND the security-critical hard guard that NO
payload may configure WHEN to escalate (the four escalation signals are
fixed runtime cognition, not admin-configurable).
"""
from __future__ import annotations

from app.policy.entitlements import TIER_FREE, TIER_PRO
from app.policy.escalation_config import (
    ESCALATION_SIGNALS,
    check_no_trigger_config,
    check_routing_rules,
    validate_escalation_config_for_tier,
)


# ---------------------------------------------------------------------
# Hard guard — escalation triggers are NEVER configurable.
# ---------------------------------------------------------------------


def test_signals_are_the_four_fixed_runtime_signals() -> None:
    assert ESCALATION_SIGNALS == frozenset(
        {
            "explicit_human_request",
            "cannot_confidently_answer",
            "strong_negative_sentiment",
            "high_value_lead",
        }
    )


def test_trigger_keys_rejected_on_every_tier() -> None:
    for tier in (TIER_FREE, TIER_PRO):
        for key in (
            "triggers",
            "thresholds",
            "conditions",
            "enabled_signals",
            "disabled_signals",
            "auto_escalate",
            "escalate_when",
            "when",
            "sensitivity",
            "confidence_threshold",
        ):
            problems = validate_escalation_config_for_tier(
                tier=tier, config={"primary_email": "a@b.com", key: True}
            )
            assert any(
                p["reason"] == "escalation_triggers_not_configurable"
                for p in problems
            ), (tier, key, problems)


def test_check_no_trigger_config_passes_clean_contact_shape() -> None:
    assert (
        check_no_trigger_config({"primary_email": "a@b.com"}) == []
    )
    assert (
        check_no_trigger_config(
            {
                "primary_contact": {"channel": "email", "value": "a@b.com"},
                "secondary_contact": {"channel": "sms", "value": "+15555550100"},
                "routing_rules": {},
                "chains": [],
            }
        )
        == []
    )


def test_unknown_top_level_key_rejected() -> None:
    # Enterprise removed; use Pro for this check.
    problems = validate_escalation_config_for_tier(
        tier=TIER_PRO, config={"primary_email": "a@b.com", "foo": 1}
    )
    assert any(p["reason"] == "unknown_field" for p in problems), problems


# ---------------------------------------------------------------------
# Free tier — primary_email only.
# ---------------------------------------------------------------------


def test_free_primary_email_ok() -> None:
    assert (
        validate_escalation_config_for_tier(
            tier=TIER_FREE, config={"primary_email": "ops@firm.com"}
        )
        == []
    )


def test_free_requires_a_primary_destination() -> None:
    problems = validate_escalation_config_for_tier(tier=TIER_FREE, config={})
    assert any(p["reason"] == "primary_contact_required" for p in problems)


def test_free_invalid_email_rejected() -> None:
    problems = validate_escalation_config_for_tier(
        tier=TIER_FREE, config={"primary_email": "not-an-email"}
    )
    assert any(p["reason"] == "invalid_email" for p in problems)


def test_free_secondary_contact_rejected() -> None:
    problems = validate_escalation_config_for_tier(
        tier=TIER_FREE,
        config={
            "primary_email": "a@b.com",
            "secondary_contact": {"channel": "email", "value": "b@c.com"},
        },
    )
    assert any(
        p["reason"] == "secondary_contact_not_available_on_tier"
        for p in problems
    )


def test_free_routing_rules_rejected() -> None:
    problems = validate_escalation_config_for_tier(
        tier=TIER_FREE,
        config={
            "primary_email": "a@b.com",
            "routing_rules": {"high_value_lead": {"channel": "email"}},
        },
    )
    assert any(
        p["reason"] == "secondary_contact_not_available_on_tier"
        for p in problems
    )


# ---------------------------------------------------------------------
# Pro tier — primary + secondary + per-signal routing (email/sms).
# ---------------------------------------------------------------------


def test_pro_primary_and_secondary_and_routing_ok() -> None:
    problems = validate_escalation_config_for_tier(
        tier=TIER_PRO,
        config={
            "primary_contact": {"channel": "email", "value": "a@b.com"},
            "secondary_contact": {"channel": "sms", "value": "+15555550100"},
            "routing_rules": {
                "explicit_human_request": {"channel": "sms"},
                "high_value_lead": {"channel": "email"},
            },
        },
    )
    assert problems == [], problems


def test_pro_routing_unknown_signal_rejected() -> None:
    problems = validate_escalation_config_for_tier(
        tier=TIER_PRO,
        config={
            "primary_contact": {"channel": "email", "value": "a@b.com"},
            "routing_rules": {"made_up_signal": {"channel": "sms"}},
        },
    )
    assert any(p["reason"] == "unknown_escalation_signal" for p in problems)


def test_pro_chains_rejected() -> None:
    problems = validate_escalation_config_for_tier(
        tier=TIER_PRO,
        config={
            "primary_contact": {"channel": "email", "value": "a@b.com"},
            "chains": [
                {"contact": {"channel": "sms", "value": "+1"}, "sla_minutes": 10}
            ],
        },
    )
    assert any(p["reason"] == "chains_not_available_on_tier" for p in problems)


# ---------------------------------------------------------------------
# check_routing_rules unit-level.
# ---------------------------------------------------------------------


def test_check_routing_rules_none_ok() -> None:
    assert check_routing_rules(routing_rules=None, tier=TIER_PRO) == []


def test_check_routing_rules_all_four_signals_ok() -> None:
    rr = {sig: {"channel": "email"} for sig in ESCALATION_SIGNALS}
    assert check_routing_rules(routing_rules=rr, tier=TIER_PRO) == []
