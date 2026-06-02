"""Arc 15 WU3 — personality-config admin API schemas (Vision §3.5).

The personality config is the STRUCTURED voice surface: a curated named
preset, or ``custom`` with four bounded axes, plus tier-capped
``business_context`` background text. There is deliberately NO free-text
"system prompt" / raw-stanza field here — the composer
(``app.persona.composer``) renders the platform-controlled PRESET and
BUSINESS_CONTEXT stanzas; an admin can only pick a preset, move the four
axis sliders (custom, Pro/Ent), or supply framed background context.

Structural validation (preset is a known enum; axes are the right four
keys with in-vocab values; axes only when preset==custom) is enforced
here. Tier gates (custom→403 on Free; business_context length per tier)
are enforced at the API layer via ``app.policy.instance_config``.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.instance import (
    PersonalityPreset,
    _validate_axes_for_preset,
)


class PersonalityConfigUpdate(BaseModel):
    """PUT body for the personality config.

    NOTE: there is intentionally no ``system_prompt_additions`` /
    raw-prompt field. The personality surface is preset + bounded axes +
    framed business_context ONLY (Architecture §3.5.1: "never raw prompt
    authoring").
    """

    model_config = ConfigDict(extra="forbid")

    personality_preset: PersonalityPreset
    personality_axes: dict[str, str] | None = None
    business_context: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _check_axes(self) -> "PersonalityConfigUpdate":
        # Structural cross-field rule: axes permitted ONLY when custom.
        # Tier gate (custom on Free) is applied at the API layer.
        _validate_axes_for_preset(self.personality_preset, self.personality_axes)
        return self


class PersonalityConfigResponse(BaseModel):
    """GET/PUT response: the stored personality config + tier context."""

    model_config = ConfigDict(from_attributes=True)

    instance_id: int
    admin_id: str
    admin_tier: str
    # Whether this tier may use the ``custom`` preset (Pro/Ent only).
    custom_preset_available: bool
    # The business_context char cap for this tier.
    business_context_max_chars: int

    personality_preset: PersonalityPreset
    personality_axes: dict[str, str] | None = None
    business_context: str | None = None
    updated_at: datetime | None = None
