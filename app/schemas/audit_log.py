"""
Audit-log API schemas.

Read-only response models for the GET /api/v1/admin/audit-log
endpoint family. The admin_audit_logs table is append-only at the DB
layer (Phase 2 worker role swap completes this); the API surface is
also strictly read-only — there is no PATCH/DELETE route. This file
only exposes Read models.

Every field is sourced from app.models.admin_audit_log.AdminAuditLog
verbatim. No PII derivation, no joins to user-supplied content. The
actor identity is the 12-char key_prefix only, never the raw key.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AdminAuditLogRead(BaseModel):
    """Single audit row, as exposed via the read API.

    Note: actor_key_prefix is the 12-char prefix only — the raw API
    key is never persisted nor returned. Permissions are captured
    verbatim at action time, so a re-scoped key shows the permissions
    it HELD WHEN it acted.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime

    # WHO
    actor_key_prefix: str | None
    actor_permissions: str | None
    actor_label: str | None

    # WHERE
    tenant_id: str
    domain_id: str | None
    agent_id: str | None
    luciel_instance_id: int | None

    # WHAT
    action: str
    resource_type: str
    resource_pk: int | None
    resource_natural_id: str | None

    # DIFF
    before_json: dict[str, Any] | None
    after_json: dict[str, Any] | None
    note: str | None


class AdminAuditLogPage(BaseModel):
    """Paginated audit-log response.

    Pagination is offset/limit by design — the volume is small enough
    (admin mutations only, not chat traces) that cursor-based pagination
    isn't worth the complexity. Hard-capped at limit<=500 to keep
    queries fast and prevent unbounded scans even with the
    tenant_time / actor_time / resource indexes covering most filters.
    """

    items: list[AdminAuditLogRead]
    limit: int = Field(..., ge=1, le=500)
    offset: int = Field(..., ge=0)
    returned: int = Field(..., ge=0)
