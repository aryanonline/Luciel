"""
Agent repository — data-access layer for the Agent (person/role) model.

Step 24.5. Wraps app.models.agent.Agent.

Scope of responsibility:
- Pure CRUD. No ScopePolicy calls, no business-rule checks, no HTTP
  exceptions. Callers (services / route handlers) handle those.
- No cross-model joins. Validation that a parent domain exists/active
  happens in AdminService.validate_domain_active (reused from Step 24).

Domain-agnostic: no imports from app/domain/, no vertical branching.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.admin_audit_log import (
    ACTION_CREATE,
    ACTION_DEACTIVATE,
    ACTION_UPDATE,
    RESOURCE_AGENT,
)
from app.models.agent import Agent
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
    diff_updated_fields,
)
import uuid

logger = logging.getLogger(__name__)


class AgentRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ---------------------------------------------------------------
    # Create
    # ---------------------------------------------------------------
    def create(
        self,
        *,
        tenant_id: str,
        domain_id: str,
        agent_id: str,
        display_name: str,
        description: str | None = None,
        contact_email: str | None = None,
        created_by: str | None = None,
        autocommit: bool = True,
        audit_ctx: AuditContext | None = None,
    ) -> Agent:
        """Insert a new Agent row.

        Unique constraint (tenant_id, domain_id, agent_id) is enforced
        at the DB. Caller is expected to translate IntegrityError into
        a 409 at the route layer.

        autocommit=False lets OnboardingService / LucielInstanceService
        compose this into a larger transaction.

        audit_ctx, when provided, writes an admin_audit_logs row in the
        same transaction. None is allowed for non-user-facing code
        paths (tests, migration backfill).
        """
        agent = Agent(
            tenant_id=tenant_id,
            domain_id=domain_id,
            agent_id=agent_id,
            display_name=display_name,
            description=description,
            contact_email=contact_email,
            active=True,
            created_by=created_by,
        )
        self.db.add(agent)
        self.db.flush()  # assigns agent.id, enables audit write before commit

        if audit_ctx is not None:
            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                tenant_id=tenant_id,
                action=ACTION_CREATE,
                resource_type=RESOURCE_AGENT,
                resource_pk=agent.id,
                resource_natural_id=agent_id,
                domain_id=domain_id,
                agent_id=agent_id,
                after={
                    "display_name": display_name,
                    "description": description,
                    "contact_email": contact_email,
                    "active": True,
                },
                autocommit=False,
            )

        if autocommit:
            self.db.commit()
            self.db.refresh(agent)
        logger.info(
            "Agent created tenant=%s domain=%s agent_id=%s",
            tenant_id,
            domain_id,
            agent_id,
        )
        return agent
    # ---------------------------------------------------------------
    # Read
    # ---------------------------------------------------------------

    def get_by_pk(self, pk: int) -> Agent | None:
        return self.db.query(Agent).filter(Agent.id == pk).first()

    def get(
        self,
        *,
        tenant_id: str,
        agent_id: str,
    ) -> Agent | None:
        """Fetch by natural identity (tenant_id, agent_id).

        agent_id is unique within (tenant_id, domain_id) per the model
        constraint, but across a single tenant we expect it to be
        unique in practice (one "sarah-listings" per tenant). If a
        tenant legitimately has the same agent_id in two different
        domains — rare but allowed — use get_scoped() instead.
        """
        return (
            self.db.query(Agent)
            .filter(
                Agent.tenant_id == tenant_id,
                Agent.agent_id == agent_id,
            )
            .order_by(Agent.id.asc())
            .first()
        )

    def get_scoped(
        self,
        *,
        tenant_id: str,
        domain_id: str,
        agent_id: str,
    ) -> Agent | None:
        """Fetch by full natural key (tenant_id, domain_id, agent_id)."""
        return (
            self.db.query(Agent)
            .filter(
                Agent.tenant_id == tenant_id,
                Agent.domain_id == domain_id,
                Agent.agent_id == agent_id,
            )
            .first()
        )

    def list_for_scope(
        self,
        *,
        tenant_id: str,
        domain_id: str | None = None,
        active_only: bool = False,
    ) -> list[Agent]:
        """List agents within a scope.

        - tenant_id only       -> all agents for that tenant
        - tenant_id + domain_id -> agents in that domain
        Caller (route handler + ScopePolicy) is responsible for
        ensuring the caller is allowed to see this scope.
        """
        query = self.db.query(Agent).filter(Agent.tenant_id == tenant_id)
        if domain_id is not None:
            query = query.filter(Agent.domain_id == domain_id)
        if active_only:
            query = query.filter(Agent.active.is_(True))
        return query.order_by(Agent.id.asc()).all()

    # ---------------------------------------------------------------
    # Update
    # ---------------------------------------------------------------

    # Whitelist — identity columns are deliberately not updatable.
    # Promotion / demotion across domains = deactivate + recreate,
    # per the Step 24.5 decision on preserving audit trails.
    _UPDATABLE_FIELDS = frozenset(
        {
            "display_name",
            "description",
            "contact_email",
            "active",
            "updated_by",
        }
    )
    def update(
        self,
        agent: Agent,
        *,
        audit_ctx: AuditContext | None = None,
        **fields,
    ) -> Agent:
        """Apply field updates to an existing Agent.

        Silently ignores any field not in _UPDATABLE_FIELDS. Writes an
        audit row containing only the fields that actually changed.
        """
        # Snapshot before so the audit diff only reflects real changes.
        before_snapshot = {
            key: getattr(agent, key) for key in self._UPDATABLE_FIELDS
        }

        applied: dict[str, object] = {}
        for key, value in fields.items():
            if key in self._UPDATABLE_FIELDS and value is not None:
                setattr(agent, key, value)
                applied[key] = value

        after_snapshot = {
            key: getattr(agent, key) for key in self._UPDATABLE_FIELDS
        }

        if audit_ctx is not None and applied:
            before_diff, after_diff = diff_updated_fields(
                before_snapshot, after_snapshot
            )
            if before_diff or after_diff:
                AdminAuditRepository(self.db).record(
                    ctx=audit_ctx,
                    tenant_id=agent.tenant_id,
                    action=ACTION_UPDATE,
                    resource_type=RESOURCE_AGENT,
                    resource_pk=agent.id,
                    resource_natural_id=agent.agent_id,
                    domain_id=agent.domain_id,
                    agent_id=agent.agent_id,
                    before=before_diff,
                    after=after_diff,
                    autocommit=False,
                )

        self.db.commit()
        self.db.refresh(agent)
        if applied:
            logger.info(
                "Agent updated tenant=%s agent_id=%s fields=%s",
                agent.tenant_id,
                agent.agent_id,
                sorted(applied.keys()),
            )
        return agent

    # ---------------------------------------------------------------
    # Deactivate (soft delete)
    # ---------------------------------------------------------------

    def deactivate(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        updated_by: str | None = None,
        audit_ctx: AuditContext | None = None,
    ) -> Agent | None:
        """Soft-deactivate an agent. Returns None if not found.

        Does NOT cascade to LucielInstance rows owned by this agent —
        that cascade lives in LucielInstanceService (File 7) so the
        hierarchy logic sits in one place.
        """
        agent = self.get(tenant_id=tenant_id, agent_id=agent_id)
        if agent is None:
            return None

        was_active = bool(agent.active)
        agent.active = False
        if updated_by is not None:
            agent.updated_by = updated_by

        if audit_ctx is not None and was_active:
            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                tenant_id=tenant_id,
                action=ACTION_DEACTIVATE,
                resource_type=RESOURCE_AGENT,
                resource_pk=agent.id,
                resource_natural_id=agent_id,
                domain_id=agent.domain_id,
                agent_id=agent_id,
                before={"active": True},
                after={"active": False},
                autocommit=False,
            )

        self.db.commit()
        self.db.refresh(agent)
        logger.info(
            "Agent deactivated tenant=%s agent_id=%s",
            tenant_id,
            agent_id,
        )
        return agent
    # ---------------------------------------------------------------
    # Step 24.5b -- User identity layer lookups
    # ---------------------------------------------------------------

    def get_by_user_and_tenant(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: str,
        active_only: bool = False,
    ) -> Agent | None:
        """Find the Agent row for this User identity within this tenant.

        Step 24.5b. Used by the promotion path: when ScopeAssignment is
        ended for (user, tenant), the service walks here to find the
        owning Agent so it can rotate ApiKeys bound to that Agent
        (mandatory key rotation per Q6 resolution).

        In steady state we expect exactly zero or one match per
        (user_id, tenant_id) -- a User holds at most one active Agent
        per tenant. Multiple matches across history are possible after
        deactivate-and-recreate cycles, in which case active_only=True
        returns the currently-active row.

        Hits ix_agents_user_id (composite-friendly via tenant_id index).
        """
        query = self.db.query(Agent).filter(
            Agent.user_id == user_id,
            Agent.tenant_id == tenant_id,
        )
        if active_only:
            query = query.filter(Agent.active.is_(True))
        # If multiple historical rows exist (deactivate-and-recreate
        # cycle), return the most recent. Active-only callers will
        # only see at most one in steady state.
        return query.order_by(Agent.id.desc()).first()

    def list_for_user(
        self,
        user_id: uuid.UUID,
        *,
        active_only: bool = False,
    ) -> list[Agent]:
        """Cross-tenant: every Agent row this User identity holds.

        Step 24.5b. Mirrors UserRepository.list_agents_for_user (which
        delegates here). Both paths exist because some call sites have
        a User in hand (UserRepository entry point) and others have
        the user_id from a ScopeAssignment row (this entry point).

        Service layer gates platform-admin authorization on the calling
        key. Tenant-scoped admins use list_for_scope(tenant_id=...) for
        tenant-bounded views; this cross-tenant lookup is platform-only.

        Sorted by tenant first then id so the result groups Agents by
        brokerage in display order.
        """
        query = self.db.query(Agent).filter(Agent.user_id == user_id)
        if active_only:
            query = query.filter(Agent.active.is_(True))
        return query.order_by(Agent.tenant_id.asc(), Agent.id.asc()).all()