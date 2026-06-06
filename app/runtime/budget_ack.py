"""Arc 18 — Free budget-exhausted acknowledgement templates (§3.4.1b).

When a Free instance is at/over its per-instance conversation cap (200/
month), the orchestrator SKIPS plan/act/reflect and emits a short
acknowledgement WITHOUT an LLM call — the end customer ALWAYS receives a
response (never a silent drop). The admin (not the customer) gets the
upgrade nudge via the budget_exhausted escalation + email notification.

Mirrors ``app.runtime.handoff``: per-persona-preset templates keyed
by an optional preset string, falling back to the neutral professional
default. The copy is graceful and customer-facing — it does NOT mention
billing, plans, or the admin's account state; it acknowledges the
message and sets the expectation that a person will follow up, exactly
like the Gate-1 handoff. This keeps the budget cap invisible to the end
customer while still routing a real human follow-up via the escalation.
"""

from __future__ import annotations

from app.runtime.handoff import (
    DEFAULT_PRESET,
    PRESET_FRIENDLY_EXPERT,
    PRESET_PROFESSIONAL_ADVISOR,
    PRESET_TRUSTED_AUTHORITY,
    PRESET_WARM_CONCIERGE,
)

# Customer-facing copy. Deliberately indistinguishable from a normal
# handoff to the end customer: a budget cap is an ADMIN-side billing
# state, not something the customer should be told. Channel-agnostic and
# short; promises no SLA the tier may not carry.
_BUDGET_TEMPLATES: dict[str, str] = {
    PRESET_WARM_CONCIERGE: (
        "Thank you for reaching out — I'm passing this to a member of our "
        "team who will follow up with you directly. We appreciate your "
        "patience."
    ),
    PRESET_PROFESSIONAL_ADVISOR: (
        "Thank you for your message. I'm routing this to a member of our "
        "team who will follow up with you shortly."
    ),
    PRESET_FRIENDLY_EXPERT: (
        "Thanks for getting in touch — I'm bringing in a teammate who will "
        "follow up with you soon."
    ),
    PRESET_TRUSTED_AUTHORITY: (
        "Thank you. I've passed this to our team, and someone will follow "
        "up with you directly."
    ),
}


def budget_exhausted_acknowledgement(
    *,
    preset: str | None = None,
    assistant_name: str = "Luciel",
) -> str:
    """Return the Free budget-exhausted acknowledgement for a persona
    preset. Falls back to the neutral professional default for None /
    unknown presets — an exhausted budget must never produce an empty
    reply (no silent drop, §3.4.1b)."""
    return _BUDGET_TEMPLATES.get(
        preset or DEFAULT_PRESET, _BUDGET_TEMPLATES[DEFAULT_PRESET]
    )
