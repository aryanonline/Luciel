"""
LucielInstance schemas — request/response models for child-Luciel CRUD.

Step 24.5. Matches the LucielInstance SQLAlchemy model
(app/models/luciel_instance.py).

Scope rules mirrored at the API boundary (validated before hitting
the DB CheckConstraint so callers get 422s, not 500s):

    scope_level = "tenant"  -> domain_id NULL, agent_id NULL
    scope_level = "domain"  -> domain_id SET,  agent_id NULL
    scope_level = "agent"   -> domain_id SET,  agent_id SET

Create-at-or-below enforcement (platform_admin / tenant / domain /
agent caller semantics) lives in app/policy/scope.py (File 9), not
here. These schemas only guarantee the payload is structurally valid.

Domain-agnostic: no vertical enums, no hardcoded role names, no
tenant-ID branches.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


# ---------------------------------------------------------------------
# Shared constants / constraints
# ---------------------------------------------------------------------

ScopeLevel = Literal["tenant", "domain", "agent"]

_SLUG_PATTERN = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$"


# ---------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------

class LucielInstanceCreate(BaseModel):
    """Payload for POST /admin/luciel-instances.

    The caller declares scope_level and provides only the owner
    identifiers required by that scope. The model_validator below
    rejects inconsistent combinations before they hit the DB
    CheckConstraint.

    scope_owner_tenant_id / domain_id / agent_id identify the OWNER
    of the new instance — not the caller. ScopePolicy (File 9)
    separately enforces that the caller's key is allowed to create
    at that owner location.
    """

    instance_id: str = Field(
        ...,
        min_length=2,
        max_length=100,
        pattern=_SLUG_PATTERN,
        description="URL-safe slug, unique within the scope owner.",
    )

    display_name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)

    scope_level: ScopeLevel
    scope_owner_tenant_id: str = Field(
        ..., min_length=2, max_length=100, pattern=_SLUG_PATTERN
    )
    scope_owner_domain_id: str | None = Field(
        default=None, min_length=2, max_length=100, pattern=_SLUG_PATTERN
    )
    scope_owner_agent_id: str | None = Field(
        default=None, min_length=2, max_length=100, pattern=_SLUG_PATTERN
    )

    # Persona / behaviour — all optional, all data-driven.
    system_prompt_additions: str | None = None
    preferred_provider: str | None = Field(default=None, max_length=50)
    allowed_tools: list[str] | None = Field(
        default=None,
        description="List of tool slugs this instance may call. "
                    "NULL => inherit from DomainConfig.allowed_tools. "
                    "[] => explicitly no tools.",
    )

    created_by: str | None = Field(default=None, max_length=100)

    # ---------------------------------------------------------------
    # Step 30a.1 — Team/Company invite mode
    # ---------------------------------------------------------------
    #
    # When ``teammate_email`` is provided, the route enters \"invite\"
    # mode: it resolves-or-creates the User + Agent + ScopeAssignment
    # for the teammate BEFORE creating the agent-scope LucielInstance,
    # then dispatches a magic-link email so the teammate can sign in.
    # In invite mode ``scope_owner_agent_id`` MUST be omitted (the
    # route mints the agent slug from the email local-part).
    #
    # Invite mode is ONLY valid for ``scope_level=\"agent\"``. Inviting a
    # teammate to a domain-scope or tenant-scope Luciel is nonsensical
    # (those scopes are shared, not assigned to a single teammate).
    teammate_email: EmailStr | None = Field(
        default=None,
        description=(
            "Step 30a.1 — if set, the route mints the User+Agent+"
            "ScopeAssignment for this teammate and binds the new "
            "agent-scope LucielInstance under them. Only valid with "
            "scope_level='agent' and scope_owner_agent_id omitted."
        ),
    )

    @model_validator(mode="after")
    def _validate_scope_shape(self) -> "LucielInstanceCreate":
        level = self.scope_level
        has_domain = self.scope_owner_domain_id is not None
        has_agent = self.scope_owner_agent_id is not None
        is_invite = self.teammate_email is not None

        # Invite-mode shape: agent-scope only, no agent_id (the route
        # mints it). teammate_email coexists with scope_owner_domain_id
        # (the inviter picks the domain the teammate joins).
        if is_invite:
            if level != "agent":
                raise ValueError(
                    "teammate_email is only valid with scope_level='agent'."
                )
            if not has_domain:
                raise ValueError(
                    "invite mode requires scope_owner_domain_id (the domain "
                    "the teammate is being assigned to)."
                )
            if has_agent:
                raise ValueError(
                    "invite mode requires scope_owner_agent_id to be null; "
                    "the route mints the agent_id from the teammate email."
                )
            return self

        if level == "tenant" and (has_domain or has_agent):
            raise ValueError(
                "scope_level='tenant' requires scope_owner_domain_id "
                "and scope_owner_agent_id to be null."
            )
        if level == "domain" and (not has_domain or has_agent):
            raise ValueError(
                "scope_level='domain' requires scope_owner_domain_id "
                "to be set and scope_owner_agent_id to be null."
            )
        if level == "agent" and (not has_domain or not has_agent):
            raise ValueError(
                "scope_level='agent' requires both scope_owner_domain_id "
                "and scope_owner_agent_id to be set."
            )
        return self


# ---------------------------------------------------------------------
# Update (PATCH semantics — all fields optional)
# ---------------------------------------------------------------------

class LucielInstanceUpdate(BaseModel):
    """Payload for PATCH /admin/luciel-instances/{id}.

    Scope fields (scope_level, scope_owner_*) are immutable. Moving an
    instance across scopes would break knowledge ownership, chat-key
    bindings, and audit trails. Deactivate and recreate instead.
    """

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    system_prompt_additions: str | None = None
    preferred_provider: str | None = Field(default=None, max_length=50)
    allowed_tools: list[str] | None = None
    active: bool | None = None
    updated_by: str | None = Field(default=None, max_length=100)


# ---------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------

class LucielInstanceRead(BaseModel):
    """Full response shape for single-instance reads and list items."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    instance_id: str
    display_name: str
    description: str | None = None

    scope_level: ScopeLevel
    scope_owner_tenant_id: str
    scope_owner_domain_id: str | None = None
    scope_owner_agent_id: str | None = None

    system_prompt_additions: str | None = None
    preferred_provider: str | None = None
    allowed_tools: list[str] | None = None

    active: bool
    knowledge_chunk_count: int

    created_by: str | None = None
    updated_by: str | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------
# Summary (embedded in onboarding response and dashboard list views)
# ---------------------------------------------------------------------

class LucielInstanceSummary(BaseModel):
    """Compact instance reference for embedding in other responses.

    Used by the Step 23 onboarding response (File 12 patch) to surface
    the auto-created default tenant-level instance, and by
    dashboards (Step 31) to list instances without shipping persona
    text over the wire.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    instance_id: str
    display_name: str
    scope_level: ScopeLevel
    scope_owner_tenant_id: str
    scope_owner_domain_id: str | None = None
    scope_owner_agent_id: str | None = None
    active: bool