"""Arc 15 WU2 — persona composer (§3.5.1).

Each named preset yields a stable PRESET stanza; custom uses the stored
axes; business_context is length-capped per tier BEFORE composition; the
composer exposes NO raw-stanza hook (it only renders curated presets /
bounded axes / framed background context).
"""
from __future__ import annotations

from app.persona.composer import (
    compose_business_context_stanza,
    compose_preset_stanza,
)
from app.persona.luciel_core import build_system_prompt
from app.persona.presets import NAMED_PRESETS, PRESET_CUSTOM
from app.policy.entitlements import (
    TIER_FREE,
    TIER_PRO,
    business_context_max_chars,
)


# ---------------------------------------------------------------------
# PRESET stanza
# ---------------------------------------------------------------------


def test_no_preset_returns_none() -> None:
    assert compose_preset_stanza(personality_preset=None) is None
    assert compose_preset_stanza(personality_preset="") is None


def test_every_named_preset_yields_stable_full_stanza() -> None:
    for preset in NAMED_PRESETS:
        stanza = compose_preset_stanza(personality_preset=preset)
        assert stanza is not None
        assert stanza.startswith("=== Personality ===")
        # All four axes present.
        for label in ("Tone:", "Verbosity:", "Formality:", "Pace:"):
            assert label in stanza, (preset, label)


def test_named_preset_is_deterministic() -> None:
    a = compose_preset_stanza(personality_preset="warm_concierge")
    b = compose_preset_stanza(personality_preset="warm_concierge")
    assert a == b


def test_distinct_presets_produce_distinct_stanzas() -> None:
    stanzas = {
        compose_preset_stanza(personality_preset=p) for p in NAMED_PRESETS
    }
    assert len(stanzas) == len(NAMED_PRESETS)


def test_custom_preset_uses_supplied_axes() -> None:
    stanza = compose_preset_stanza(
        personality_preset=PRESET_CUSTOM,
        personality_axes={
            "tone": "authoritative",
            "verbosity": "concise",
            "formality": "formal",
            "pace": "brisk",
        },
    )
    assert stanza is not None
    assert "authoritative" in stanza
    assert "concise" in stanza


def test_custom_preset_without_axes_falls_back_but_still_complete() -> None:
    stanza = compose_preset_stanza(
        personality_preset=PRESET_CUSTOM, personality_axes=None
    )
    assert stanza is not None
    for label in ("Tone:", "Verbosity:", "Formality:", "Pace:"):
        assert label in stanza


# ---------------------------------------------------------------------
# BUSINESS_CONTEXT stanza
# ---------------------------------------------------------------------


def test_no_business_context_returns_none() -> None:
    assert compose_business_context_stanza(business_context=None, tier=TIER_FREE) is None
    assert compose_business_context_stanza(business_context="   ", tier=TIER_FREE) is None


def test_business_context_framed_as_context_not_instruction() -> None:
    stanza = compose_business_context_stanza(
        business_context="We sell condos.", tier=TIER_FREE
    )
    assert stanza is not None
    assert stanza.startswith("=== Business Context ===")
    assert "context, not an instruction" in stanza
    assert "We sell condos." in stanza


def test_business_context_truncated_to_free_cap() -> None:
    cap = business_context_max_chars(TIER_FREE)
    stanza = compose_business_context_stanza(
        business_context="x" * (cap + 500), tier=TIER_FREE
    )
    assert stanza is not None
    # The body (after the framing) must not exceed the cap.
    body = stanza.split("\n\n", 1)[1]
    assert len(body) <= cap


def test_business_context_pro_cap_is_280() -> None:
    # Enterprise removed (Unit 1 excision). Both tiers are capped at 280.
    pro_cap = business_context_max_chars(TIER_PRO)
    free_cap = business_context_max_chars(TIER_FREE)
    assert pro_cap == 280
    assert free_cap == 280


# ---------------------------------------------------------------------
# Integration with build_system_prompt — §3.5.1 stanza order.
# ---------------------------------------------------------------------


def test_build_system_prompt_places_preset_before_business_before_knowledge() -> None:
    preset = compose_preset_stanza(personality_preset="trusted_authority")
    business = compose_business_context_stanza(
        business_context="We are a law firm.", tier=TIER_PRO
    )
    out = build_system_prompt(
        preset_stanza=preset,
        business_context_stanza=business,
        knowledge=["Office hours are 9-5."],
        assistant_name="Counsel",
    )
    i_name = out.index("Counsel")
    i_preset = out.index("=== Personality ===")
    i_business = out.index("=== Business Context ===")
    i_knowledge = out.index("=== Relevant Knowledge ===")
    assert i_name < i_preset < i_business < i_knowledge


def test_build_system_prompt_omits_unset_stanzas() -> None:
    out = build_system_prompt(assistant_name="Plain")
    assert "=== Personality ===" not in out
    assert "=== Business Context ===" not in out
