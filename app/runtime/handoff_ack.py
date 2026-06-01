"""Arc 14 U2 — Gate-1 handoff acknowledgement templates.

When the INTAKE gate fires (explicit human request OR strong negative
sentiment), the orchestrator SKIPS plan/act/reflect and emits a short
acknowledgement that a human is being brought in. That ack is NOT a PLAN
call — it is a fixed, templated line so a handoff never depends on the
LLM (which may be exactly what's failing / frustrating the customer).

Persona presets — documented ambiguity (see PR)
------------------------------------------------
The §3.4.5 spec calls for the ack to be "templated per persona preset"
(Warm Concierge / Professional Advisor / Friendly Expert / Trusted
Authority). v2 has NO persona-preset surface: the persona is the fixed
``LUCIEL_SYSTEM_PROMPT`` core with a configurable ``assistant_name``
only (see app/persona/luciel_core.py; the Instance model has no
``persona_preset`` column). So this module ships the four preset
templates the spec names, keyed by an OPTIONAL preset string, and falls
back to a neutral professional default when none is supplied — which is
the case for every turn today. The preset key is the seam a later unit
fills once Instances can carry a persona preset; the judge and the
orchestrator gate do not change when it lands.
"""
from __future__ import annotations

# Preset ids the §3.4.5 spec names. No Instance surface selects one yet
# (documented ambiguity), so DEFAULT_PRESET is what every turn uses today.
PRESET_WARM_CONCIERGE = "warm_concierge"
PRESET_PROFESSIONAL_ADVISOR = "professional_advisor"
PRESET_FRIENDLY_EXPERT = "friendly_expert"
PRESET_TRUSTED_AUTHORITY = "trusted_authority"
DEFAULT_PRESET = PRESET_PROFESSIONAL_ADVISOR


# Each template is formatted with ``assistant_name``. The lines are
# deliberately short and channel-agnostic: they confirm the handoff and
# set the expectation that a human will follow up. They never promise an
# SLA the tier may not carry.
_TEMPLATES: dict[str, str] = {
    PRESET_WARM_CONCIERGE: (
        "Of course — I'm connecting you with a member of our team right "
        "now. They'll pick this up with you shortly. Thank you for your "
        "patience."
    ),
    PRESET_PROFESSIONAL_ADVISOR: (
        "I understand. I'm escalating this to a member of our team who "
        "will follow up with you directly. Someone will be in touch "
        "shortly."
    ),
    PRESET_FRIENDLY_EXPERT: (
        "Got it — let me bring in a teammate who can help with this. "
        "They'll reach out to you soon."
    ),
    PRESET_TRUSTED_AUTHORITY: (
        "I've flagged this for our team. A specialist will review it and "
        "follow up with you directly."
    ),
}


def handoff_acknowledgement(
    *,
    preset: str | None = None,
    assistant_name: str = "Luciel",
) -> str:
    """Return the Gate-1 handoff acknowledgement for a persona preset.

    Falls back to the neutral professional default when ``preset`` is
    None or unrecognised — an unknown preset must never produce an empty
    handoff line. ``assistant_name`` is reserved for templates that name
    the assistant; the current copy does not interpolate it, but the
    signature carries it so a later preset surface can.
    """
    template = _TEMPLATES.get(preset or DEFAULT_PRESET, _TEMPLATES[DEFAULT_PRESET])
    return template
