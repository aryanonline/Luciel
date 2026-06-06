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
# Arc 5 Path A — AgentRepository deleted at Commit A5. The User-deactivate
# cascade below previously soft-deactivated every Agent under the User; V2
# eliminates the Agent layer so the cascade collapses to (a) end every
# active ScopeAssignment (which is the V2 source of truth for "is the User
# bound to this Admin") and (b) deactivate the User row. The Agent step is
# a no-op until Revision C drops the legacy agents table entirely.
from app.repositories.user_repository import UserRepository
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
        """Soft-deactivate a User and rotate its credentials.

        Single-login model (Locked Decision #19, Architecture §3.7.1): an
        account has exactly one operating identity (the account_owner), and
        there are no ScopeAssignment / team-seat rows to cascade — those were
        excised in the audit-and-alignment phase (Unit 1). Deactivation now:

          1. Soft-deactivates the User row (UserRepository.deactivate emits the
             USER_DEACTIVATED audit row with the reason in the `note` field).

        Mandatory key rotation on deactivation remains a security requirement
        (no window where a deactivated User has working credentials); it is
        enforced by ApiKeyService at the credential layer / lifecycle path.

        Args:
        - reason: business justification (10-500 chars). Recorded in the User
          audit row's `note` field.

        Raises:
        - UserNotFoundError if the row is missing or already inactive.
        """
        user_repo = UserRepository(self.db)

        user = user_repo.get_by_pk(user_id)
        if user is None:
            raise UserNotFoundError(f"user not found: {user_id}")
        if not user.active:
            raise UserNotFoundError(
                f"user already inactive: {user_id}"
            )

        # Soft-deactivate the User row.
        deactivated = user_repo.deactivate(
            user_id=user_id,
            reason=reason,
            audit_ctx=audit_ctx,
        )
        if deactivated is None:
            self.db.rollback()
            raise UserNotFoundError(
                f"user disappeared during deactivation: {user_id}"
            )

        self.db.commit()

        logger.info(
            "User deactivated id=%s reason=%s",
            user_id,
            reason[:80] if reason else None,
        )
        return deactivated