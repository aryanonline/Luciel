"""
ScopeAssignment repository -- data-access layer for the durable
User<->(tenant, domain, role) binding.

Step 24.5b. Wraps app.models.scope_assignment.ScopeAssignment.

Q6 resolution: "Data lives with scope, not person. Users + scope
assignments + mandatory key rotation + immutable audit log."

Scope of responsibility:
- Pure CRUD. No ScopePolicy calls, no business-rule checks, no HTTP
  exceptions. Callers (ScopeAssignmentService / route handlers) handle
  those.
- Audit-row emission is INSIDE this layer, in the same DB transaction
  as the mutation, so audit rows can never drift out of sync. Same
  doctrine as agent_repository / user_repository.
- Mandatory key rotation on assignment end (Q6 element) lives in
  ScopeAssignmentService (Commit 2), NOT here -- the cascade walks
  Agent + ApiKey + LucielInstance and that hierarchy logic belongs in
  the service, not the repository. This repo only ends the assignment
  row itself; the service orchestrates the rotation.
- Promotion / demotion / reassignment / departure are end-and-recreate
  operations. The repo does not implement "promote" as a single call;
  the service composes end_assignment() + create() in one transaction.

Audit semantics for ScopeAssignment mutations:
- AdminAuditLog.tenant_id is set to the assignment's own tenant_id.
  Per the C decision: assignment lifecycle events are tenant-level
  events (a promotion happens within a brokerage), not platform-level
  events (which is what User CRUD is). Tenant admins reading their
  own audit log will see assignment events; cross-tenant aggregation
  is platform-admin-only.

Domain-agnostic: no imports from app/domain/, no vertical branching.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, text as _sa_text
from sqlalchemy.orm import Session

from app.models.admin_audit_log import (
    ACTION_CREATE,
    ACTION_DEACTIVATE,
    RESOURCE_SCOPE_ASSIGNMENT,
)
from app.models.scope_assignment import EndReason, ScopeAssignment
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)

logger = logging.getLogger(__name__)


class ScopeAssignmentRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ---------------------------------------------------------------
    # Class-level constants
    # ---------------------------------------------------------------

    # ScopeAssignment is intentionally NOT mass-updatable. Lifecycle
    # changes (promotion / demotion / reassignment / departure) flow
    # through end_assignment() so each transition emits a distinct,
    # filterable audit row. The only "patches" we permit:
    # - end_assignment(): sets ended_at, ended_reason, ended_note,
    #                     ended_by_api_key_id, active=False
    # - administrative deactivation via the same path with
    #                     ended_reason=DEACTIVATED
    #
    # Per Q6 doctrine: never UPDATE in place; always end-and-recreate.

    # ---------------------------------------------------------------
    # Create
    # ---------------------------------------------------------------

    def create(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: str,
        domain_id: str,
        role: str,
        started_at: datetime | None = None,
        autocommit: bool = True,
        audit_ctx: AuditContext | None = None,
    ) -> ScopeAssignment:
        """Insert a new ScopeAssignment row.

        Caller (ScopeAssignmentService) is expected to:
        1. Verify the User exists and is active.
        2. Verify (tenant_id, domain_id) exist and are active.
        3. Verify the calling key has admin scope at-or-above
           (tenant_id, domain_id) per Invariant 5.
        4. Optionally end any conflicting active assignment for the
           same User in the same tenant before calling create() --
           this is the "promotion" composition pattern.

        autocommit=False lets the service compose this with
        end_assignment() in a single transaction (the promotion txn).

        started_at defaults to server now() if omitted (matches the
        column server_default). Override only for historical backfills.
        """
        assignment = ScopeAssignment(
            user_id=user_id,
            tenant_id=tenant_id,
            domain_id=domain_id,
            role=role,
            started_at=started_at,  # None -> DB server_default fires
            active=True,
        )
        self.db.add(assignment)
        self.db.flush()  # assigns assignment.id, enables audit write before commit

        if audit_ctx is not None:
            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                tenant_id=tenant_id,
                action=ACTION_CREATE,
                resource_type=RESOURCE_SCOPE_ASSIGNMENT,
                resource_pk=None,  # ScopeAssignment PK is UUID
                resource_natural_id=str(assignment.id),
                domain_id=domain_id,
                after={
                    "id": str(assignment.id),
                    "user_id": str(user_id),
                    "tenant_id": tenant_id,
                    "domain_id": domain_id,
                    "role": role,
                    "active": True,
                },
                autocommit=False,
            )

        if autocommit:
            self.db.commit()
            self.db.refresh(assignment)

        logger.info(
            "ScopeAssignment created id=%s user=%s tenant=%s domain=%s role=%s",
            assignment.id,
            user_id,
            tenant_id,
            domain_id,
            role,
        )
        return assignment

    # ---------------------------------------------------------------
    # Read
    # ---------------------------------------------------------------

    def get_by_pk(self, pk: uuid.UUID) -> ScopeAssignment | None:
        return (
            self.db.query(ScopeAssignment)
            .filter(ScopeAssignment.id == pk)
            .first()
        )

    def list_for_user(
        self,
        user_id: uuid.UUID,
        *,
        active_only: bool = False,
    ) -> list[ScopeAssignment]:
        """All assignments this User has held.

        active_only=True filters to currently-active assignments
        (ended_at IS NULL AND active=True), used by hot-path "what
        roles does this user currently hold" queries. Hits the partial
        index ix_scope_assignments_user_id_active.

        active_only=False returns full role history -- used by PIPEDA
        access-request flows and tenant-admin role-history dashboards.
        Sorted by started_at ascending so history reads chronologically.

        Arc 9 C21 -- RLS-aware discovery path
        --------------------------------------
        ``scope_assignments`` has FORCE ROW LEVEL SECURITY with
        policies gating visibility on ``app.admin_id``. Several
        callers invoke list_for_user BEFORE app.admin_id has been
        set -- the cookied /billing/me handler, the upgrade /
        downgrade endpoints, the invite-acceptance path, and the
        audit_role_change helper -- because the whole point of those
        reads is to *discover* which tenant the user belongs to.

        Under FORCE RLS the direct ORM query returns [] silently in
        that window, which surfaces to the frontend as ``tenant_id=""``
        in /billing/me and cascades into 405 / 422 errors on the
        downstream admin endpoints.

        The fix mirrors the C20 single-row resolver: when
        ``app.admin_id`` is empty we route the read through
        ``public.arc9_c21_list_scopes_for_user(uuid, boolean)`` -- a
        SECURITY DEFINER function owned by luciel_ops -- and hydrate
        the rows into ScopeAssignment ORM instances. When the GUC is
        already set, the existing ORM path runs and RLS evaluates
        normally.

        The SECDEF escape hatch is read-only and returns ONLY one
        user's rows (no cross-user enumeration); see
        alembic/versions/arc9_c21_list_scopes_secdef.py for the full
        doctrine.
        """
        # Step 1: probe the tenant GUC. If unset/empty we're in the
        # discovery window and must use the SECDEF function to avoid
        # the silent-empty-list RLS trap.
        try:
            admin_id_guc = self.db.execute(
                _sa_text("SELECT current_setting('app.admin_id', true)")
            ).scalar()
        except Exception:  # pragma: no cover - extremely defensive
            admin_id_guc = None

        if not admin_id_guc:
            return self._list_for_user_secdef(
                user_id, active_only=active_only
            )

        # Step 2: GUC is set -- the normal ORM path with RLS in effect.
        query = self.db.query(ScopeAssignment).filter(
            ScopeAssignment.user_id == user_id
        )
        if active_only:
            query = query.filter(
                ScopeAssignment.ended_at.is_(None),
                ScopeAssignment.active.is_(True),
            )
        return query.order_by(ScopeAssignment.started_at.asc()).all()

    def _list_for_user_secdef(
        self,
        user_id: uuid.UUID,
        *,
        active_only: bool,
    ) -> list[ScopeAssignment]:
        """Read scope rows via the C21 SECURITY DEFINER function.

        Hydrates the returned columns into transient (detached)
        ScopeAssignment instances so callers receive the same shape
        they expect from the ORM path. The instances are NOT added to
        the session -- they represent a read-only view and should not
        be mutated.
        """
        rows = self.db.execute(
            _sa_text(
                "SELECT id, user_id, tenant_id, domain_id, role, "
                "started_at, ended_at, ended_reason, ended_note, "
                "ended_by_api_key_id, active "
                "FROM public.arc9_c21_list_scopes_for_user(:uid, :ao)"
            ),
            {"uid": str(user_id), "ao": active_only},
        ).all()

        out: list[ScopeAssignment] = []
        for r in rows:
            sa = ScopeAssignment(
                id=r.id,
                user_id=r.user_id,
                tenant_id=r.tenant_id,
                domain_id=r.domain_id,
                role=r.role,
                started_at=r.started_at,
                ended_at=r.ended_at,
                ended_reason=(
                    EndReason(r.ended_reason)
                    if r.ended_reason is not None
                    else None
                ),
                ended_note=r.ended_note,
                ended_by_api_key_id=r.ended_by_api_key_id,
                active=r.active,
            )
            # No db.add() above -- instances stay transient by default,
            # which is exactly what we want: they represent a
            # SECDEF-sourced read view that must never be flushed back.
            out.append(sa)
        return out

    def list_for_tenant(
        self,
        tenant_id: str,
        *,
        active_only: bool = False,
    ) -> list[ScopeAssignment]:
        """All assignments under one tenant.

        Hits ix_scope_assignments_tenant_id_active when active_only=True.
        Tenant admins use this for "who currently works at this brokerage";
        platform admins use the unfiltered version for full tenant history.
        """
        query = self.db.query(ScopeAssignment).filter(
            ScopeAssignment.tenant_id == tenant_id
        )
        if active_only:
            query = query.filter(
                ScopeAssignment.ended_at.is_(None),
                ScopeAssignment.active.is_(True),
            )
        return query.order_by(ScopeAssignment.started_at.asc()).all()

    def get_active_for_user_in_tenant(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: str,
    ) -> ScopeAssignment | None:
        """Find the User's currently-active assignment within one tenant.

        Used by the promotion path: "before creating Sarah's new
        team_lead assignment, find and end her current listings_agent
        assignment in the same tenant." Hits the multi-column partial
        index ix_scope_assignments_user_tenant_domain_role_active --
        though typically only one active assignment per (user, tenant)
        in steady state, which is what the service-layer doctrine
        enforces.
        """
        return (
            self.db.query(ScopeAssignment)
            .filter(
                and_(
                    ScopeAssignment.user_id == user_id,
                    ScopeAssignment.tenant_id == tenant_id,
                    ScopeAssignment.ended_at.is_(None),
                    ScopeAssignment.active.is_(True),
                )
            )
            .order_by(ScopeAssignment.started_at.desc())
            .first()
        )
    # ---------------------------------------------------------------
    # End assignment (the lifecycle action)
    # ---------------------------------------------------------------

    def end_assignment(
        self,
        *,
        assignment_id: uuid.UUID,
        reason: EndReason,
        note: str | None = None,
        ended_by_api_key_id: int | None = None,
        autocommit: bool = True,
        audit_ctx: AuditContext | None = None,
    ) -> ScopeAssignment | None:
        """End an active assignment. Returns None if not found.

        Sets ended_at = now(UTC), ended_reason = reason, ended_note,
        ended_by_api_key_id, active = False -- all in one statement.

        Idempotent: if the assignment is already ended (ended_at is
        not NULL), returns the row unchanged and emits no audit row.
        This protects against duplicate end calls during cascades
        (e.g. tenant-deactivation cascade hitting the same assignment
        twice through different paths).

        Does NOT cascade to ApiKey rotation -- that lives in
        ScopeAssignmentService.end_assignment() per Q6 doctrine, where
        the service walks Agent -> ApiKey + LucielInstance -> ApiKey
        and emits the per-key KEY_ROTATED_ON_ROLE_CHANGE audit rows.
        Same hierarchy-logic-in-one-place doctrine as
        (deleted V1) AgentRepository.deactivate not cascading to LucielInstance.

        autocommit=False lets the service compose this with create()
        in a single transaction (the promotion txn).
        """
        assignment = self.get_by_pk(assignment_id)
        if assignment is None:
            return None

        # Idempotency guard: already-ended assignments are a no-op.
        # Don't emit a duplicate audit row, don't update timestamps.
        if assignment.ended_at is not None:
            logger.info(
                "ScopeAssignment end_assignment no-op (already ended) "
                "id=%s ended_at=%s",
                assignment.id,
                assignment.ended_at,
            )
            return assignment

        # Snapshot the before-state for the audit diff. Only the
        # lifecycle columns matter -- identity columns don't change.
        before_snapshot = {
            "active": assignment.active,
            "ended_at": None,
            "ended_reason": None,
            "ended_note": None,
            "ended_by_api_key_id": None,
        }

        # End-and-mark inactive in one logical step.
        now = datetime.now(timezone.utc)
        assignment.ended_at = now
        assignment.ended_reason = reason
        assignment.ended_note = note
        assignment.ended_by_api_key_id = ended_by_api_key_id
        assignment.active = False

        after_snapshot = {
            "active": False,
            "ended_at": now.isoformat(),
            "ended_reason": reason.value,
            "ended_note": note,
            "ended_by_api_key_id": ended_by_api_key_id,
        }

        if audit_ctx is not None:
            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                tenant_id=assignment.tenant_id,
                action=ACTION_DEACTIVATE,
                resource_type=RESOURCE_SCOPE_ASSIGNMENT,
                resource_pk=None,  # ScopeAssignment PK is UUID
                resource_natural_id=str(assignment.id),
                domain_id=assignment.domain_id,
                before=before_snapshot,
                after=after_snapshot,
                note=(
                    f"end_reason={reason.value}; note={note!r}"
                    if note
                    else f"end_reason={reason.value}"
                ),
                autocommit=False,
            )

        if autocommit:
            self.db.commit()
            self.db.refresh(assignment)

        logger.info(
            "ScopeAssignment ended id=%s user=%s tenant=%s reason=%s",
            assignment.id,
            assignment.user_id,
            assignment.tenant_id,
            reason.value,
        )
        return assignment