"""Tier-conditional validation for the Arc 15 instance config pillars.

The Pydantic schemas (``app/schemas/instance.py``) enforce STRUCTURAL
validity — a preset is a known enum value, custom axes carry the right
four keys, lead_routing has a valid strategy. They cannot enforce the
TIER-conditional rules, because the admin's tier is resolved
server-side, not carried in the request body.

This module is the single place those tier-conditional rules live, so
the create route, the PATCH route, and the WU3 personality API all
agree:

  * ``custom`` personality preset is Pro/Enterprise only (Free → reject).
  * ``business_context`` length is capped per tier (280 Free/Pro, 2000
    Enterprise).
  * ``lead_routing`` is Pro/Enterprise only (Free → reject).

Each function returns a list of structured problem dicts (empty = OK).
Routes translate a non-empty list into the appropriate HTTP error:
422 for the create/PATCH surface (validation), or the personality API's
403 for the custom-preset tier gate (an entitlement refusal, not a
malformed payload). The caller picks the status code; this module only
decides whether each rule passed and why.
"""

from __future__ import annotations

from typing import Any

from app.policy.entitlements import (
    business_context_max_chars,
    custom_personality_enabled,
    lead_routing_enabled,
)
from app.persona.presets import PRESET_CUSTOM


def check_business_context_length(
    *, tier: str, business_context: str | None
) -> list[dict[str, Any]]:
    """Return problems if business_context exceeds the tier's char cap."""
    if not business_context:
        return []
    cap = business_context_max_chars(tier)
    if len(business_context) > cap:
        return [
            {
                "field": "business_context",
                "reason": "too_long_for_tier",
                "message": (
                    f"business_context is {len(business_context)} chars; "
                    f"the limit on tier '{tier}' is {cap}."
                ),
                "tier": tier,
                "max_chars": cap,
            }
        ]
    return []


def check_custom_preset_allowed(
    *, tier: str, personality_preset: str | None
) -> list[dict[str, Any]]:
    """Return problems if the custom preset is used on a tier without it.

    The ``custom`` preset (direct axis authoring) is Pro/Enterprise only;
    Free must use one of the four named presets.
    """
    if personality_preset == PRESET_CUSTOM and not custom_personality_enabled(
        tier
    ):
        return [
            {
                "field": "personality_preset",
                "reason": "custom_preset_not_available_on_tier",
                "message": (
                    "The 'custom' personality preset is available on Pro "
                    "only. Choose one of the named presets "
                    "(warm_concierge, professional_advisor, "
                    "friendly_expert, trusted_authority)."
                ),
                "tier": tier,
                "upgrade_required": True,
            }
        ]
    return []


def check_lead_routing_allowed(
    *, tier: str, lead_routing: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Return problems if lead_routing is set on a tier without it."""
    if lead_routing is not None and not lead_routing_enabled(tier):
        return [
            {
                "field": "lead_routing",
                "reason": "lead_routing_not_available_on_tier",
                "message": (
                    "lead_routing is available on Pro "
                    "only. Free instances must leave it unset."
                ),
                "tier": tier,
                "upgrade_required": True,
            }
        ]
    return []


def validate_pillars_for_tier(
    *,
    tier: str,
    personality_preset: str | None = None,
    business_context: str | None = None,
    lead_routing: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run every tier-conditional pillar check, returning all problems.

    Empty list means the payload is permitted on the given tier.
    """
    problems: list[dict[str, Any]] = []
    problems += check_custom_preset_allowed(
        tier=tier, personality_preset=personality_preset
    )
    problems += check_business_context_length(
        tier=tier, business_context=business_context
    )
    problems += check_lead_routing_allowed(tier=tier, lead_routing=lead_routing)
    return problems


__all__ = [
    "check_business_context_length",
    "check_custom_preset_allowed",
    "check_lead_routing_allowed",
    "validate_pillars_for_tier",
]
