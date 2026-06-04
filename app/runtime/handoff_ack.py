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


# §3.4.13 + §3.4.5 canonical cannot_answer phrase. Used as the reply when
# the OUTCOME gate fires SIGNAL_CANNOT_CONFIDENTLY_ANSWER (grounding below
# the per-tier floor). This exact phrase is the Vision §1 "without
# hallucinating" promise made tangible: when the system cannot ground its
# answer it says so clearly and hands off to a human rather than fabricating.
CANNOT_ANSWER_REPLY: str = (
    "I don't have that information, let me get someone who does."
)


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


def cannot_answer_reply() -> str:
    """Return the §3.4.13 canonical cannot_answer phrase.

    Used when the OUTCOME gate fires SIGNAL_CANNOT_CONFIDENTLY_ANSWER:
    the system cannot ground the answer above the per-tier floor so it
    uses this exact phrase (Vision §1 anti-hallucination promise) and
    hands off to a human. The return-value wrapper is kept for
    symmetry with ``handoff_acknowledgement`` and to provide a single
    import point for both Gate-1 and Gate-2 reply text.
    """
    return CANNOT_ANSWER_REPLY
