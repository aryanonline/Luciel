from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SessionCreate(BaseModel):
    # Arc 12 EX1c — ``domain_id`` and ``agent_id`` removed from the
    # request body. V2 sessions are admin + instance scoped
    # (Architecture §3.7.2 / §3.7.3, Walls 3/4). The legacy
    # ``sessions.domain_id`` column is still NOT NULL (EX3 owns the
    # relax/drop) — the route synthesises a sentinel from
    # ``luciel_instance_id`` to keep the insert satisfied without
    # accepting the field at the API boundary.
    user_id: str | None = None
    channel: str = "web"

    # Optional; usually comes from the API key's tenant binding.
    admin_id: str | None = None
    # Optional; required for platform-admin cross-tenant Instance binds.
    luciel_instance_id: int | None = None


class SessionRead(BaseModel):
    # Arc 12 EX1c — ``domain_id`` and ``agent_id`` removed from the
    # public response projection. Underlying columns persist until EX3
    # drops them; the API surface no longer surfaces them.
    model_config = ConfigDict(from_attributes=True)

    id: str
    admin_id: str
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
