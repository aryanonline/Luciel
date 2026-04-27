"""
User repository -- data-access layer for the User (durable identity) model.

Step 24.5b. Wraps app.models.user.User. Q6 resolution: "Data lives with
scope, not person. Users + scope assignments + mandatory key rotation +
immutable audit log."

Scope of responsibility:
- Pure CRUD. No ScopePolicy calls, no business-rule checks, no HTTP
  exceptions. Callers (UserService / route handlers) handle those.
- Audit-row emission is INSIDE this layer, in the same DB transaction
  as the mutation, so audit rows can never drift out of sync. Same
  doctrine as agent_repository.
- Cascade to ScopeAssignments + ApiKeys on user deactivation lives in
  UserService (Commit 2), NOT here -- keeps the hierarchy logic in one
  place. Same doctrine as agent_repository.deactivate not cascading to
  LucielInstance.
- Email is the natural lookup key. Case-insensitive uniqueness enforced
  via LOWER(email) expression index landed in the migration (File 1.9).
  All email comparisons here go through func.lower() for index alignment.

Audit semantics for User mutations:
- AdminAuditLog.tenant_id is NOT NULL but Users are tenant-agnostic.
- Per the existing project convention (documented in admin_audit_log.py
  and AuditContext.SYSTEM_ACTOR_TENANT), system-level actions with no
  tenant scope use the literal string "platform". User mutations are
  platform-level events -- one User identity spans tenants -- so they
  always log against tenant_id="platform". This keeps cross-tenant
  identity history queryable without a join.

Domain-agnostic: no imports from app/domain/, no vertical branching.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.admin_audit_log import (
    ACTION_CREATE,
    ACTION_DEACTIVATE,
    ACTION_UPDATE,
    RESOURCE_USER,
)
from app.models.agent import Agent
from app.models.user import User
from app.repositories.admin_audit_repository import (
    SYSTEM_ACTOR_TENANT,
    AdminAuditRepository,
    AuditContext,
    diff_updated_fields,
)

logger = logging.getLogger(__name__)


class UserRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ---------------------------------------------------------------
    # Class-level constants
    # ---------------------------------------------------------------

    # Whitelist -- identity columns are deliberately not updatable here.
    # `id`             : UUID PK, never mutate.
    # `synthetic`      : create-time-only flag (Option B onboarding stub
    #                    vs real user). Toggling it post-create would
    #                    rewrite audit semantics; treat as immutable.
    # `active`         : route through deactivate() so the audit row
    #                    captures the lifecycle event distinctly.
    # `created_at`     : DB-managed.
    # `updated_at`     : DB-managed via TimestampMixin.onupdate.
    _UPDATABLE_FIELDS = frozenset({"display_name", "email"})

    # ---------------------------------------------------------------
    # Create
    # ---------------------------------------------------------------

    def create(
        self,
        *,
        email: str,
        display_name: str,
        synthetic: bool = False,
        autocommit: bool = True,
        audit_ctx: AuditContext | None = None,
    ) -> User:
        """Insert a new User row.

        Email uniqueness is enforced at the DB via the LOWER(email)
        expression index (File 1.9). Caller is expected to translate
        IntegrityError into a 409 at the route layer.

        autocommit=False lets OnboardingService compose this into a
        larger transaction (Option B onboarding creates a User stub
        + a Tenant + a Domain + an Agent in a single txn).

        audit_ctx, when provided, writes an admin_audit_logs row in
        the same transaction. None is allowed for non-user-facing
        code paths (tests, migration backfill).
        """
        # Normalize defensively in case the schema validator was
        # bypassed by an internal call path. The schema lowercases on
        # input; this is belt-and-suspenders so the LOWER(email) index
        # is always queryable with the canonical form.
        email_norm = email.strip().lower()

        user = User(
            email=email_norm,
            display_name=display_name,
            synthetic=synthetic,
            active=True,
        )
        self.db.add(user)
        self.db.flush()  # assigns user.id, enables audit write before commit

        if audit_ctx is not None:
            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                tenant_id=SYSTEM_ACTOR_TENANT,
                action=ACTION_CREATE,
                resource_type=RESOURCE_USER,
                resource_pk=None,  # User PK is UUID, not int; use natural id
                resource_natural_id=email_norm,
                after={
                    "id": str(user.id),
                    "email": email_norm,
                    "display_name": display_name,
                    "synthetic": synthetic,
                    "active": True,
                },
                autocommit=False,
            )

        if autocommit:
            self.db.commit()
            self.db.refresh(user)

        logger.info(
            "User created id=%s email=%s synthetic=%s",
            user.id,
            email_norm,
            synthetic,
        )
        return user 
    # ---------------------------------------------------------------
    # Read
    # ---------------------------------------------------------------

    def get_by_pk(self, pk: uuid.UUID) -> User | None:
        return self.db.query(User).filter(User.id == pk).first()

    def get_by_email(self, email: str) -> User | None:
        """Case-insensitive lookup via LOWER(email).

        Aligns with the LOWER(email) expression index in File 1.9 so
        this query uses the index, not a sequential scan.
        """
        if not email:
            return None
        return (
            self.db.query(User)
            .filter(func.lower(User.email) == email.strip().lower())
            .first()
        )

    def email_exists(
        self,
        *,
        email: str,
        exclude_user_id: uuid.UUID | None = None,
    ) -> bool:
        """Uniqueness pre-flight helper for the service layer.

        Used on UserUpdate.email changes to surface clean 409s before
        issuing the UPDATE that would otherwise raise IntegrityError.
        exclude_user_id ignores the row being updated itself.
        """
        if not email:
            return False
        query = self.db.query(User.id).filter(
            func.lower(User.email) == email.strip().lower()
        )
        if exclude_user_id is not None:
            query = query.filter(User.id != exclude_user_id)
        return self.db.query(query.exists()).scalar() is True

    def list_for_scope(
        self,
        *,
        tenant_id: str | None = None,
        active_only: bool = False,
        include_synthetic: bool = True,
    ) -> list[User]:
        """List Users, optionally filtered by tenant scope.

        - tenant_id=None       -> all Users (platform-admin view; route
                                  handler / ScopePolicy gates this).
        - tenant_id="t1"       -> Users that hold any Agent row under
                                  tenant t1 (active or inactive Agent).
                                  Useful for "who has ever worked at
                                  this brokerage".

        active_only filters the User row's `active`. include_synthetic
        toggles whether Step 23 Option B onboarding stubs appear --
        real-user dashboards typically exclude them; PIPEDA access
        flows include them.
        """
        query = self.db.query(User)

        if tenant_id is not None:
            query = query.join(Agent, Agent.user_id == User.id).filter(
                Agent.tenant_id == tenant_id
            )

        if active_only:
            query = query.filter(User.active.is_(True))

        if not include_synthetic:
            query = query.filter(User.synthetic.is_(False))

        return query.order_by(User.created_at.asc()).distinct().all()

    def list_agents_for_user(
        self,
        user_id: uuid.UUID,
        *,
        active_only: bool = False,
    ) -> list[Agent]:
        """Cross-tenant: every Agent row this User identity holds.

        Service layer gates platform-admin authorization on the calling
        key. Tenant-scoped admins see only assignments under their own
        tenant via list_for_scope(tenant_id=...).
        """
        query = self.db.query(Agent).filter(Agent.user_id == user_id)
        if active_only:
            query = query.filter(Agent.active.is_(True))
        return query.order_by(Agent.tenant_id.asc(), Agent.id.asc()).all()
    # ---------------------------------------------------------------
    # Update
    # ---------------------------------------------------------------

    def update(
        self,
        user: User,
        *,
        audit_ctx: AuditContext | None = None,
        **fields,
    ) -> User:
        """Apply field updates to an existing User.

        Silently ignores any field not in _UPDATABLE_FIELDS. Writes an
        audit row containing only the fields that actually changed.

        Email uniqueness is NOT pre-checked here -- the caller
        (UserService) is expected to use email_exists() before calling
        update() if it wants a clean 409. If the caller skips the
        pre-flight, the DB unique index raises IntegrityError, which
        the route layer translates to 409.
        """
        # Normalize email to canonical form if it's being updated --
        # mirrors the create() path so LOWER(email) lookups always
        # match the stored value, and so email_exists() pre-flight
        # results agree with the eventual INSERT.
        if "email" in fields and isinstance(fields["email"], str):
            fields["email"] = fields["email"].strip().lower()

        # Snapshot before so the audit diff only reflects real changes.
        before_snapshot = {
            key: getattr(user, key) for key in self._UPDATABLE_FIELDS
        }

        applied: dict[str, object] = {}
        for key, value in fields.items():
            if key in self._UPDATABLE_FIELDS and value is not None:
                setattr(user, key, value)
                applied[key] = value

        after_snapshot = {
            key: getattr(user, key) for key in self._UPDATABLE_FIELDS
        }

        if audit_ctx is not None and applied:
            before_diff, after_diff = diff_updated_fields(
                before_snapshot, after_snapshot
            )
            if before_diff or after_diff:
                AdminAuditRepository(self.db).record(
                    ctx=audit_ctx,
                    tenant_id=SYSTEM_ACTOR_TENANT,
                    action=ACTION_UPDATE,
                    resource_type=RESOURCE_USER,
                    resource_pk=None,  # User PK is UUID
                    resource_natural_id=user.email,
                    before=before_diff,
                    after=after_diff,
                    autocommit=False,
                )

        self.db.commit()
        self.db.refresh(user)
        if applied:
            logger.info(
                "User updated id=%s fields=%s",
                user.id,
                sorted(applied.keys()),
            )
        return user

    # ---------------------------------------------------------------
    # Deactivate (soft delete)
    # ---------------------------------------------------------------

    def deactivate(
        self,
        *,
        user_id: uuid.UUID,
        reason: str | None = None,
        audit_ctx: AuditContext | None = None,
    ) -> User | None:
        """Soft-deactivate a User. Returns None if not found.

        Does NOT cascade to ScopeAssignments or ApiKeys -- that cascade
        lives in UserService.deactivate_user (Commit 2) so the
        hierarchy logic sits in one place. Same doctrine as
        AgentRepository.deactivate not cascading to LucielInstance.

        `reason` is the business justification recorded in the audit
        row's `note` field. UserDeactivate schema enforces 10-500 chars
        at the API boundary; the repo accepts None for system-initiated
        deactivations (cascades from tenant deactivation, retention
        purges, etc.).
        """
        user = self.get_by_pk(user_id)
        if user is None:
            return None

        was_active = bool(user.active)
        user.active = False

        if audit_ctx is not None and was_active:
            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                tenant_id=SYSTEM_ACTOR_TENANT,
                action=ACTION_DEACTIVATE,
                resource_type=RESOURCE_USER,
                resource_pk=None,  # User PK is UUID
                resource_natural_id=user.email,
                before={"active": True},
                after={"active": False},
                note=reason,
                autocommit=False,
            )

        self.db.commit()
        self.db.refresh(user)
        logger.info(
            "User deactivated id=%s email=%s",
            user.id,
            user.email,
        )
        return user