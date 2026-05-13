"""
LucielInstanceService — orchestration layer for scope-owned child Luciels.

Step 24.5 (File 7). Sits on top of:
  - LucielInstanceRepository (File 6)
  - AgentRepository          (File 5)
  - AdminService.validate_domain_active  (Step 24, re-patched in File 11)
  - TenantRepository / ConfigRepository (existing)

Responsibilities:
  1. Parent-scope-active validation before any create:
       - scope_level="tenant" -> tenant must exist and be active
       - scope_level="domain" -> tenant + domain must exist and be active
       - scope_level="agent"  -> tenant + domain + agent must exist and be active
     Prevents orphaned instances under deactivated scope owners.
  2. Atomic create / deactivate with audit rows (audit_ctx propagated
     into the repo layer from File 6.5c).
  3. Cascade hooks called by callers that deactivate a parent scope:
       - cascade_on_agent_deactivate
       - cascade_on_domain_deactivate
     Both delegate to the repo's bulk helpers which write one audit
     row per cascade event.
  4. Create-at-or-below authorization is NOT enforced here. That lives
     in app.policy.scope.ScopePolicy (File 9) and is applied at the
     route layer (File 10). This service trusts that authorization has
     already run.

Domain-agnostic: no imports from app/domain/, no vertical branching,
no hardcoded role names.
"""
from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.luciel_instance import (
    SCOPE_LEVEL_AGENT,
    SCOPE_LEVEL_DOMAIN,
    SCOPE_LEVEL_TENANT,
    LucielInstance,
)
from app.repositories.admin_audit_repository import AuditContext
from app.repositories.agent_repository import AgentRepository
from app.repositories.luciel_instance_repository import LucielInstanceRepository

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Error types — service-level exceptions the route layer (File 10)
# translates into HTTP responses.
# ---------------------------------------------------------------------

class LucielInstanceError(Exception):
    """Base class for service-level errors."""


class ParentScopeInactiveError(LucielInstanceError):
    """Raised when the owning tenant/domain/agent doesn't exist or isn't
    active. Route layer maps to 400."""


class DuplicateInstanceError(LucielInstanceError):
    """Raised when (scope_owner_*, instance_id) already exists.
    Route layer maps to 409."""


class InstanceNotFoundError(LucielInstanceError):
    """Raised when a target instance can't be resolved. Route layer
    maps to 404."""


class TierScopeViolationError(LucielInstanceError):
    """Step 30a.1: raised when a tenant's active subscription tier does
    NOT permit the requested scope level, or when the tenant has hit
    its ``instance_count_cap``. Route layer maps to 402 Payment Required
    so the caller distinguishes \"upgrade your tier\" from a 403
    (\"this key is not allowed\") or 400 (\"payload is malformed\").

    Two distinct sub-conditions share this error type because they both
    resolve through the same user action (upgrade tier); the ``reason``
    attribute and the human message disambiguate.
    """

    REASON_SCOPE_NOT_PERMITTED = "scope_not_permitted"
    REASON_CAP_EXCEEDED = "cap_exceeded"
    REASON_NO_ACTIVE_SUBSCRIPTION = "no_active_subscription"

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------

