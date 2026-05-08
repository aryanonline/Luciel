"""
Agent schemas — request/response models for agent (person/role) CRUD.

Step 24.5. Matches the Agent SQLAlchemy model (app/models/agent.py).
Persona fields are deliberately absent — they live on LucielInstance
schemas (app/schemas/luciel_instance.py).

Domain-agnostic: no vertical enums, no hardcoded role names, no
tenant-ID branches.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# ---------------------------------------------------------------------
# Shared field constraints
# ---------------------------------------------------------------------

# Slug pattern: lowercase alphanumerics + hyphens, not starting or
# ending with a hyphen. Same pattern used by tenant_id and domain_id
# elsewhere in the codebase for consistency.
_SLUG_PATTERN = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$"


# ---------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------

class AgentCreate(BaseModel):
    """Payload for POST /admin/agents."""

    tenant_id: str = Field(
        ...,
        min_length=2,
        max_length=100,
        pattern=_SLUG_PATTERN,
        description="Tenant that owns this agent.",
    )
    domain_id: str = Field(
        ...,
        min_length=2,
        max_length=100,
        pattern=_SLUG_PATTERN,
        description="Domain (department/vertical slot) the agent belongs to. "
                    "Must exist and be active under the given tenant_id.",
    )
    agent_id: str = Field(
        ...,
        min_length=2,
        max_length=100,
        pattern=_SLUG_PATTERN,
        description="URL-safe slug, unique within (tenant_id, domain_id).",
    )
    display_name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=500)
    contact_email: EmailStr | None = None

    created_by: str | None = Field(default=None, max_length=100)


# ---------------------------------------------------------------------
# Update (PATCH semantics — all fields optional)
# ---------------------------------------------------------------------

# ---------------------------------------------------------------------
# Bind-user payload (Step 28 Phase 2 - Commit 9)
#
# Dedicated platform-admin-only payload for the
# POST /admin/agents/{tenant_id}/{agent_id}/bind-user route. Carved out
# from AgentUpdate so user_id binding is explicit at the API surface
# (cannot be smuggled in via a tenant-admin display_name PATCH).
# ---------------------------------------------------------------------

class AgentBindUserPayload(BaseModel):
    """Bind an Agent to a User identity.

    Platform-admin only. Used by Step 24.5b user-rebind workflows and
    by the Step 28 Phase 2 verification harness (Pillars 12, 13, 14)
    which previously reached around the API to mutate agents.user_id
    via a raw SQLAlchemy session — a path that is correctly refused
    when the harness runs from a least-privilege Pattern N task with
    the worker DSN.

    Invariant (enforced at service layer): a User holds at most one
    active Agent per tenant. Attempting to bind a User who already
    has an active Agent in the same tenant returns 409.
    """

    user_id: uuid.UUID = Field(
        ...,
        description="User identity to bind this Agent to. Must reference an active User row.",
    )
    updated_by: str | None = Field(default=None, max_length=100)


class AgentUpdate(BaseModel):
    """Payload for PATCH /admin/agents/{tenant_id}/{agent_id}.

    tenant_id / domain_id / agent_id are immutable after creation.
    To move an agent between domains, deactivate and recreate — this
    keeps audit trails clean (see Step 24.5 decision on promotion /
    demotion handling).
    """

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=500)
    contact_email: EmailStr | None = None
    active: bool | None = None
    updated_by: str | None = Field(default=None, max_length=100)


# ---------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------

class AgentRead(BaseModel):
    """Response shape for single-agent reads and list items."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: str
    domain_id: str
    agent_id: str
    display_name: str
    description: str | None = None
    contact_email: str | None = None
    active: bool
    # Step 28 Phase 2 - Commit 9: surface user_id so the bind-user route
    # response and the verification harness (P12/13/14) can confirm the
    # bind landed. Existing list/get callers gain visibility too — useful
    # for tenant-admin UIs that need to show who an Agent maps to.
    user_id: uuid.UUID | None = None
    created_by: str | None = None
    updated_by: str | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------
# Summary (used embedded in other responses — e.g. LucielInstanceRead
# when it wants to include the owning agent's display_name)
# ---------------------------------------------------------------------

class AgentSummary(BaseModel):
    """Compact agent reference for embedding in other responses."""

    model_config = ConfigDict(from_attributes=True)

    tenant_id: str
    domain_id: str
    agent_id: str
    display_name: str
    active: bool