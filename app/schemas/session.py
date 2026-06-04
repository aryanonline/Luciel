from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SessionCreate(BaseModel):
    # Arc 12 EX1c — ``domain_id`` and ``agent_id`` removed from the
    # request body. V2 sessions are admin + instance scoped
    # (Architecture §3.7.2 / §3.7.3, Walls 3/4). Arc 12 EX3 dropped
    # both columns from the ``sessions`` table; no sentinel is
    # synthesised any more.
    user_id: str | None = None
    channel: str = "web"

    # Optional; usually comes from the API key's tenant binding.
    admin_id: str | None = None
    # Optional; required for platform-admin cross-tenant Instance binds.
    luciel_instance_id: int | None = None


class SessionRead(BaseModel):
    # Arc 12 EX1c / EX3 — ``domain_id`` and ``agent_id`` removed from
    # the public response projection. Arc 12 EX3 also dropped the
    # underlying columns from the ``sessions`` table.
    model_config = ConfigDict(from_attributes=True)

    id: str
    admin_id: str
    user_id: str | None
    channel: str
    status: str
    created_at: datetime
    updated_at: datetime
    # Rescan Tier-C §3.4.12 — human-controlled session mode fields.
    control_mode: str = "luciel"
    taken_over_by_user_id: uuid.UUID | None = None
    taken_over_at: datetime | None = None
    handed_back_at: datetime | None = None


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: str
    role: str
    content: str
    trace_id: str | None
    created_at: datetime
    updated_at: datetime
