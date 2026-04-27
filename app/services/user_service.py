"""
User service.

Step 24.5b. Orchestrates User lifecycle and the cascade-on-deactivation
required by Q6 resolution: "Data lives with scope, not person. Users +
scope assignments + mandatory key rotation + immutable audit log."

Service-layer responsibilities (vs. repository):
- Authorization gates (e.g. only platform-admin may set synthetic=True
  on User creation -- service raises PlatformAdminRequiredError, route
  layer translates to 403).
- Cross-aggregate cascades (deactivating a User ends every active
  ScopeAssignment and rotates every bound ApiKey -- service composes
  ScopeAssignmentService + ApiKeyService).
- Pre-flight validation (email_exists check before issuing UPDATE so
  the route layer gets a clean 409 instead of an IntegrityError).
- Domain exceptions only -- no FastAPI / HTTPException imports here.
  Route layer maps domain exceptions to HTTP status codes.

Transaction discipline:
- Every mutation method takes audit_ctx: AuditContext | None and
  passes it through to the repositories so audit rows land in the
  same txn as the mutation (Invariant 4).
- deactivate_user uses auto_commit=False on every nested repository
  call, then commits ONCE at the end of the cascade. If any step
  fails, the entire cascade rolls back -- the User stays active
  rather than ending up in a half-deactivated state.

Domain-agnostic: no imports from app/domain/, no vertical branching.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.repositories.admin_audit_repository import AuditContext
from app.repositories.agent_repository import AgentRepository
from app.repositories.scope_assignment_repository import ScopeAssignmentRepository
from app.repositories.user_repository import UserRepository
from app.models.scope_assignment import EndReason
from app.models.user import User
from app.schemas.user import UserCreate, UserUpdate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Domain exceptions -- route layer translates to HTTP
# ---------------------------------------------------------------------

class UserError(Exception):
    """Base class for User service exceptions."""


class UserNotFoundError(UserError):
    """Raised by get_user / update_user / deactivate_user when row is
    missing or inactive (unless include_inactive=True is passed)."""


class EmailAlreadyExistsError(UserError):
    """Raised by create_user / update_user when an email collision is
    detected via the LOWER(email) unique index pre-flight."""


class PlatformAdminRequiredError(UserError):
    """Raised by create_user when a non-platform-admin actor attempts
    to set synthetic=True. Service-layer authorization gate; route
    layer translates to 403."""


# ---------------------------------------------------------------------
# UserService
# ---------------------------------------------------------------------

class UserService:
    """Orchestrates User lifecycle. See module docstring for service vs.
    repository responsibility split."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ---------------------------------------------------------------
    # Create
    # ---------------------------------------------------------------

    def create_user(
        self,
        payload: UserCreate,
        *,
        actor_is_platform_admin: bool,
        audit_ctx: AuditContext | None = None,
    ) -> User:
        """Create a User from a validated UserCreate payload.

        Raises:
        - PlatformAdminRequiredError if payload.synthetic is True and
          the calling actor isn't platform-admin. Route layer translates
          to 403. Synthetic users are an internal Option B onboarding
          construct and must not be creatable by tenant admins via the
          public API -- it would let them forge identity audit trails.
        - EmailAlreadyExistsError if the email already exists (case-
          insensitive). Route layer translates to 409.
        """
        # Authorization gate: synthetic flag is platform-admin-only.
        if payload.synthetic and not actor_is_platform_admin:
            raise PlatformAdminRequiredError(
                "synthetic=True requires platform_admin permission"
            )

        repo = UserRepository(self.db)

        # Pre-flight uniqueness check so the route layer gets a clean
        # 409. The DB unique index is the source of truth, but checking
        # first lets us return a structured error rather than catching
        # IntegrityError. The schema already lowercases payload.email.
        if repo.email_exists(email=payload.email):
            raise EmailAlreadyExistsError(
                f"email already exists: {payload.email}"
            )

        return repo.create(
            email=payload.email,
            display_name=payload.display_name,
            synthetic=payload.synthetic,
            autocommit=True,
            audit_ctx=audit_ctx,
        )

    def get_or_create_by_email(
        self,
        *,
        email: str,
        display_name: str,
        synthetic: bool,
        audit_ctx: AuditContext | None = None,
    ) -> tuple[User, bool]:
        """Idempotent lookup-or-create by email.

        Returns (user, was_created). Used by OnboardingService when it
        needs to bind an Agent to a User and may not know whether the
        User already exists. Safe to call multiple times for the same
        email -- second call returns (existing_user, False).

        Bypasses the platform-admin synthetic gate because this entry
        point is service-internal -- only OnboardingService and other
        services should call it. Route handlers go through create_user.
        """
        repo = UserRepository(self.db)
        existing = repo.get_by_email(email)
        if existing is not None:
            logger.info(
                "User get_or_create_by_email -> existing id=%s email=%s",
                existing.id,
                email,
            )
            return existing, False

        created = repo.create(
            email=email,
            display_name=display_name,
            synthetic=synthetic,
            autocommit=True,
            audit_ctx=audit_ctx,
        )
        return created, True

    # ---------------------------------------------------------------
    # Read
    # ---------------------------------------------------------------

    def get_user(
        self,
        user_id: uuid.UUID,
        *,
        include_inactive: bool = False,
    ) -> User:
        """Fetch a User by PK. Raises UserNotFoundError if missing or
        (when include_inactive=False) inactive."""
        repo = UserRepository(self.db)
        user = repo.get_by_pk(user_id)
        if user is None:
            raise UserNotFoundError(f"user not found: {user_id}")
        if not include_inactive and not user.active:
            raise UserNotFoundError(f"user inactive: {user_id}")
        return user
    # ---------------------------------------------------------------
    # Update
    # ---------------------------------------------------------------

    def update_user(
        self,
        user_id: uuid.UUID,
        payload: UserUpdate,
        *,
        audit_ctx: AuditContext | None = None,
    ) -> User:
        """Apply a partial update to a User.

        Email uniqueness is pre-flighted via email_exists(exclude_user_id=...)
        so a collision returns a structured 409 instead of an
        IntegrityError. The schema already lowercases payload.email
        before we get here.

        Raises:
        - UserNotFoundError if the row is missing or inactive.
        - EmailAlreadyExistsError if payload.email collides with another
          User's email (case-insensitive).
        """
        repo = UserRepository(self.db)
        user = repo.get_by_pk(user_id)
        if user is None:
            raise UserNotFoundError(f"user not found: {user_id}")
        if not user.active:
            raise UserNotFoundError(f"user inactive: {user_id}")

        # Pre-flight email collision check, only if email is being
        # changed. Excludes the row being updated so unchanged-email
        # PATCH calls don't trip the "already exists" check on the
        # user's own current email.
        if payload.email is not None and payload.email != user.email:
            if repo.email_exists(
                email=payload.email,
                exclude_user_id=user_id,
            ):
                raise EmailAlreadyExistsError(
                    f"email already exists: {payload.email}"
                )

        # Build the kwargs for repo.update from non-None payload fields.
        # The repo's _UPDATABLE_FIELDS frozenset is the final filter --
        # any extra kwargs are silently ignored there.
        update_kwargs: dict[str, object] = {}
        if payload.display_name is not None:
            update_kwargs["display_name"] = payload.display_name
        if payload.email is not None:
            update_kwargs["email"] = payload.email

        return repo.update(user, audit_ctx=audit_ctx, **update_kwargs)

    # ---------------------------------------------------------------
    # Deactivate -- the cascade
    # ---------------------------------------------------------------

    def deactivate_user(
        self,
        user_id: uuid.UUID,
        *,
        reason: str,
        audit_ctx: AuditContext | None = None,
    ) -> User:
        """Soft-deactivate a User and cascade.

        Q6 resolution: deactivating a User must end every active
        ScopeAssignment for that User across all tenants AND rotate
        every ApiKey bound to any Agent under those assignments. All
        in the same transaction so:

        1. Audit rows are atomic with the mutations they describe
           (Invariant 4).
        2. There is no window where a deactivated User has working
           credentials (Q6 "mandatory key rotation", hard rotation
           with no grace period per Step 24.5b decision A).
        3. A failure mid-cascade rolls everything back -- the User
           stays active rather than half-deactivated.

        Cascade order (chosen so each step's audit row records the
        accurate prior state):

          a. End every active ScopeAssignment via
             ScopeAssignmentService.end_assignment(reason=DEACTIVATED).
             Each end_assignment call internally cascades to
             ApiKeyService.rotate_keys_for_agent for the bound Agent
             (mandatory key rotation per Q6).
          b. Soft-deactivate every Agent row bound to this User.
             AgentRepository.update with active=False, audit_ctx
             propagated so per-Agent audit rows land.
          c. Soft-deactivate the User row itself. UserRepository
             emits the USER_DEACTIVATED audit row.

        All repo calls run with autocommit=False; this method calls
        db.commit() exactly once at the end.

        Args:
        - reason: business justification (10-500 chars per
          UserDeactivate schema). Recorded in the User audit row's
          `note` field AND propagated to each ScopeAssignment's
          ended_note for cross-aggregate traceability.

        Raises:
        - UserNotFoundError if the row is missing or already inactive.
        """
        # Late imports to avoid circular-import risk between services.
        # ScopeAssignmentService and ApiKeyService both import some
        # repositories that ultimately touch User, so importing them
        # at module top can deadlock the import graph during early
        # SQLAlchemy mapper configuration. Service-internal late
        # imports keep the runtime behavior identical without forcing
        # a top-level reordering.
        from app.services.api_key_service import ApiKeyService
        from app.services.scope_assignment_service import ScopeAssignmentService

        user_repo = UserRepository(self.db)
        sa_repo = ScopeAssignmentRepository(self.db)
        agent_repo = AgentRepository(self.db)

        user = user_repo.get_by_pk(user_id)
        if user is None:
            raise UserNotFoundError(f"user not found: {user_id}")
        if not user.active:
            raise UserNotFoundError(
                f"user already inactive: {user_id}"
            )

        # ---- Step a: end every active ScopeAssignment ----
        # We compose ScopeAssignmentService here (not the repo
        # directly) because end_assignment() is the entry point that
        # carries the mandatory key rotation cascade per Q6. Calling
        # the repo's end_assignment skips the rotation, which would
        # leave deactivated-User keys working -- a security violation.
        sa_service = ScopeAssignmentService(self.db)
        active_assignments = sa_repo.list_for_user(
            user_id=user_id,
            active_only=True,
        )
        for assignment in active_assignments:
            sa_service.end_assignment(
                assignment_id=assignment.id,
                reason=EndReason.DEACTIVATED,
                note=reason,
                ended_by_api_key_id=None,  # cascade source is User-level
                autocommit=False,
                audit_ctx=audit_ctx,
            )

        # ---- Step b: soft-deactivate every Agent ----
        # Cross-tenant. AgentRepository.update emits per-Agent
        # ACTION_DEACTIVATE audit rows. We loop here rather than
        # issuing a bulk UPDATE because the audit-row contract is
        # one row per mutation (per Step 24.5 doctrine).
        active_agents = agent_repo.list_for_user(
            user_id=user_id,
            active_only=True,
        )
        for agent in active_agents:
            agent_repo.update(
                agent,
                audit_ctx=audit_ctx,
                active=False,
                updated_by=(
                    audit_ctx.actor_label if audit_ctx else None
                ),
            )

        # ---- Step c: soft-deactivate the User row ----
        # UserRepository.deactivate emits the USER_DEACTIVATED audit
        # row with the reason in the audit `note` field.
        deactivated = user_repo.deactivate(
            user_id=user_id,
            reason=reason,
            audit_ctx=audit_ctx,
        )

        # Sanity: deactivate() returns None only if the row was missing,
        # which we already guarded against. Defensive log for forensics.
        if deactivated is None:
            self.db.rollback()
            raise UserNotFoundError(
                f"user disappeared during cascade: {user_id}"
            )

        # All steps successful -- single commit at the cascade end.
        # AgentRepository.update auto-commits per the existing pattern;
        # this commit is a no-op if nothing remains pending. Kept
        # explicit so the cascade contract is readable.
        self.db.commit()

        logger.info(
            "User deactivated cascade complete id=%s "
            "assignments_ended=%d agents_deactivated=%d "
            "reason=%s",
            user_id,
            len(active_assignments),
            len(active_agents),
            reason[:80] if reason else None,
        )
        return deactivated