class LucielInstanceService:
    def __init__(
        self,
        db: Session,
        *,
        admin_service,  # app.services.admin_service.AdminService (Step 24)
    ) -> None:
        self.db = db
        self.repo = LucielInstanceRepository(db)
        self.agents = AgentRepository(db)
        # AdminService is injected (not imported directly) to avoid a
        # circular import: AdminService is what File 11 patches to call
        # back into this service for cascades.
        self.admin = admin_service

    # ---------------------------------------------------------------
    # Parent-scope-active validation
    # ---------------------------------------------------------------

    def validate_parent_scope_active(
        self,
        *,
        scope_level: str,
        tenant_id: str,
        domain_id: str | None,
        agent_id: str | None,
    ) -> None:
        """Verify the owning scope chain exists and is fully active.

        Raises ParentScopeInactiveError on any failure. Silent on success.

        Rules (mirroring LucielInstanceCreate's shape invariant):
          - tenant -> tenant active
          - domain -> tenant active + domain active
          - agent  -> tenant active + domain active + agent active
        """
        # Tenant — always required.
        tenant = self.admin.get_tenant_config(tenant_id)
        if tenant is None or not getattr(tenant, "active", False):
            raise ParentScopeInactiveError(
                f"Tenant {tenant_id!r} does not exist or is inactive."
            )

        if scope_level == SCOPE_LEVEL_TENANT:
            return

        # Domain — required for domain + agent scopes.
        if domain_id is None:
            # Defensive — schema validator already caught this, but
            # service layer should never rely solely on schema.
            raise ParentScopeInactiveError(
                f"scope_level={scope_level!r} requires domain_id."
            )
        if not self.admin.validate_domain_active(tenant_id, domain_id):
            raise ParentScopeInactiveError(
                f"Domain {domain_id!r} under tenant {tenant_id!r} "
                f"does not exist or is inactive."
            )

        if scope_level == SCOPE_LEVEL_DOMAIN:
            return

        # Agent — required for agent scope only.
        if agent_id is None:
            raise ParentScopeInactiveError(
                f"scope_level={scope_level!r} requires agent_id."
            )
        agent = self.agents.get_scoped(
            tenant_id=tenant_id,
            domain_id=domain_id,
            agent_id=agent_id,
        )
        if agent is None or not getattr(agent, "active", False):
            raise ParentScopeInactiveError(
                f"Agent {agent_id!r} under tenant {tenant_id!r} / "
                f"domain {domain_id!r} does not exist or is inactive."
            )

    # ---------------------------------------------------------------
    # Create
    # ---------------------------------------------------------------

    def create_instance(
        self,
        *,
        audit_ctx: AuditContext,
        instance_id: str,
        display_name: str,
        scope_level: str,
        scope_owner_tenant_id: str,
        scope_owner_domain_id: str | None = None,
        scope_owner_agent_id: str | None = None,
        description: str | None = None,
        system_prompt_additions: str | None = None,
        preferred_provider: str | None = None,
        allowed_tools: list[str] | None = None,
        created_by: str | None = None,
    ) -> LucielInstance:
        """Create a new LucielInstance atomically.

        Workflow:
          1. Parent-scope-active validation.
          2. Delegate to repo with autocommit=False.
          3. Commit on success; rollback on any exception.
          4. Audit row written by the repo in the same transaction.

        Raises:
          ParentScopeInactiveError  -> 400
          DuplicateInstanceError    -> 409
        """
        self.validate_parent_scope_active(
            scope_level=scope_level,
            tenant_id=scope_owner_tenant_id,
            domain_id=scope_owner_domain_id,
            agent_id=scope_owner_agent_id,
        )

        try:
            instance = self.repo.create(
                instance_id=instance_id,
                display_name=display_name,
                scope_level=scope_level,
                scope_owner_tenant_id=scope_owner_tenant_id,
                scope_owner_domain_id=scope_owner_domain_id,
                scope_owner_agent_id=scope_owner_agent_id,
                description=description,
                system_prompt_additions=system_prompt_additions,
                preferred_provider=preferred_provider,
                allowed_tools=allowed_tools,
                created_by=created_by,
                autocommit=False,
                audit_ctx=audit_ctx,
            )
            self.db.commit()
            self.db.refresh(instance)
        except IntegrityError as exc:
            self.db.rollback()
            # Unique constraint on (scope_owner_*, instance_id).
            # Any other IntegrityError (CHECK failures) also surface
            # as DuplicateInstanceError-adjacent; we keep the message
            # specific so the route layer can 409 cleanly.
            logger.info(
                "LucielInstance create rejected (integrity): tenant=%s "
                "scope=%s instance_id=%s",
                scope_owner_tenant_id,
                scope_level,
                instance_id,
            )
            raise DuplicateInstanceError(
                f"A LucielInstance with instance_id={instance_id!r} "
                f"already exists under this scope owner."
            ) from exc
        except Exception:
            self.db.rollback()
            logger.exception(
                "LucielInstance create failed tenant=%s scope=%s instance_id=%s",
                scope_owner_tenant_id,
                scope_level,
                instance_id,
            )
            raise

        return instance

    # ---------------------------------------------------------------
    # Deactivate (single instance)
    # ---------------------------------------------------------------

    def deactivate_instance(
        self,
        *,
        audit_ctx: AuditContext,
        pk: int,
        updated_by: str | None = None,
    ) -> LucielInstance:
        """Soft-deactivate one instance by PK.

        Assumes scope-based authorization has already run at the route
        layer (File 10 via ScopePolicy from File 9). This method only
        enforces existence.

        Raises InstanceNotFoundError -> 404.
        """
        instance = self.repo.deactivate_by_pk(
            pk,
            updated_by=updated_by,
            audit_ctx=audit_ctx,
        )
        if instance is None:
            raise InstanceNotFoundError(
                f"LucielInstance pk={pk} not found."
            )
        return instance

    # ---------------------------------------------------------------
    # Cascade hooks — called when a parent scope is deactivated
    # ---------------------------------------------------------------

    def cascade_on_agent_deactivate(
        self,
        *,
        audit_ctx: AuditContext,
        tenant_id: str,
        domain_id: str,
        agent_id: str,
        updated_by: str | None = None,
    ) -> int:
        """Deactivate every agent-scoped instance owned by the agent.

        Called by AgentRepository.deactivate (File 5) through the
        File 11 admin_service patch. Returns the number of instances
        deactivated (zero if the agent owned none).

        Writes ONE audit row for the cascade, not one per row
        (implemented at the repo in File 6.5c).
        """
        count = self.repo.deactivate_all_for_agent(
            tenant_id=tenant_id,
            domain_id=domain_id,
            agent_id=agent_id,
            updated_by=updated_by,
            audit_ctx=audit_ctx,
        )
        logger.info(
            "Cascade on agent deactivate: count=%d tenant=%s "
            "domain=%s agent=%s",
            count,
            tenant_id,
            domain_id,
            agent_id,
        )
        return count

    def cascade_on_domain_deactivate(
        self,
        *,
        audit_ctx: AuditContext,
        tenant_id: str,
        domain_id: str,
        updated_by: str | None = None,
    ) -> int:
        """Deactivate every domain-scoped AND agent-scoped instance
        under the domain.

        Called by AdminService.deactivate_domain (Step 24, re-patched
        in File 11) so the cascade applied to the domain's agents and
        to all Luciels (domain + agent scope) happens atomically.

        Returns the number of instances deactivated.
        Writes ONE audit row for the cascade.
        """
        count = self.repo.deactivate_all_for_domain(
            tenant_id=tenant_id,
            domain_id=domain_id,
            updated_by=updated_by,
            audit_ctx=audit_ctx,
        )
        logger.info(
            "Cascade on domain deactivate: count=%d tenant=%s domain=%s",
            count,
            tenant_id,
            domain_id,
        )
        return count

    # ---------------------------------------------------------------
    # Convenience reads (no authorization — route layer enforces it)
    # ---------------------------------------------------------------

    def get_by_pk(self, pk: int) -> LucielInstance | None:
        return self.repo.get_by_pk(pk)

    def list_for_scope(
        self,
        *,
        tenant_id: str,
        domain_id: str | None = None,
        agent_id: str | None = None,
        include_inherited: bool = False,
        active_only: bool = False,
    ) -> list[LucielInstance]:
        return self.repo.list_for_scope(
            tenant_id=tenant_id,
            domain_id=domain_id,
            agent_id=agent_id,
            include_inherited=include_inherited,
            active_only=active_only,
        )