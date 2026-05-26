from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SessionCreate(BaseModel):
    # Only user_id and channel are provided by the client.
    user_id: str | None = None
    channel: str = "web"

    # These can optionally be provided but usually come from the API key.
    # If not provided, they come from what the API key allows.
    # Arc 9.2 PR #101: tenant_id collapsed into admin_id.  Only admin_id
    # is accepted now.
    admin_id: str | None = None
    domain_id: str | None = None
    agent_id: str | None = None


class SessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    admin_id: str
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
