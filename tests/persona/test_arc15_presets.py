"""Arc 15 WU1/WU2 — personality preset map + axis resolution/validation.

Pins ``app.persona.presets``: the four named presets each resolve to a
fixed four-axis tuple held in code (never the DB), ``custom`` resolves
to supplied stored axes, and the custom-axes validator enforces the
closed axis vocabulary.
"""
from __future__ import annotations

from app.persona.presets import (
    ALL_PRESETS,
    AXIS_KEYS,
    AXIS_VOCAB,
    DEFAULT_PRESET,
    NAMED_PRESETS,
    PRESET_AXES,
    PRESET_CUSTOM,
    PRESET_WARM_CONCIERGE,
    is_named_preset,
    resolve_axes,
    validate_custom_axes,
)


# ---------------------------------------------------------------------
# Preset name surface — must match the DB enum exactly.
# ---------------------------------------------------------------------


def test_all_presets_is_named_plus_custom() -> None:
    assert ALL_PRESETS == NAMED_PRESETS + (PRESET_CUSTOM,)


def test_default_preset_is_warm_concierge() -> None:
    assert DEFAULT_PRESET == PRESET_WARM_CONCIERGE


def test_exactly_four_named_presets() -> None:
    assert len(NAMED_PRESETS) == 4
    assert PRESET_CUSTOM not in NAMED_PRESETS


def test_is_named_preset() -> None:
    for p in NAMED_PRESETS:
        assert is_named_preset(p) is True
    assert is_named_preset(PRESET_CUSTOM) is False
    assert is_named_preset("nope") is False


# ---------------------------------------------------------------------
# Named preset axis map — every named preset carries all four axes with
# in-vocab values.
# ---------------------------------------------------------------------


def test_every_named_preset_has_four_valid_axes() -> None:
    for preset in NAMED_PRESETS:
        axes = PRESET_AXES[preset]
        assert set(axes.keys()) == set(AXIS_KEYS), preset
        for axis, value in axes.items():
            assert value in AXIS_VOCAB[axis], (preset, axis, value)


def test_custom_has_no_fixed_axis_tuple() -> None:
    assert PRESET_CUSTOM not in PRESET_AXES


# ---------------------------------------------------------------------
# resolve_axes
# ---------------------------------------------------------------------


def test_resolve_named_returns_fixed_tuple() -> None:
    assert resolve_axes(PRESET_WARM_CONCIERGE) == PRESET_AXES[PRESET_WARM_CONCIERGE]


def test_resolve_named_returns_copy_not_shared_ref() -> None:
    out = resolve_axes(PRESET_WARM_CONCIERGE)
    out["tone"] = "MUTATED"
    assert PRESET_AXES[PRESET_WARM_CONCIERGE]["tone"] != "MUTATED"


def test_resolve_custom_uses_supplied_axes() -> None:
    custom = {
        "tone": "authoritative",
        "verbosity": "concise",
        "formality": "formal",
        "pace": "brisk",
    }
    assert resolve_axes(PRESET_CUSTOM, custom_axes=custom) == custom


def test_resolve_custom_without_axes_falls_back_to_default() -> None:
    assert resolve_axes(PRESET_CUSTOM, custom_axes=None) == PRESET_AXES[DEFAULT_PRESET]


# ---------------------------------------------------------------------
# validate_custom_axes
# ---------------------------------------------------------------------


def test_validate_custom_axes_valid() -> None:
    axes = {
        "tone": "warm",
        "verbosity": "balanced",
        "formality": "casual",
        "pace": "relaxed",
    }
    assert validate_custom_axes(axes) == []


def test_validate_custom_axes_missing_key() -> None:
    problems = validate_custom_axes(
        {"tone": "warm", "verbosity": "balanced", "formality": "casual"}
    )
    assert any("missing" in p for p in problems)


def test_validate_custom_axes_extra_key() -> None:
    problems = validate_custom_axes(
        {
            "tone": "warm",
            "verbosity": "balanced",
            "formality": "casual",
            "pace": "relaxed",
            "bogus": "x",
        }
    )
    assert any("unknown" in p for p in problems)


def test_validate_custom_axes_out_of_vocab_value() -> None:
    problems = validate_custom_axes(
        {
            "tone": "sassy",
            "verbosity": "balanced",
            "formality": "casual",
            "pace": "relaxed",
        }
    )
    assert any("tone" in p for p in problems)


def test_validate_custom_axes_non_dict() -> None:
    assert validate_custom_axes("not-a-dict") != []  # type: ignore[arg-type]
