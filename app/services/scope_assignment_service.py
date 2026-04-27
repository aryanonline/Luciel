"""
ScopeAssignment service.

Step 24.5b. Orchestrates the role-binding lifecycle and the mandatory
key rotation cascade required by Q6 resolution: "Data lives with scope,
not person. Users + scope assignments + mandatory key rotation +
immutable audit log."

This is the most security-sensitive file in Step 24.5b. end_assignment()
is the single audit-clean entry point for promotion / demotion /
reassignment / departure / administrative deactivation. Every one of
those lifecycle transitions MUST go through here so the mandatory key
rotation cascade fires consistently. Direct calls to
ScopeAssignmentRepository.end_assignment skip the cascade and are a
security violation -- the repo path exists for service composition
inside the same txn, not as a public lifecycle entry point.

Service-layer responsibilities (vs. repository):
- Pre-flight validation (User active, tenant + domain exist, calling
  key has admin scope at-or-above target via ScopePolicy at the route
  layer).
- Cross-aggregate cascades (ending an assignment rotates every
  ApiKey bound to the affected Agent and any LucielInstance under
  that Agent -- service composes ApiKeyService.rotate_keys_for_agent).
- Atomic compositions (promote() = end-old + create-new in one txn).
- Domain exceptions only -- no FastAPI / HTTPException imports here.

Mandatory key rotation contract (Q6, hard rotation per Step 24.5b
decision A):
- Hard rotation. No grace period. No overlap window.
- Triggered automatically by end_assignment() for ANY end_reason.
- All ApiKeys bound to the (tenant, agent) pair of the ending
  assignment are deactivated in the same txn as the assignment end.
- Per-key KEY_ROTATED_ON_ROLE_CHANGE audit rows emitted by
  ApiKeyService.
- The `promote()` composite mints new keys for the new assignment
  BEFORE ending the old one (caller decides; this service exposes
  end_assignment and create_assignment as primitives).

Transaction discipline:
- end_assignment() runs the entire cascade (assignment end + key
  rotation) inside one txn. Single db.commit() at the end. If
  rotation fails, the assignment-end rolls back -- no half-state
  where the assignment ended but keys still work.
- promote() composes end_assignment + create in one txn, single
  commit at the end of both.

Domain-agnostic: no imports from app/domain/, no vertical branching.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.scope_assignment import EndReason, ScopeAssignment
from app.repositories.admin_audit_repository import AuditContext
from app.repositories.agent_repository import AgentRepository
from app.repositories.scope_assignment_repository import (
    ScopeAssignmentRepository,
)
from app.repositories.user_repository import UserRepository
from app.schemas.scope_assignment import (
    EndAssignmentRequest,
    ScopeAssignmentCreate,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Domain exceptions -- route layer translates to HTTP
# ---------------------------------------------------------------------

class ScopeAssignmentError(Exception):
    """Base class for ScopeAssignment service exceptions."""


class AssignmentNotFoundError(ScopeAssignmentError):
    """Raised when assignment_id is missing or already inactive on a
    path that requires an active assignment (e.g. end_assignment on
    an already-ended row goes through the repo's idempotent no-op
    rather than raising)."""


class AssignmentUserInactiveError(ScopeAssignmentError):
    """Raised by create_assignment when the target User is not active.
    Inactive Users cannot acquire new role bindings -- the deactivation
    cascade ended all their assignments and we don't reactivate them
    via this path."""


class AssignmentUserNotFoundError(ScopeAssignmentError):
    """Raised by create_assignment when the target User doesn't exist."""


# ---------------------------------------------------------------------
# ScopeAssignmentService
# ---------------------------------------------------------------------

