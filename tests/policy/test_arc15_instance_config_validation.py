"""Arc 15 WU1 — tier-conditional pillar validation.

Covers ``app.policy.instance_config`` — the single place the
tier-conditional rules live (the create route, the PATCH route, and the
WU3 personality API all call ``validate_pillars_for_tier`` so they
agree). Structural validity is a Pydantic concern; THESE rules need the
server-resolved tier and so cannot live on the request schema.
"""
from __future__ import annotations

from app.persona.presets import (
    PRESET_CUSTOM,
    PRESET_WARM_CONCIERGE,
)
from app.policy.entitlements import TIER_FREE, TIER_PRO
from app.policy.instance_config import (
    check_business_context_length,
    check_custom_preset_allowed,
    check_lead_routing_allowed,
    validate_pillars_for_tier,
)


# ---------------------------------------------------------------------
# business_context length
# ---------------------------------------------------------------------


def test_business_context_under_cap_ok() -> None:
    assert check_business_context_length(tier=TIER_FREE, business_context="x" * 280) == []


def test_business_context_over_free_cap_rejected() -> None:
    problems = check_business_context_length(
        tier=TIER_FREE, business_context="x" * 281
    )
    assert len(problems) == 1
    assert problems[0]["field"] == "business_context"
    assert problems[0]["reason"] == "too_long_for_tier"
    assert problems[0]["max_chars"] == 280


def test_business_context_none_is_ok() -> None:
    assert check_business_context_length(tier=TIER_FREE, business_context=None) == []
    assert check_business_context_length(tier=TIER_FREE, business_context="") == []


# ---------------------------------------------------------------------
# custom preset gate
# ---------------------------------------------------------------------


def test_custom_preset_rejected_on_free() -> None:
    problems = check_custom_preset_allowed(
        tier=TIER_FREE, personality_preset=PRESET_CUSTOM
    )
    assert len(problems) == 1
    assert problems[0]["field"] == "personality_preset"
    assert problems[0]["reason"] == "custom_preset_not_available_on_tier"
    assert problems[0]["upgrade_required"] is True


def test_custom_preset_allowed_on_pro() -> None:
    assert (
        check_custom_preset_allowed(tier=TIER_PRO, personality_preset=PRESET_CUSTOM)
        == []
    )


def test_named_preset_ok_on_free() -> None:
    assert (
        check_custom_preset_allowed(
            tier=TIER_FREE, personality_preset=PRESET_WARM_CONCIERGE
        )
        == []
    )


def test_no_preset_supplied_is_ok() -> None:
    assert check_custom_preset_allowed(tier=TIER_FREE, personality_preset=None) == []


# ---------------------------------------------------------------------
# lead_routing gate
# ---------------------------------------------------------------------


def test_lead_routing_rejected_on_free() -> None:
    problems = check_lead_routing_allowed(
        tier=TIER_FREE, lead_routing={"strategy": "round_robin", "rules": []}
    )
    assert len(problems) == 1
    assert problems[0]["field"] == "lead_routing"
    assert problems[0]["reason"] == "lead_routing_not_available_on_tier"


def test_lead_routing_allowed_on_pro() -> None:
    assert (
        check_lead_routing_allowed(
            tier=TIER_PRO, lead_routing={"strategy": "round_robin", "rules": []}
        )
        == []
    )


def test_lead_routing_none_ok_on_free() -> None:
    assert check_lead_routing_allowed(tier=TIER_FREE, lead_routing=None) == []


# ---------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------


def test_validate_pillars_clean_free() -> None:
    assert (
        validate_pillars_for_tier(
            tier=TIER_FREE,
            personality_preset=PRESET_WARM_CONCIERGE,
            business_context="short",
            lead_routing=None,
        )
        == []
    )


def test_validate_pillars_free_accumulates_all_problems() -> None:
    problems = validate_pillars_for_tier(
        tier=TIER_FREE,
        personality_preset=PRESET_CUSTOM,
        business_context="x" * 500,
        lead_routing={"strategy": "round_robin", "rules": []},
    )
    reasons = {p["reason"] for p in problems}
    assert reasons == {
        "custom_preset_not_available_on_tier",
        "too_long_for_tier",
        "lead_routing_not_available_on_tier",
    }


def test_validate_pillars_pro_all_allowed() -> None:
    assert (
        validate_pillars_for_tier(
            tier=TIER_PRO,
            personality_preset=PRESET_CUSTOM,
            business_context="x" * 280,
            lead_routing={"strategy": "round_robin", "rules": []},
        )
        == []
    )
