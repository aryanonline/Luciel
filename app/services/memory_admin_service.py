"""
Memory admin service.

Step 28 - Commit 8b-prereq-data-cascade-fix.

Admin-path memory operations: list and soft-deactivate memory_items
rows for tenant-deactivation cleanup. Distinct from app.memory.service
(MemoryService), which is the chat-path companion handling extraction
and retrieval.

Why a separate class:
- MemoryService takes a ModelRouter dependency (for extraction); admin
  paths do not need that.
- Separation of concerns: chat-path mutations vs admin-path mutations
  emit different audit signals and have different auth boundaries.
- Mirrors the existing pattern (ApiKeyService is admin-path,
  ChatService/SessionService are chat-path).

Soft-delete contract: MemoryItem.active is the lifecycle column. This
service flips active=True -> active=False via a single UPDATE,
emitting an ACTION_DEACTIVATE audit row in the same transaction
(Invariant 4: audit-before-commit).

The walker (scripts/cleanup_residue_tenant.ps1) calls this service via
two new admin endpoints in app/api/v1/admin.py:
  - GET    /api/v1/admin/memory-items?tenant_id=X
  - DELETE /api/v1/admin/memory-items/{id}

Both endpoints are platform_admin gated. Tenant-scoped admin keys cannot
list or deactivate memory_items - admin tooling is platform-only.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.admin_audit_log import (
    ACTION_DEACTIVATE,
    RESOURCE_MEMORY,
)
from app.models.memory import MemoryItem
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
    SYSTEM_ACTOR_TENANT,
)

logger = logging.getLogger(__name__)


class MemoryAdminService:
    """Admin-path memory operations: list + soft-deactivate."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def list_memories_for_tenant(
        self,
        *,
        tenant_id: str,
        active_only: bool = False,
    ) -> list[MemoryItem]:
        """List memory items for a tenant.

        Default returns ALL rows (active + inactive) so the walker can
        emit one log line per row including already-inactive ones for
        full audit traceability. Set active_only=True to filter.
        """
        stmt = (
            select(MemoryItem)
            .where(MemoryItem.tenant_id == tenant_id)
            .order_by(MemoryItem.created_at.desc())
        )
        if active_only:
            stmt = stmt.where(MemoryItem.active.is_(True))
        return list(self.db.scalars(stmt).all())

    def deactivate_memory(
        self,
        memory_id: int,
        *,
        audit_ctx: AuditContext | None = None,
    ) -> bool:
        """Deactivate a single memory item.

        Mirrors ApiKeyService.deactivate_key (Step 28 D5 pattern):
        emits an ACTION_DEACTIVATE audit row in the same transaction
        as the active=False UPDATE (Invariant 4).

        Returns True on success, False if the row does not exist.
        Idempotent on already-inactive rows: still emits an audit row
        with before={"active": False} after={"active": False} so the
        operator log shows the visit. The walker pre-filters to active
        rows, so this idempotent branch is defensive only.
        """
        item = self.db.get(MemoryItem, memory_id)
        if item is None:
            return False

        was_active = bool(item.active)
        item.active = False

        audit_repo = AdminAuditRepository(self.db)
        audit_repo.record(
            ctx=audit_ctx if audit_ctx is not None else AuditContext.system(
                label="deactivate_memory"
            ),
            tenant_id=item.tenant_id or SYSTEM_ACTOR_TENANT,
            action=ACTION_DEACTIVATE,
            resource_type=RESOURCE_MEMORY,
            resource_pk=item.id,
            resource_natural_id=None,
            domain_id=None,
            agent_id=item.agent_id,
            luciel_instance_id=item.luciel_instance_id,
            before={"active": was_active},
            after={"active": False},
            note=None,
            autocommit=False,
        )

        self.db.commit()
        logger.info("Deactivated memory id=%d tenant=%s", memory_id, item.tenant_id)
        return True