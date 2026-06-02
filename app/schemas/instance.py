"""Instance schemas — V2 Admin → Instance Pydantic surface.

Arc 5 Path A (Commit A3). Matches the V2 SQLAlchemy model
:class:`app.models.instance.Instance` exactly.

There is no Domain layer and no Agent layer; every Instance hangs off
exactly one Admin via ``admin_id``. The legacy ``scope_level`` /
``scope_owner_tenant_id`` / ``scope_owner_domain_id`` /
``scope_owner_agent_id`` discriminator quadruple is gone.

Authorization (which Admin may create which Instance) lives at the
route layer; these schemas only guarantee a payload is structurally
valid.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.persona.presets import (
    ALL_PRESETS,
    PRESET_CUSTOM,
    validate_custom_axes,
)


# ---------------------------------------------------------------------
# Shared constants / constraints
# ---------------------------------------------------------------------

# Admin.id is a VARCHAR(100) semantic slug per Q1 lock — mirrors the
# legacy tenant_configs.admin_id at Revision B backfill. Constrain
# at the schema boundary so callers get a 422 rather than an FK error.
_ADMIN_ID_PATTERN = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$"
_SLUG_PATTERN = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$"

PersonalityPreset = Literal[
    "warm_concierge",
    "professional_advisor",
    "friendly_expert",
    "trusted_authority",
    "custom",
]

_LEAD_ROUTING_STRATEGIES = frozenset(
    {"round_robin", "geographic", "specialty_match", "single_contact"}
)


def _validate_lead_routing_shape(value: dict[str, Any]) -> None:
    """Structural validation for the lead_routing JSONB shape.

    Raises ``ValueError`` (→ 422 via Pydantic) on a malformed object.
    Tier gating (Pro/Ent only) is enforced separately at the API layer
    where the resolved tier is known.
    """
    if not isinstance(value, dict):
        raise ValueError("lead_routing must be an object.")
    strategy = value.get("strategy")
    if strategy not in _LEAD_ROUTING_STRATEGIES:
        raise ValueError(
            "lead_routing.strategy must be one of "
            f"{sorted(_LEAD_ROUTING_STRATEGIES)}."
        )
    rules = value.get("rules", [])
    if rules is not None and not isinstance(rules, list):
        raise ValueError("lead_routing.rules must be a list when present.")


def _validate_axes_for_preset(
    preset: str | None,
    axes: dict[str, Any] | None,
) -> None:
    """Structural cross-field check: personality_axes is permitted ONLY
    when preset == custom, and must carry a valid four-axis shape then.

    Raises ``ValueError`` (→ 422). Tier gating (custom on Free → 403) is
    enforced at the API layer.
    """
    if preset == PRESET_CUSTOM:
        if not axes:
            raise ValueError(
                "personality_axes is required when "
                "personality_preset is 'custom'."
            )
        problems = validate_custom_axes(axes)
        if problems:
            raise ValueError(" ".join(problems))
    else:
        if axes:
            raise ValueError(
                "personality_axes may only be set when "
                "personality_preset is 'custom'."
            )


# ---------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------

class InstanceCreate(BaseModel):
    """Payload for POST /admin/instances.

    Every Instance is created under a specific Admin; the V2 doctrine
    has no scope hierarchy below the Admin, so this payload is flat —
    no discriminator, no sub-scope identifiers, no parent-scope
    validation. The DB enforces ``UNIQUE(admin_id, instance_slug)``
    and the FK to ``admins.id`` (RESTRICT-on-delete).
    """

    admin_id: str = Field(
        ...,
        min_length=2,
        max_length=100,
        pattern=_ADMIN_ID_PATTERN,
        description="Owning Admin (V2 billing entity and permissions root).",
    )

    instance_slug: str = Field(
        ...,
        min_length=2,
        max_length=100,
        pattern=_SLUG_PATTERN,
        description="URL-safe slug, unique within the Admin.",
    )

    display_name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)

    active: bool = Field(default=True)
    created_by: str | None = Field(default=None, max_length=100)

    # --- Arc 15 WU1 — instance configuration pillars (Vision §3.5) ---
    website: str | None = Field(default=None, max_length=255)
    personality_preset: PersonalityPreset = "warm_concierge"
    personality_axes: dict[str, str] | None = None
    # business_context is tier-capped (280 Free/Pro, 2000 Ent) at the
    # API layer where the tier is known. The 2000 ceiling here is the
    # structural max; the route applies the tighter per-tier cap.
    business_context: str | None = Field(default=None, max_length=2000)
    # lead_routing is Pro/Enterprise only (gated at the API). Structural
    # shape validated below.
    lead_routing: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check_pillars(self) -> "InstanceCreate":
        _validate_axes_for_preset(self.personality_preset, self.personality_axes)
        if self.lead_routing is not None:
            _validate_lead_routing_shape(self.lead_routing)
        return self


# ---------------------------------------------------------------------
# Update (PATCH semantics — all fields optional)
# ---------------------------------------------------------------------

class InstanceUpdate(BaseModel):
    """Payload for PATCH /admin/instances/{id}.

    Identity columns (``admin_id``, ``instance_slug``) are immutable.
    Moving an Instance across Admins would break knowledge ownership,
    chat-key bindings, and audit trails. Deactivate and recreate
    instead.
    """

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    active: bool | None = None

    # --- Arc 15 WU1 — instance configuration pillars (Vision §3.5) ---
    website: str | None = Field(default=None, max_length=255)
    personality_preset: PersonalityPreset | None = None
    personality_axes: dict[str, str] | None = None
    business_context: str | None = Field(default=None, max_length=2000)
    lead_routing: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check_pillars(self) -> "InstanceUpdate":
        # On PATCH, only cross-validate axes when a preset is supplied.
        # A partial update that sets axes without a preset cannot decide
        # the cross-field rule here (the stored preset is unknown to the
        # schema), so that case is enforced at the API layer against the
        # merged row.
        if self.personality_preset is not None:
            _validate_axes_for_preset(
                self.personality_preset, self.personality_axes
            )
        if self.lead_routing is not None:
            _validate_lead_routing_shape(self.lead_routing)
        return self


# ---------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------

class InstanceRead(BaseModel):
    """Full response shape for single-instance reads and list items."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    admin_id: str
    instance_slug: str
    display_name: str
    description: str | None = None

    active: bool

    # --- Arc 15 WU1 — instance configuration pillars (Vision §3.5) ---
    website: str | None = None
    personality_preset: PersonalityPreset = "warm_concierge"
    personality_axes: dict[str, str] | None = None
    business_context: str | None = None
    lead_routing: dict[str, Any] | None = None
    # Arc 15 WU3 — escalation contact + routing config (contact only).
    escalation_config: dict[str, Any] | None = None

    # Arc 11 Closeout PR-A — instance lifecycle status per Customer
    # Journey §4.5 Phase 8 + Architecture §3.6.1. Source of truth for
    # the three-affordance lifecycle (Pause / Delete / Restore); the
    # legacy ``active`` boolean is a deprecated mirror.
    instance_status: Literal["active", "paused", "deleted"] = "active"
    soft_deleted_at: datetime | None = None

    # Arc 11 Closeout PR-A — populated ONLY on the response of
    # POST /admin/instances/{pk}/restore, per Vision §6.4 Reactivation
    # ("embed keys re-minted (new keys, old keys stay revoked)"). The
    # raw key is returned once and never persisted to SSM; the admin
    # must paste it into their site. All other reads carry None.
    new_embed_key: str | None = None

    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------
# Summary (embedded in onboarding response and dashboard list views)
# ---------------------------------------------------------------------

class InstanceSummary(BaseModel):
    """Compact Instance reference for embedding in other responses.

    Used by the tier-provisioning response to surface the auto-created
    primary Instance and by dashboards to list Instances without
    shipping description text over the wire.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    admin_id: str
    instance_slug: str
    display_name: str
    active: bool
