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

from pydantic import BaseModel, ConfigDict


class ApiKeyCreate(BaseModel):
    tenant_id: str
    domain_id: str | None = None
    agent_id: str | None = None
    display_name: str
    permissions: list[str] | None = None
    rate_limit: int = 1000
    created_by: str | None = None


class ApiKeyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    key_prefix: str
    tenant_id: str
    domain_id: str | None
    agent_id: str | None
    display_name: str
    permissions: list
    rate_limit: int
    active: bool
    created_by: str | None
    created_at: datetime


class ApiKeyCreateResponse(BaseModel):
    """Returned only at creation time. The raw_key is shown once and never again."""
    api_key: ApiKeyRead
    raw_key: str