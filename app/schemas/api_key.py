"""
API key schemas.

Permission vocabulary (Step 24):
- "chat"            : may call /api/v1/chat and /chat/stream
- "sessions"        : may manage sessions under its scope
- "admin"           : may manage domains/agents/knowledge/keys WITHIN its scope
                       (tenant-, domain-, or agent-scoped based on key fields)
- "platform_admin"  : may act across all tenants (VantageMind operators only)

Scope is determined by the key's tenant_id / domain_id / agent_id columns,
not by the permissions list. Permissions gate WHICH actions; scope gates
WHICH rows those actions may touch.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ApiKeyCreate(BaseModel):
    tenant_id: str | None = Field(
        default=None,
        min_length=2,
        max_length=100,
        description="NULL for platform-admin keys (cross-tenant bypass via 'platformadmin' permission per Invariant 5).",
    )
    domain_id: str | None = None
    agent_id: str | None = None
    luciel_instance_id: int | None = Field(                 # Step 24.5
        default=None,
        description=(
            "Pin this key to a specific LucielInstance. When set, the "
            "key can only chat with that one Luciel. Admin keys leave "
            "this null."
        ),
    )
    display_name: str
    permissions: list[str] = Field(default_factory=lambda: ["chat", "sessions"])
    rate_limit: int = Field(default=1000, ge=0)
    created_by: str | None = None


class ApiKeyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    key_prefix: str
    tenant_id: str | None
    domain_id: str | None
    agent_id: str | None
    display_name: str
    permissions: list
    rate_limit: int
    active: bool
    created_by: str | None
    created_at: datetime
    luciel_instance_id: int | None = None


class ApiKeyCreateResponse(BaseModel):
    """Returned only at creation time. The raw_key is shown once and never again."""
    api_key: ApiKeyRead
    raw_key: str