"""
API Key schemas.
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