class ScopeAssignmentService:
    """Orchestrates ScopeAssignment lifecycle. Mandatory key rotation
    cascade per Q6 resolution lives in end_assignment()."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ---------------------------------------------------------------
    # Create
    # ---------------------------------------------------------------

    def create_assignment(
        self,
        *,
        user_id: uuid.UUID,
        payload: ScopeAssignmentCreate,
        autocommit: bool = True,
        audit_ctx: AuditContext | None = None,
    ) -> ScopeAssignment:
        """Create a new ScopeAssignment for an existing active User.

        Pre-flight checks:
        - User exists and is active. Inactive Users cannot acquire
          new assignments -- raise AssignmentUserInactiveError.
        - tenant_id and domain_id are caller-validated at the route
          layer via ScopePolicy (this service does not re-validate
          tenant/domain existence; that's the route handler's job).

        autocommit=False lets promote() compose this with end_assignment
        in a single transaction.

        Note: started_at can be overridden via payload.started_at for
        historical backfills. Production callers leave it None to use
        the column server_default.
        """
        user_repo = UserRepository(self.db)
        sa_repo = ScopeAssignmentRepository(self.db)

        user = user_repo.get_by_pk(user_id)
        if user is None:
            raise AssignmentUserNotFoundError(
                f"user not found: {user_id}"
            )
        if not user.active:
            raise AssignmentUserInactiveError(
                f"cannot assign role to inactive user: {user_id}"
            )

        return sa_repo.create(
            user_id=user_id,
            tenant_id=payload.tenant_id,
            domain_id=payload.domain_id,
            role=payload.role,
            started_at=payload.started_at,
            autocommit=autocommit,
            audit_ctx=audit_ctx,
        )

    # ---------------------------------------------------------------
    # Read passthroughs (services don't add behavior here, but route
    # layer goes through the service for consistency and future
    # filtering hooks)
    # ---------------------------------------------------------------

    def get_assignment(
        self,
        assignment_id: uuid.UUID,
    ) -> ScopeAssignment | None:
        return ScopeAssignmentRepository(self.db).get_by_pk(assignment_id)

    def list_for_user(
        self,
        user_id: uuid.UUID,
        *,
        active_only: bool = False,
    ) -> list[ScopeAssignment]:
        return ScopeAssignmentRepository(self.db).list_for_user(
            user_id,
            active_only=active_only,
        )

    def list_for_tenant(
        self,
        tenant_id: str,
        *,
        active_only: bool = False,
    ) -> list[ScopeAssignment]:
        return ScopeAssignmentRepository(self.db).list_for_tenant(
            tenant_id,
            active_only=active_only,
        )
    # ---------------------------------------------------------------
    # End assignment -- the security-critical cascade
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
        """End an assignment AND rotate every bound ApiKey.

        Q6 resolution -- mandatory key rotation. Hard rotation, no
        grace period (Step 24.5b decision A). Triggered for any
        end_reason: PROMOTED / DEMOTED / REASSIGNED / DEPARTED /
        DEACTIVATED.

        Cascade order (single txn):

        1. Repository ends the assignment row (sets ended_at,
           ended_reason, ended_note, ended_by_api_key_id, active=False).
           Idempotent: if already ended, returns the row unchanged
           and emits no audit row -- we then SKIP key rotation as
           well (an already-ended assignment has already had its
           keys rotated; re-rotating would emit duplicate audit rows
           for keys that may have legitimately been re-issued since).
        2. Resolve the Agent rows bound to this (user, tenant) pair.
           In steady state there is exactly one active Agent per
           (user, tenant); historical rows from deactivate-and-recreate
           cycles may also appear and we rotate keys on all of them
           defensively.
        3. ApiKeyService.rotate_keys_for_agent for each Agent. The
           api-key service emits per-key KEY_ROTATED_ON_ROLE_CHANGE
           audit rows internally and returns the count rotated.

        Returns the ended ScopeAssignment row, or None if the
        assignment_id was not found at all (not the same as
        already-ended -- not-found returns None, already-ended
        returns the unchanged row).

        autocommit=False lets promote() compose this with create_assignment
        in a single transaction. UserService.deactivate_user also passes
        autocommit=False because it loops end_assignment over many
        assignments and commits once at the cascade end.
        """
        # Late import to avoid the circular-import path that would
        # otherwise form: ApiKeyService imports UserRepository for
        # something else -> UserRepository imports nothing risky now,
        # but the late import is defensive against future drift and
        # mirrors the same pattern used in UserService.deactivate_user.
        from app.services.api_key_service import ApiKeyService

        sa_repo = ScopeAssignmentRepository(self.db)
        agent_repo = AgentRepository(self.db)
        api_key_service = ApiKeyService(self.db)

        # ---- Step 1: end the assignment row (idempotent) ----
        assignment = sa_repo.get_by_pk(assignment_id)
        if assignment is None:
            # Distinguishes "not found" from "already ended". Service
            # contract: not-found returns None so callers can decide
            # whether 404 or silent-no-op is appropriate.
            return None

        was_already_ended = assignment.ended_at is not None

        ended = sa_repo.end_assignment(
            assignment_id=assignment_id,
            reason=reason,
            note=note,
            ended_by_api_key_id=ended_by_api_key_id,
            autocommit=False,  # we own the txn boundary in this method
            audit_ctx=audit_ctx,
        )

        # If repo end_assignment was a no-op (already ended), skip the
        # key rotation cascade. Re-rotating already-rotated keys would
        # emit duplicate audit rows AND could deactivate keys that
        # were legitimately re-issued for this Agent in a fresh
        # assignment after the original end.
        if was_already_ended:
            logger.info(
                "ScopeAssignmentService.end_assignment cascade skipped "
                "(already ended) id=%s",
                assignment_id,
            )
            if autocommit:
                self.db.commit()
            return ended

        # ---- Step 2: resolve the Agent rows for this (user, tenant) ----
        # In steady state we expect exactly one active Agent. We
        # rotate defensively across all matches (including inactive
        # historical rows) so deactivate-and-recreate cycles can't
        # leak working keys via stale Agent rows.
        target_agents = agent_repo.list_for_user(
            user_id=assignment.user_id,
            active_only=False,
        )
        target_agents = [
            a for a in target_agents if a.tenant_id == assignment.tenant_id
        ]

        # ---- Step 3: rotate keys for each Agent ----
        # ApiKeyService.rotate_keys_for_agent emits per-key
        # KEY_ROTATED_ON_ROLE_CHANGE audit rows internally. Returns
        # the count rotated for logging.
        total_rotated = 0
        for agent in target_agents:
            rotated_count = api_key_service.rotate_keys_for_agent(
                agent_id_pk=agent.id,
                reason=(
                    f"scope_assignment_ended:{reason.value}"
                    + (f":{note}" if note else "")
                ),
                audit_ctx=audit_ctx,
            )
            total_rotated += rotated_count

        if autocommit:
            self.db.commit()

        logger.info(
            "ScopeAssignmentService.end_assignment cascade complete "
            "id=%s user=%s tenant=%s reason=%s agents_visited=%d "
            "keys_rotated=%d",
            assignment_id,
            assignment.user_id,
            assignment.tenant_id,
            reason.value,
            len(target_agents),
            total_rotated,
        )
        return ended

    # ---------------------------------------------------------------
    # Promote -- atomic end-old + create-new composition
    # ---------------------------------------------------------------

    def promote(
        self,
        *,
        old_assignment_id: uuid.UUID,
        new_payload: ScopeAssignmentCreate,
        ended_by_api_key_id: int | None = None,
        end_reason: EndReason = EndReason.PROMOTED,
        end_note: str | None = None,
        audit_ctx: AuditContext | None = None,
    ) -> tuple[ScopeAssignment, ScopeAssignment]:
        """Atomic role transition: end old assignment + create new one.

        Returns (ended_old, created_new).

        Single transaction. If create fails, the assignment-end rolls
        back -- caller never sees a half-state where the User has no
        active role.

        Important: the OLD assignment's keys are rotated (Q6 mandatory
        rotation). The CALLER is responsible for minting new keys for
        the new assignment AFTER promote() returns -- this service
        owns assignment lifecycle, not key minting. The route layer
        composes promote() + ApiKeyService.create_key for the new
        scope.

        end_reason defaults to PROMOTED but callers can override
        (e.g. REASSIGNED for lateral moves within a tenant).

        Pre-flight: the old and new assignments must be for the SAME
        user_id. Cross-user "promotions" don't exist -- those are
        two separate operations (deactivate one user, create another).
        """
        sa_repo = ScopeAssignmentRepository(self.db)

        old = sa_repo.get_by_pk(old_assignment_id)
        if old is None:
            raise AssignmentNotFoundError(
                f"assignment not found: {old_assignment_id}"
            )

        # Service-layer integrity: same User on both sides of a promote.
        # The schema doesn't enforce this because new_payload doesn't
        # carry user_id (it comes from the URL path / inferred from old).
        # This guard prevents a malformed call from creating a "promotion"
        # that's actually a cross-user role transfer.
        new_user_id = old.user_id

        ended = self.end_assignment(
            assignment_id=old_assignment_id,
            reason=end_reason,
            note=end_note,
            ended_by_api_key_id=ended_by_api_key_id,
            autocommit=False,  # promote owns the txn boundary
            audit_ctx=audit_ctx,
        )
        if ended is None:
            # Should be unreachable -- we just fetched the row above.
            # Defensive: roll back to avoid half-state and raise.
            self.db.rollback()
            raise AssignmentNotFoundError(
                f"assignment disappeared during promote: "
                f"{old_assignment_id}"
            )

        created = sa_repo.create(
            user_id=new_user_id,
            tenant_id=new_payload.tenant_id,
            domain_id=new_payload.domain_id,
            role=new_payload.role,
            started_at=new_payload.started_at,
            autocommit=False,
            audit_ctx=audit_ctx,
        )

        self.db.commit()

        logger.info(
            "ScopeAssignmentService.promote complete user=%s "
            "old_id=%s old_role=%s new_id=%s new_role=%s reason=%s",
            new_user_id,
            old_assignment_id,
            old.role,
            created.id,
            created.role,
            end_reason.value,
        )
        return ended, created