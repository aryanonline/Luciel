from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas._tenant_admin_alias import (
    TenantAdminOutputAlias,
)


class SessionCreate(BaseModel):
    # Only user_id and channel are provided by the client.
    user_id: str | None = None
    channel: str = "web"

    # These can optionally be provided but usually come from the API key.
    # If not provided, they come from what the API key allows.
    # Arc 9.2 PR #100: input alias removed; callers MUST send ``admin_id``.
    # ``tenant_id`` retained on input for backward-compat read only; it is
    # ignored if both keys are present.  Removed entirely in PR #101.
    tenant_id: str | None = None
    admin_id: str | None = None
    domain_id: str | None = None
    agent_id: str | None = None


class SessionRead(TenantAdminOutputAlias):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    # Arc 9.2 PR #98: mirrored from tenant_id by TenantAdminOutputAlias so
    # response consumers can read either key.  Becomes the canonical key
    # in PR #101 when tenant_id is dropped.
    admin_id: str | None = None
    domain_id: str
    agent_id: str | None
    user_id: str | None
    channel: str
    status: str
    created_at: datetime
    updated_at: datetime


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: str
    role: str
    content: str
    trace_id: str | None
    created_at: datetime
    updated_at: datetime