"""
Memory item schemas.

Step 28 - Commit 8b-prereq-data-cascade-fix: MemoryRead added to support
admin endpoints for tenant-deactivation cascade walker. The walker calls
GET /api/v1/admin/memory-items?admin_id=X to enumerate rows it must
soft-deactivate before PATCHing the parent tenant to active=False.

The schema mirrors the underlying MemoryItem model but excludes the
`content` column - admin tooling does not need the memory body to make
deactivation decisions, and content may be sensitive (extracted user
preferences/identity facts). Listing endpoint stays metadata-only.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MemoryRead(BaseModel):
    # Arc 12 EX1c — ``agent_id`` removed from the public projection.
    # V2 memory rows are admin + instance + user scoped (Architecture
    # §3.7.3). The ``memory_items.agent_id`` column persists until EX3
    # drops it; new rows are written with NULL (EX1b).
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: str
    admin_id: str
    category: str
    source_session_id: str | None
    active: bool
    message_id: int | None
    luciel_instance_id: int | None
    actor_user_id: uuid.UUID | None
    created_at: datetime