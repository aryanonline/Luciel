"""Personality presets — Architecture §3.5.1 (Arc 15 WU1/WU2).

Vision §3.5 ships four NAMED personality presets on every tier plus a
``custom`` preset (Pro/Enterprise only). Each named preset maps to a
fixed tuple of four personality *axes* — ``(tone, verbosity, formality,
pace)`` — held HERE in code, never in the database. The DB only stores
the preset *name* (``instances.personality_preset``); the axis values
for a named preset are looked up from this module at compose time.

The ``custom`` preset is the sole exception: it carries no fixed tuple,
because a custom instance stores its four axis values directly in
``instances.personality_axes`` (a JSONB ``{tone, verbosity, formality,
pace}`` object). The composer reads those stored axes instead of this
map when the preset is ``custom``.

Why a tuple-in-code map (not a DB lookup table)
-----------------------------------------------
The named presets are platform-curated voice profiles. They are part of
the product surface, not tenant data — an admin picks one by name, they
do not edit its axis values. Keeping the map in code means:

  * the four named voices are versioned with the composer that renders
    them (a preset and its stanza text move together);
  * there is no migration / seed coupling for what is really a constant;
  * the "never raw prompt authoring" rule (Architecture §3.5.1) is
    structurally enforced — an admin can only ever select a curated
    preset or move four bounded axis sliders, never inject free text
    into the PRESET stanza.

Axis vocabulary
---------------
Each axis is a small closed vocabulary so the custom-preset validator
(``app/schemas/instance.py``) can reject anything off-menu and the
composer can render a stable, deterministic stanza:

  * ``tone``      — ``warm | neutral | authoritative | enthusiastic``
  * ``verbosity`` — ``concise | balanced | detailed``
  * ``formality`` — ``casual | professional | formal``
  * ``pace``      — ``relaxed | measured | brisk``

The four named presets pick one value per axis (see ``PRESET_AXES``).
"""

from __future__ import annotations

# ---------------------------------------------------------------------
# Preset names. The DB enum ``personality_preset`` carries exactly these
# five values; the migration creates the type from this same list.
# ---------------------------------------------------------------------

PRESET_WARM_CONCIERGE = "warm_concierge"
PRESET_PROFESSIONAL_ADVISOR = "professional_advisor"
PRESET_FRIENDLY_EXPERT = "friendly_expert"
PRESET_TRUSTED_AUTHORITY = "trusted_authority"
PRESET_CUSTOM = "custom"

#: The four named presets (every tier).
NAMED_PRESETS: tuple[str, ...] = (
    PRESET_WARM_CONCIERGE,
    PRESET_PROFESSIONAL_ADVISOR,
    PRESET_FRIENDLY_EXPERT,
    PRESET_TRUSTED_AUTHORITY,
)

#: All five preset names (named + custom). Matches the DB enum exactly.
ALL_PRESETS: tuple[str, ...] = NAMED_PRESETS + (PRESET_CUSTOM,)

#: DB default for the column.
DEFAULT_PRESET = PRESET_WARM_CONCIERGE


# ---------------------------------------------------------------------
# Axis vocabularies. Closed sets — the custom validator rejects anything
# not in these, and the composer renders only these values.
# ---------------------------------------------------------------------

AXIS_KEYS: tuple[str, ...] = ("tone", "verbosity", "formality", "pace")

AXIS_VOCAB: dict[str, frozenset[str]] = {
    "tone": frozenset({"warm", "neutral", "authoritative", "enthusiastic"}),
    "verbosity": frozenset({"concise", "balanced", "detailed"}),
    "formality": frozenset({"casual", "professional", "formal"}),
    "pace": frozenset({"relaxed", "measured", "brisk"}),
}


