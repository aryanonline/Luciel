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
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------
# Shared constants / constraints
# ---------------------------------------------------------------------

# Admin.id is a VARCHAR(100) semantic slug per Q1 lock — mirrors the
# legacy tenant_configs.admin_id at Revision B backfill. Constrain
# at the schema boundary so callers get a 422 rather than an FK error.
_ADMIN_ID_PATTERN = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$"
_SLUG_PATTERN = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$"


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

    # Arc 9 C17 — per-Instance persona/system_prompt_additions. Composed
    # by chat_service into the four-layer system prompt (Luciel Core →
    # tenant → domain → instance). 8 000 chars is the hard ceiling so a
    # full persona + few-shot examples fit comfortably while leaving
    # headroom inside model context windows.
    system_prompt_additions: str | None = Field(default=None, max_length=8000)


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
    system_prompt_additions: str | None = Field(default=None, max_length=8000)


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
    system_prompt_additions: str | None = None

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


# ---------------------------------------------------------------------
# Transitional legacy aliases — DELETED at B1 when route bodies finish
# their V2 rewrite. These exist solely so admin.py and the two test
# files that still import the legacy class names (
# LucielInstanceCreate / LucielInstanceRead / LucielInstanceUpdate /
# LucielInstanceSummary) compile through B1. New code must NEVER
# import the legacy names.
# ---------------------------------------------------------------------

LucielInstanceCreate = InstanceCreate
LucielInstanceUpdate = InstanceUpdate
LucielInstanceRead = InstanceRead
LucielInstanceSummary = InstanceSummary
