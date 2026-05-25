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
    """V2 Admin read model. Field names below are the V2 truth
    (`name` / `tier` / `tier_source`); the legacy aliases `tenant_id`
    and `display_name` are populated from `id` and `name` via a
    model_validator for back-compat with the widget-E2E harness,
    Stripe webhooks, and external smoke scripts.

    Arc 9.2 PR #99 cleanup of the Arc-5-Rev-C drift: the prior schema
    declared eight fields (`description`, `escalation_contact`,
    `allowed_domains`, `system_prompt_additions`, `created_by`,
    `updated_by`, plus the wrong `id: int`) that have not existed on
    `admins` since Arc 5 Rev C. Any 200-OK response from this endpoint
    would have failed Pydantic validation before reaching the client.
    """
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    tier: str
    tier_source: str
    active: bool
    stripe_customer_id: str | None = None
    created_at: datetime
    updated_at: datetime

    # Back-compat aliases. Populated by a model_validator so external
    # callers that still read `tenant_id` / `display_name` keep working.
    tenant_id: str = ""
    display_name: str = ""

    @classmethod
    def model_validate(cls, obj, *args, **kwargs):  # type: ignore[override]
        instance = super().model_validate(obj, *args, **kwargs)
        if not instance.tenant_id:
            instance.tenant_id = instance.id
        if not instance.display_name:
            instance.display_name = instance.name
        return instance


# --- Domain Config: REMOVED (Arc 5 Path A) ---
#
# The V2 hierarchy is Admin → Instance → Lead. There is no Domain
# layer. The Pydantic schemas previously defined here
# (DomainConfigCreate / Read / Update / SelfServeRead / SelfServeCreate)
# were deleted at Arc 5 B4 along with the /admin/domains/* route surface
# (deleted at B1) and the AdminService.create_domain_config /
# get_domain_config / list_domain_configs methods (deleted at B3).
#
# Any test or script that still references these names must be deleted
# or rewritten to operate at the Admin or Instance layer.

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