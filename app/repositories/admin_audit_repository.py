"""
AdminAuditLog repository — writes and queries for the admin audit trail.

Step 24.5 (File 6.5b). Thin wrapper around app.models.admin_audit_log.

Usage pattern:

    # At the route / service boundary (File 9 / 10 / 11 / 12), build
    # the AuditContext once from request.state:
    ctx = AuditContext.from_request(request)

    # Pass ctx down into repository mutation methods. The repos call
    # AdminAuditRepository.record(...) inside the same DB transaction
    # as the mutation so audit rows can never drift out of sync with
    # the mutations they describe.

Domain-agnostic: no imports from app/domain/, no vertical branching,
no hardcoded role names.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.models.admin_audit_log import (
    ALLOWED_ACTIONS,
    ALLOWED_RESOURCE_TYPES,
    AdminAuditLog,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# AuditContext — the actor identity captured at the request boundary
# ---------------------------------------------------------------------

SYSTEM_ACTOR_TENANT = "platform"


@dataclass(frozen=True)
class AuditContext:
    """Snapshot of WHO is performing an admin action.

    Captured once at the route/service boundary, then passed by value
    through every repository mutation call. Repositories never read
    request.state directly — that keeps them testable and HTTP-free.

    Use AuditContext.system() for background jobs and cascades that
    have no HTTP caller (retention purges, scheduled deactivations).
    """

    actor_key_prefix: str | None = None
    actor_permissions: tuple[str, ...] = field(default_factory=tuple)
    actor_label: str | None = None

    # The tenant the ACTOR's key is scoped to. Not necessarily the
    # tenant of the resource being mutated — ScopePolicy already
    # enforces that they match (or that the actor is platform_admin).
    actor_tenant_id: str | None = None

    @classmethod
    def from_request(cls, request) -> "AuditContext":
        """Build an AuditContext from a FastAPI Request whose state
        has been populated by app.middleware.auth."""
        state = getattr(request, "state", None)
        if state is None:
            return cls.system()

        perms = getattr(state, "permissions", None) or ()
        if isinstance(perms, str):  # defensive
            perms = tuple(p.strip() for p in perms.split(",") if p.strip())
        else:
            perms = tuple(perms)

        return cls(
            actor_key_prefix=getattr(state, "key_prefix", None),
            actor_permissions=perms,
            actor_label=getattr(state, "actor_label", None),
            actor_tenant_id=getattr(state, "tenant_id", None),
        )

    @classmethod
    def system(cls, label: str = "system") -> "AuditContext":
        """For background jobs, migrations, retention cascades."""
        return cls(
            actor_key_prefix=None,
            actor_permissions=("system",),
            actor_label=label,
            actor_tenant_id=SYSTEM_ACTOR_TENANT,
        )
    
    @classmethod
    def worker(cls, task_id: str, actor_key_prefix: str | None) -> "AuditContext":
        """For async worker tasks (Step 27b+).

        Preserves the enqueuing API key's prefix for audit linkage so
        every worker-written row traces back to the HTTP caller that
        triggered the enqueue. Distinguished from system() by the
        ('worker',) permissions tuple.
        """
        return cls(
            actor_key_prefix=actor_key_prefix,
            actor_permissions=("worker",),
            actor_label=f"worker:{task_id}",
            actor_tenant_id=None,  # set by caller if known
        )

    @property
    def permissions_str(self) -> str | None:
        if not self.actor_permissions:
            return None
        return ",".join(self.actor_permissions)


# ---------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------

class AdminAuditRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ---------------------------------------------------------------
    # Write
    # ---------------------------------------------------------------

    def record(
        self,
        *,
        ctx: AuditContext,
        tenant_id: str,
        action: str,
        resource_type: str,
        resource_pk: int | None = None,
        resource_natural_id: str | None = None,
        domain_id: str | None = None,
        agent_id: str | None = None,
        luciel_instance_id: int | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        note: str | None = None,
        autocommit: bool = False,
    ) -> AdminAuditLog:
        """Append a single audit row.

        Defaults to autocommit=False so the caller's mutation and its
        audit row commit atomically. Background jobs that aren't
        already inside a transaction can pass autocommit=True.

        Validates action / resource_type against the advisory
        allow-lists. If you need a new action or resource, extend the
        tuples in app.models.admin_audit_log — don't suppress the
        error here, it's how we catch typos that would otherwise
        silently fragment the audit trail.
        """
        if action not in ALLOWED_ACTIONS:
            raise ValueError(
                f"Unknown audit action {action!r}; "
                f"extend ALLOWED_ACTIONS in app.models.admin_audit_log."
            )
        if resource_type not in ALLOWED_RESOURCE_TYPES:
            raise ValueError(
                f"Unknown audit resource_type {resource_type!r}; "
                f"extend ALLOWED_RESOURCE_TYPES in app.models.admin_audit_log."
            )

        row = AdminAuditLog(
            actor_key_prefix=ctx.actor_key_prefix,
            actor_permissions=ctx.permissions_str,
            actor_label=ctx.actor_label,
            tenant_id=tenant_id or SYSTEM_ACTOR_TENANT,
            domain_id=domain_id,
            agent_id=agent_id,
            luciel_instance_id=luciel_instance_id,
            action=action,
            resource_type=resource_type,
            resource_pk=resource_pk,
            resource_natural_id=resource_natural_id,
            before_json=before,
            after_json=after,
            note=note,
        )
        self.db.add(row)
        if autocommit:
            self.db.commit()
            self.db.refresh(row)
        else:
            self.db.flush()

        logger.info(
            "AUDIT tenant=%s action=%s resource=%s pk=%s nat=%s "
            "actor=%s perms=%s",
            row.tenant_id,
            row.action,
            row.resource_type,
            row.resource_pk,
            row.resource_natural_id,
            row.actor_key_prefix or "<system>",
            row.actor_permissions or "<none>",
        )
        return row

    # ---------------------------------------------------------------
    # Read (used by dashboards / tenant self-service / forensics)
    # ---------------------------------------------------------------

    def list_for_tenant(
        self,
        *,
        tenant_id: str,
        limit: int = 200,
        offset: int = 0,
        actions: Iterable[str] | None = None,
        resource_types: Iterable[str] | None = None,
    ) -> list[AdminAuditLog]:
        """Most-recent-first listing for one tenant. Caller
        (ScopePolicy in Files 9/10) enforces that only the owning
        tenant or platform_admin can hit this."""
        query = self.db.query(AdminAuditLog).filter(
            AdminAuditLog.tenant_id == tenant_id
        )
        if actions:
            query = query.filter(AdminAuditLog.action.in_(tuple(actions)))
        if resource_types:
            query = query.filter(
                AdminAuditLog.resource_type.in_(tuple(resource_types))
            )
        return (
            query.order_by(AdminAuditLog.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

    def list_for_resource(
        self,
        *,
        resource_type: str,
        resource_pk: int,
        limit: int = 100,
    ) -> list[AdminAuditLog]:
        """All audit events for one specific resource over time —
        powers the 'history' view on a LucielInstance / Agent / etc.
        detail page in the Step 31 dashboard."""
        return (
            self.db.query(AdminAuditLog)
            .filter(
                AdminAuditLog.resource_type == resource_type,
                AdminAuditLog.resource_pk == resource_pk,
            )
            .order_by(AdminAuditLog.created_at.desc())
            .limit(limit)
            .all()
        )

    def list_for_actor(
        self,
        *,
        actor_key_prefix: str,
        limit: int = 200,
    ) -> list[AdminAuditLog]:
        """Forensic view: everything this key has ever done."""
        return (
            self.db.query(AdminAuditLog)
            .filter(AdminAuditLog.actor_key_prefix == actor_key_prefix)
            .order_by(AdminAuditLog.created_at.desc())
            .limit(limit)
            .all()
        )


# ---------------------------------------------------------------------
# Helpers — shared diff builder used by repo-layer patches in 6.5c
# ---------------------------------------------------------------------

def diff_updated_fields(
    before: dict[str, Any],
    after: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Given before/after snapshots, return only the keys whose values
    actually changed. Keeps audit rows small — a PATCH that touches
    one field logs one field, not the whole row.

    Values must be JSON-serialisable. For non-serialisable fields
    (e.g. datetime objects), cast at the call site.
    """
    before_diff: dict[str, Any] = {}
    after_diff: dict[str, Any] = {}
    all_keys = set(before.keys()) | set(after.keys())
    for key in all_keys:
        b = before.get(key)
        a = after.get(key)
        if b != a:
            before_diff[key] = b
            after_diff[key] = a
    return before_diff, after_diff