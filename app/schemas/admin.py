"""
Admin API schemas.

Request and response models for tenant config, domain config,
agent config, and knowledge ingestion endpoints.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


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