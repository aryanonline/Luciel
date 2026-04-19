"""
Tenant onboarding schemas.

Request and response models for the one-call tenant onboarding endpoint.
Step 23 — eliminates manual multi-step tenant setup.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TenantOnboardRequest(BaseModel):
    """Everything needed to onboard a new tenant in one call."""

    # --- Tenant fields (required) ---
    tenant_id: str = Field(
        ..., min_length=2, max_length=100,
        pattern=r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$",
        description="URL-safe tenant slug, e.g. 'remax-crossroads'",
    )
    display_name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    escalation_contact: str | None = None
    system_prompt_additions: str | None = None

    # --- Default domain (optional overrides) ---
    default_domain_id: str = Field(
        default="general",
        min_length=2, max_length=100,
        description="Domain ID for the seed domain config",
    )
    default_domain_display_name: str = Field(
        default="General Assistant",
        description="Display name for the seed domain",
    )
    default_domain_description: str | None = Field(
        default="Default domain created during onboarding",
    )

    # --- API key ---
    api_key_display_name: str = Field(
        default="Default onboarding key",
        description="Label for the first API key",
    )
    api_key_permissions: list[str] = Field(
        default=["chat", "sessions"],
        description="Permissions for the first API key",
    )
    api_key_rate_limit: int = Field(default=1000, ge=0)

    # --- Retention defaults ---
    retention_days_sessions: int = Field(default=90, ge=0)
    retention_days_messages: int = Field(default=90, ge=0)
    retention_days_memory_items: int = Field(default=365, ge=0)
    retention_days_traces: int = Field(default=30, ge=0)
    retention_days_knowledge: int = Field(default=0, ge=0, description="0 = no auto-purge")

    # --- Audit ---
    created_by: str | None = None


class OnboardedTenantSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: str
    display_name: str
    active: bool
    created_at: datetime


class OnboardedDomainSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: str
    domain_id: str
    display_name: str
    active: bool
    created_at: datetime


class OnboardedApiKeySummary(BaseModel):
    key_prefix: str
    display_name: str
    permissions: list[str]
    rate_limit: int
    raw_key: str = Field(
        ..., description="Shown once. Store securely.",
    )


class OnboardedRetentionSummary(BaseModel):
    data_category: str
    retention_days: int
    action: str


class TenantOnboardResponse(BaseModel):
    """Full result of a successful onboarding.

    Step 24.5 (Option B — self-service): onboarding returns the
    admin_api_key only. The tenant admin uses it to create their own
    LucielInstance(s) via POST /admin/luciel-instances and to mint
    chat key(s) bound to specific instances via POST /admin/api-keys.
    No Luciel is auto-created during onboarding — every Luciel in the
    system exists because a tenant deliberately created it.
    """

    tenant: OnboardedTenantSummary
    default_domain: OnboardedDomainSummary
    admin_api_key: OnboardedApiKeySummary
    retention_policies: list[OnboardedRetentionSummary]
    message: str = (
        "Tenant onboarded. Use the admin key to create your first "
        "LucielInstance and its chat key."
    )