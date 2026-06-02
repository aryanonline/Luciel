"""Arc 15 WU1 — InstanceCreate/Update structural pillar validation.

These test the STRUCTURAL (Pydantic) layer only. Tier-conditional rules
(custom on Free → 403, business_context cap per tier, lead_routing
Pro/Ent only) live at the API layer and are covered separately.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.instance import InstanceCreate, InstanceUpdate


def _base_create(**overrides):
    payload = {
        "admin_id": "acme",
        "instance_slug": "main",
        "display_name": "Acme Main",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------
# defaults
# ---------------------------------------------------------------------


def test_create_defaults_preset_warm_concierge() -> None:
    model = InstanceCreate(**_base_create())
    assert model.personality_preset == "warm_concierge"
    assert model.personality_axes is None
    assert model.business_context is None
    assert model.lead_routing is None
    assert model.website is None


# ---------------------------------------------------------------------
# personality_axes cross-field rule (structural)
# ---------------------------------------------------------------------


def test_create_named_preset_rejects_axes() -> None:
    with pytest.raises(ValidationError):
        InstanceCreate(
            **_base_create(
                personality_preset="warm_concierge",
                personality_axes={
                    "tone": "warm",
                    "verbosity": "balanced",
                    "formality": "casual",
                    "pace": "relaxed",
                },
            )
        )


def test_create_custom_requires_axes() -> None:
    with pytest.raises(ValidationError):
        InstanceCreate(**_base_create(personality_preset="custom"))


def test_create_custom_with_valid_axes_ok() -> None:
    model = InstanceCreate(
        **_base_create(
            personality_preset="custom",
            personality_axes={
                "tone": "authoritative",
                "verbosity": "concise",
                "formality": "formal",
                "pace": "brisk",
            },
        )
    )
    assert model.personality_preset == "custom"


def test_create_custom_with_invalid_axis_value_rejected() -> None:
    with pytest.raises(ValidationError):
        InstanceCreate(
            **_base_create(
                personality_preset="custom",
                personality_axes={
                    "tone": "sassy",
                    "verbosity": "concise",
                    "formality": "formal",
                    "pace": "brisk",
                },
            )
        )


# ---------------------------------------------------------------------
# lead_routing shape
# ---------------------------------------------------------------------


def test_create_lead_routing_valid_strategy() -> None:
    model = InstanceCreate(
        **_base_create(lead_routing={"strategy": "round_robin", "rules": []})
    )
    assert model.lead_routing["strategy"] == "round_robin"


def test_create_lead_routing_bad_strategy_rejected() -> None:
    with pytest.raises(ValidationError):
        InstanceCreate(
            **_base_create(lead_routing={"strategy": "telepathy", "rules": []})
        )


def test_create_lead_routing_rules_must_be_list() -> None:
    with pytest.raises(ValidationError):
        InstanceCreate(
            **_base_create(
                lead_routing={"strategy": "round_robin", "rules": "nope"}
            )
        )


# ---------------------------------------------------------------------
# business_context structural ceiling (2000)
# ---------------------------------------------------------------------


def test_create_business_context_over_structural_max_rejected() -> None:
    with pytest.raises(ValidationError):
        InstanceCreate(**_base_create(business_context="x" * 2001))


def test_create_business_context_at_structural_max_ok() -> None:
    model = InstanceCreate(**_base_create(business_context="x" * 2000))
    assert len(model.business_context) == 2000


# ---------------------------------------------------------------------
# Update — partial semantics
# ---------------------------------------------------------------------


def test_update_axes_without_preset_not_cross_validated_here() -> None:
    # Schema cannot know the stored preset; defers to API layer.
    model = InstanceUpdate(
        personality_axes={
            "tone": "warm",
            "verbosity": "balanced",
            "formality": "casual",
            "pace": "relaxed",
        }
    )
    assert model.personality_preset is None


def test_update_custom_preset_requires_axes() -> None:
    with pytest.raises(ValidationError):
        InstanceUpdate(personality_preset="custom")


def test_update_named_preset_rejects_axes() -> None:
    with pytest.raises(ValidationError):
        InstanceUpdate(
            personality_preset="professional_advisor",
            personality_axes={
                "tone": "neutral",
                "verbosity": "balanced",
                "formality": "professional",
                "pace": "measured",
            },
        )


def test_update_empty_is_valid() -> None:
    model = InstanceUpdate()
    assert model.personality_preset is None
