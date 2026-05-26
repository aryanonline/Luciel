"""
Tenant onboarding schemas.

Request and response models for the one-call tenant onboarding endpoint.
Step 23 — eliminates manual multi-step tenant setup.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TenantOnboardRequest(BaseModel):
    """Everything needed to onboard a new Admin in one call.

    Arc 6 Commit 8 (2026-05-23): added ``tier`` + ``tier_source`` to
    align with the V2 Admin schema. Legacy descriptive fields
    (description, escalation_contact, system_prompt_additions) are
    retained on the request for source-compat but are no longer
    persisted as Admin columns -- they thread into audit metadata and
    the api-key display name only.
    """

    # --- Admin fields (required) ---
    admin_id: str = Field(
        ..., min_length=2, max_length=100,
        pattern=r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$",
        description="URL-safe Admin slug, e.g. 'remax-crossroads'",
    )
    display_name: str = Field(..., min_length=1, max_length=200)

    # Arc 6 Commit 8 -- V2 tier vocabulary. Defaults match the
    # platform-admin onboarding intent (paid tier, stripe-webhook-
    # equivalent source); callers can override per their flow.
    tier: Literal["free", "pro", "enterprise"] = Field(
        default="pro",
        description="V2 tier; one of 'free' / 'pro' / 'enterprise'",
    )
    tier_source: Literal[
        "stripe_webhook", "platform_admin", "free_signup"
    ] = Field(
        default="stripe_webhook",
        description="Provenance of the tier assignment",
    )

    # --- Legacy descriptive fields (retained, no longer column-bearing) ---
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
    """Summary of the freshly-created Admin row.

    Arc 6 Commit 8 (2026-05-23): rewired to V2 Admin columns. The V2
    Admin model uses ``id`` (string slug PK) and ``name``; the legacy
    ``admin_id`` / ``display_name`` shape is preserved on the wire by
    explicit field construction in the route (see admin.py), so
    pre-Arc-6 API consumers do not see a breaking change.
    """

    model_config = ConfigDict(from_attributes=True)

    # Legacy wire shape -- preserved verbatim for API source-compat.
    id: str
    admin_id: str = Field(..., description="V2 Admin slug (== id)")
    display_name: str = Field(..., description="V2 Admin name")
    tier: str
    tier_source: str
    active: bool
    created_at: datetime


class OnboardedDomainSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    admin_id: str
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
    # Arc 5 Path A collapsed the Domain layer; ``default_domain`` is
    # always None post-collapse. Retained on the response shape for
    # API source-compat so older clients that read it tolerate the
    # null.
    default_domain: OnboardedDomainSummary | None = None
    admin_api_key: OnboardedApiKeySummary
    retention_policies: list[OnboardedRetentionSummary]
    message: str = (
        "Tenant onboarded. Use the admin key to create your first "
        "LucielInstance and its chat key."
    )