# ---------------------------------------------------------------------
# Named preset → axis tuple map. Each value is a dict keyed by AXIS_KEYS.
# Values are chosen to be self-consistent with the preset name:
#
#   warm_concierge      — welcoming, attentive, easy to talk to.
#   professional_advisor — measured, precise, businesslike.
#   friendly_expert     — approachable but knowledgeable; explains well.
#   trusted_authority   — confident, succinct, commands credibility.
# ---------------------------------------------------------------------

PRESET_AXES: dict[str, dict[str, str]] = {
    PRESET_WARM_CONCIERGE: {
        "tone": "warm",
        "verbosity": "balanced",
        "formality": "casual",
        "pace": "relaxed",
    },
    PRESET_PROFESSIONAL_ADVISOR: {
        "tone": "neutral",
        "verbosity": "balanced",
        "formality": "professional",
        "pace": "measured",
    },
    PRESET_FRIENDLY_EXPERT: {
        "tone": "enthusiastic",
        "verbosity": "detailed",
        "formality": "casual",
        "pace": "measured",
    },
    PRESET_TRUSTED_AUTHORITY: {
        "tone": "authoritative",
        "verbosity": "concise",
        "formality": "formal",
        "pace": "brisk",
    },
}


# ---------------------------------------------------------------------
# Resolution + validation helpers.
# ---------------------------------------------------------------------


def is_named_preset(preset: str) -> bool:
    """True if ``preset`` is one of the four curated named presets."""
    return preset in NAMED_PRESETS


def resolve_axes(
    preset: str,
    custom_axes: dict[str, str] | None = None,
) -> dict[str, str]:
    """Resolve the effective four-axis dict for an instance.

    * A named preset returns its fixed tuple from :data:`PRESET_AXES`.
    * ``custom`` returns the supplied ``custom_axes`` (the stored
      ``instances.personality_axes`` JSONB). When a custom instance has
      no stored axes yet, fall back to the default preset's axes so the
      composer never emits a half-built stanza.

    Raises ``KeyError`` for an unknown preset name — callers validate
    ``preset in ALL_PRESETS`` upstream (the DB enum guarantees it for
    persisted rows).
    """
    if preset == PRESET_CUSTOM:
        if custom_axes:
            return dict(custom_axes)
        return dict(PRESET_AXES[DEFAULT_PRESET])
    return dict(PRESET_AXES[preset])


def validate_custom_axes(axes: dict[str, str]) -> list[str]:
    """Return a list of human-readable problems with a custom-axes dict.

    Empty list means valid. The shape must carry EXACTLY the four
    :data:`AXIS_KEYS`, each with a value drawn from :data:`AXIS_VOCAB`.
    Used by the ``InstanceCreate`` / personality-API validators to emit
    422 errors.
    """
    problems: list[str] = []
    if not isinstance(axes, dict):
        return ["personality_axes must be an object."]

    keys = set(axes.keys())
    expected = set(AXIS_KEYS)
    missing = expected - keys
    extra = keys - expected
    if missing:
        problems.append(
            "personality_axes missing axis(es): "
            f"{sorted(missing)}."
        )
    if extra:
        problems.append(
            "personality_axes has unknown axis(es): "
            f"{sorted(extra)}."
        )
    for axis in AXIS_KEYS:
        if axis not in axes:
            continue
        value = axes[axis]
        if value not in AXIS_VOCAB[axis]:
            problems.append(
                f"personality_axes.{axis}={value!r} is not one of "
                f"{sorted(AXIS_VOCAB[axis])}."
            )
    return problems


__all__ = [
    "PRESET_WARM_CONCIERGE",
    "PRESET_PROFESSIONAL_ADVISOR",
    "PRESET_FRIENDLY_EXPERT",
    "PRESET_TRUSTED_AUTHORITY",
    "PRESET_CUSTOM",
    "NAMED_PRESETS",
    "ALL_PRESETS",
    "DEFAULT_PRESET",
    "AXIS_KEYS",
    "AXIS_VOCAB",
    "PRESET_AXES",
    "is_named_preset",
    "resolve_axes",
    "validate_custom_axes",
]
