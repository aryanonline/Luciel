"""
Admin API schemas.

Request and response models for tenant config, domain config,
agent config, and knowledge ingestion endpoints.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# --- Tenant Config ---

class TenantConfigCreate(BaseModel):
    tenant_id: str
    display_name: str
    description: str | None = None
    escalation_contact: str | None = None
    allowed_domains: list[str] | None = None
    system_prompt_additions: str | None = None
    created_by: str | None = None


class TenantConfigUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    escalation_contact: str | None = None
    allowed_domains: list[str] | None = None
    system_prompt_additions: str | None = None
    active: bool | None = None       # <-- ADD THIS
    updated_by: str | None = None


class TenantConfigRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: str
    display_name: str
    description: str | None
    escalation_contact: str | None
    allowed_domains: list | None
    system_prompt_additions: str | None
    active: bool
    created_by: str | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime


# --- Domain Config ---

class DomainConfigCreate(BaseModel):
    tenant_id: str
    domain_id: str
    display_name: str
    description: str | None = None
    system_prompt_additions: str | None = None
    allowed_tools: list[str] | None = None
    escalation_contact: str | None = None
    policy_overrides: dict | None = None
    preferred_provider: str | None = None
    created_by: str | None = None


class DomainConfigUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    system_prompt_additions: str | None = None
    allowed_tools: list[str] | None = None
    escalation_contact: str | None = None
    policy_overrides: dict | None = None
    preferred_provider: str | None = None
    active: bool | None = None       # <-- ADD THIS
    updated_by: str | None = None


class DomainConfigRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: str
    domain_id: str
    display_name: str
    description: str | None
    system_prompt_additions: str | None
    allowed_tools: list | None
    escalation_contact: str | None
    policy_overrides: dict | None
    preferred_provider: str | None
    active: bool
    created_by: str | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime


# Step 30a.5 §4.4 -- route-level rollup variant of DomainConfigRead.
#
# GET /api/v1/admin/domains/self-serve must surface two per-Domain
# rollups so the website's CompanyTab can render "3 agents · 1 invite
# pending" without a second N+1 fan-out. The two counts are NOT on
# the DomainConfig model -- they are joins computed at the route
# level and only meaningful for the self-serve list (the
# admin-key /admin/domains route stays clean). Keeping the rollup on
# a subclass preserves DomainConfigRead's role as the canonical
# model serializer and matches design §4.4 verbatim ("two extra
# rollup fields injected at the route level -- not on the model").
#
# pending_invites_count counts UserInvite rows under (tenant_id,
# domain_id) with status='pending'. active_agents_count counts
# Agent rows under (tenant_id, domain_id) with active=True. Both
# are non-negative ints; zero is a legitimate value and renders as
# "No agents yet" / "No invites pending" on the frontend.
class DomainConfigSelfServeRead(DomainConfigRead):
    """DomainConfigRead + per-Domain rollup counts for the CompanyTab."""

    pending_invites_count: int = Field(
        ...,
        ge=0,
        description=(
            "Count of UserInvite rows under (tenant_id, domain_id) "
            "with status='pending'. Rendered as the 'invites pending' "
            "badge on the CompanyTab Domain card."
        ),
    )
    active_agents_count: int = Field(
        ...,
        ge=0,
        description=(
            "Count of Agent rows under (tenant_id, domain_id) with "
            "active=True. Rendered as the 'N agents' badge on the "
            "CompanyTab Domain card."
        ),
    )


# Step 30a.5 -- cookied self-serve Domain creation. Distinct from
# DomainConfigCreate (operator/admin-key path) because:
#   (a) the slug is locked to a hard regex (uppercase, leading hyphen,
#       trailing hyphen, underscores all rejected) since the slug shows
#       up in URLs and audit logs;
#   (b) tenant_id is derived from the cookied user's active scope
#       assignment, never accepted from the client (cross-tenant safety);
#   (c) the only optional content field is description -- system prompt
#       additions / tool allowlists / policy overrides remain operator-
#       only territory in v1.
class DomainConfigSelfServeCreate(BaseModel):
    """Payload for POST /api/v1/admin/domains/self-serve."""

    domain_id: str = Field(
        ...,
        min_length=2,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$",
        description=(
            "URL-safe slug for the Domain. Lowercase letters, digits, "
            "and internal hyphens only. Used in audit logs and URLs."
        ),
    )
    display_name: str = Field(
        ...,
        min_length=1,
        max_length=120,
        description="Human-readable name shown in the dashboard.",
    )
    description: str | None = Field(
        default=None,
        max_length=500,
        description="Optional one-line description.",
    )


# --- Agent Config ---

class AgentConfigCreate(BaseModel):
    tenant_id: str
    agent_id: str
    display_name: str
    description: str | None = None
    system_prompt_additions: str | None = None
    escalation_contact: str | None = None
    allowed_domains: list[str] | None = None
    policy_overrides: dict | None = None
    preferred_provider: str | None = None
    created_by: str | None = None


class AgentConfigUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    system_prompt_additions: str | None = None
    escalation_contact: str | None = None
    allowed_domains: list[str] | None = None
    policy_overrides: dict | None = None
    preferred_provider: str | None = None
    active: bool | None = None       # <-- ADD THIS
    updated_by: str | None = None


class AgentConfigRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: str
    agent_id: str
    display_name: str
    description: str | None
    system_prompt_additions: str | None
    escalation_contact: str | None
    allowed_domains: list | None
    policy_overrides: dict | None
    preferred_provider: str | None
    active: bool
    created_by: str | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime


# --- Knowledge Ingestion ---

class KnowledgeIngestRequest(BaseModel):
    content: str
    knowledge_type: str
    tenant_id: str | None = None
    domain_id: str | None = None
    agent_id: str | None = None
    title: str | None = None
    source: str | None = None
    created_by: str | None = None
    max_chunk_size: int = 800
    replace_existing: bool = False


class KnowledgeIngestResponse(BaseModel):
    chunks_stored: int
    knowledge_type: str
    tenant_id: str | None
    domain_id: str | None
    agent_id: str | None = None
    source: str | None