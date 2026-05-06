"""
Admin service.

Handles business logic for tenant, domain, and agent config management.
Keeps route handlers thin by centralizing validation and persistence.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.agent_config import AgentConfig
from app.models.domain_config import DomainConfig
from app.models.tenant import TenantConfig

logger = logging.getLogger(__name__)


class AdminService:

    def __init__(self, db: Session) -> None:
        self.db = db

    # --- Tenant Config ---

    def create_tenant_config(self, **kwargs) -> TenantConfig:
        config = TenantConfig(**kwargs)
        self.db.add(config)
        self.db.commit()
        self.db.refresh(config)
        logger.info("Created tenant config: %s", config.tenant_id)
        return config

    def get_tenant_config(self, tenant_id: str) -> TenantConfig | None:
        stmt = select(TenantConfig).where(TenantConfig.tenant_id == tenant_id)
        return self.db.scalars(stmt).first()

    def update_tenant_config(self, tenant_id: str, **kwargs) -> TenantConfig | None:
        config = self.get_tenant_config(tenant_id)
        if not config:
            return None
        for key, value in kwargs.items():
            if value is not None and hasattr(config, key):
                setattr(config, key, value)
        self.db.commit()
        self.db.refresh(config)
        logger.info("Updated tenant config: %s", tenant_id)
        return config

    def list_tenant_configs(self) -> list[TenantConfig]:
        stmt = select(TenantConfig).order_by(TenantConfig.created_at.desc())
        return list(self.db.scalars(stmt).all())

    # --- Domain Config ---

    def create_domain_config(self, **kwargs) -> DomainConfig:
        config = DomainConfig(**kwargs)
        self.db.add(config)
        self.db.commit()
        self.db.refresh(config)
        logger.info(
            "Created domain config: %s/%s", config.tenant_id, config.domain_id
        )
        return config

    def get_domain_config(self, tenant_id: str, domain_id: str) -> DomainConfig | None:
        stmt = select(DomainConfig).where(
            DomainConfig.tenant_id == tenant_id,
            DomainConfig.domain_id == domain_id,
        )
        return self.db.scalars(stmt).first()

    def update_domain_config(
        self, tenant_id: str, domain_id: str, **kwargs
    ) -> DomainConfig | None:
        config = self.get_domain_config(tenant_id, domain_id)
        if not config:
            return None
        for key, value in kwargs.items():
            if value is not None and hasattr(config, key):
                setattr(config, key, value)
        self.db.commit()
        self.db.refresh(config)
        logger.info("Updated domain config: %s/%s", tenant_id, domain_id)
        return config

    def list_domain_configs(self, tenant_id: str | None = None) -> list[DomainConfig]:
        stmt = select(DomainConfig).order_by(DomainConfig.created_at.desc())
        if tenant_id:
            stmt = stmt.where(DomainConfig.tenant_id == tenant_id)
        return list(self.db.scalars(stmt).all())

    # --- Agent Config ---

    def create_agent_config(self, **kwargs) -> AgentConfig:
        config = AgentConfig(**kwargs)
        self.db.add(config)
        self.db.commit()
        self.db.refresh(config)
        logger.info(
            "Created agent config: %s/%s", config.tenant_id, config.agent_id
        )
        return config

    def get_agent_config(self, tenant_id: str, agent_id: str) -> AgentConfig | None:
        stmt = select(AgentConfig).where(
            AgentConfig.tenant_id == tenant_id,
            AgentConfig.agent_id == agent_id,
        )
        return self.db.scalars(stmt).first()

    def update_agent_config(
        self, tenant_id: str, agent_id: str, **kwargs
    ) -> AgentConfig | None:
        config = self.get_agent_config(tenant_id, agent_id)
        if not config:
            return None
        for key, value in kwargs.items():
            if value is not None and hasattr(config, key):
                setattr(config, key, value)
        self.db.commit()
        self.db.refresh(config)
        logger.info("Updated agent config: %s/%s", tenant_id, agent_id)
        return config

    def list_agent_configs(self, tenant_id: str | None = None) -> list[AgentConfig]:
        stmt = select(AgentConfig).order_by(AgentConfig.created_at.desc())
        if tenant_id:
            stmt = stmt.where(AgentConfig.tenant_id == tenant_id)
        return list(self.db.scalars(stmt).all())
    
    def validate_domain_active(self, tenant_id: str, domain_id: str) -> bool:
        """
        Hierarchy validation used before creating an agent.
        Returns True only if a DomainConfig exists for (tenant_id, domain_id)
        AND it is active.
        """
        domain = self.get_domain_config(tenant_id, domain_id)
        return bool(domain and getattr(domain, "active", False))

    def list_agent_configs_by_domain(
        self, tenant_id: str, domain_id: str,
    ) -> list:
        """List agents filtered to a specific domain within a tenant."""
        from app.models.agent_config import AgentConfig  # local import to avoid cycles
        return (
            self.db.query(AgentConfig)
            .filter(
                AgentConfig.tenant_id == tenant_id,
                AgentConfig.domain_id == domain_id,
            )
            .order_by(AgentConfig.id.asc())
            .all()
        )

    def deactivate_domain(
        self,
        tenant_id: str,
        domain_id: str,
        *,
        audit_ctx=None,                    # Step 24.5: AuditContext | None
        luciel_instance_service=None,      # Step 24.5: LucielInstanceService | None
        updated_by: str | None = None,
    ) -> bool:
        """Soft-deactivate a domain and cascade:

        1. Every AgentConfig row in that domain (legacy — Step 24).
        2. Every new-table Agent row in that domain (Step 24.5).
        3. Every domain- and agent-scoped LucielInstance under the
            domain (Step 24.5, via luciel_instance_service).

        All three cascade steps commit in a single transaction with the
        domain deactivation itself. If any step fails, nothing changes.

        audit_ctx / luciel_instance_service are optional so legacy callers
        (internal scripts, tests) still work without the cascade.
        """
        from app.models.agent import Agent          # Step 24.5 new table
        from app.repositories.admin_audit_repository import AdminAuditRepository

        # Import constants late to avoid circular imports.
        from app.models.admin_audit_log import (
            ACTION_CASCADE_DEACTIVATE,
            ACTION_DEACTIVATE,
            RESOURCE_DOMAIN,
        )

        domain = self.get_domain_config(tenant_id, domain_id)
        if not domain:
            return False

        was_active = bool(domain.active)

        try:
            # --- 1. Deactivate the domain itself -----------------------
            domain.active = False
            if updated_by is not None:
                domain.updated_by = updated_by

            if audit_ctx is not None and was_active:
                AdminAuditRepository(self.db).record(
                    ctx=audit_ctx,
                    tenant_id=tenant_id,
                    action=ACTION_DEACTIVATE,
                    resource_type=RESOURCE_DOMAIN,
                    resource_pk=domain.id,
                    resource_natural_id=domain_id,
                    domain_id=domain_id,
                    before={"active": True},
                    after={"active": False},
                    autocommit=False,
                )

            # --- 3. Step 24.5: new-table Agent cascade -----------------
            affected_agents = (
                self.db.query(Agent.id, Agent.agent_id)
                .filter(
                    Agent.tenant_id == tenant_id,
                    Agent.domain_id == domain_id,
                    Agent.active.is_(True),
                )
                .all()
            )
            affected_agent_pks = [pk for pk, _ in affected_agents]
            affected_agent_ids = [nid for _, nid in affected_agents]

            self.db.query(Agent).filter(
                Agent.tenant_id == tenant_id,
                Agent.domain_id == domain_id,
                Agent.active.is_(True),
            ).update(
                {"active": False, "updated_by": updated_by},
                synchronize_session=False,
            )

            if audit_ctx is not None and affected_agent_pks:
                AdminAuditRepository(self.db).record(
                    ctx=audit_ctx,
                    tenant_id=tenant_id,
                    action=ACTION_CASCADE_DEACTIVATE,
                    resource_type="agent",
                    resource_pk=None,
                    resource_natural_id=None,
                    domain_id=domain_id,
                    after={
                        "count": len(affected_agent_pks),
                        "affected_pks": affected_agent_pks,
                        "affected_agent_ids": affected_agent_ids,
                        "trigger": "domain_deactivate",
                    },
                    note=f"Cascade from domain {domain_id} deactivation",
                    autocommit=False,
                )

            # --- 3.5 Memory cascade -------------------------------------
            # Soft-deactivate every memory_items row scoped to agents in
            # this domain (via subquery through agents.domain_id).
            # Tenant-only memories (agent_id=NULL) are NOT touched here --
            # they survive a domain-only deactivation.
            if audit_ctx is not None:
                self.bulk_soft_deactivate_memory_items_for_domain(
                    tenant_id=tenant_id,
                    domain_id=domain_id,
                    audit_ctx=audit_ctx,
                    updated_by=updated_by,
                    autocommit=False,
                )


            # --- 4. Step 24.5: LucielInstance cascade ------------------
            if luciel_instance_service is not None and audit_ctx is not None:
                luciel_instance_service.cascade_on_domain_deactivate(
                    audit_ctx=audit_ctx,
                    tenant_id=tenant_id,
                    domain_id=domain_id,
                    updated_by=updated_by,
                )

            self.db.commit()
            self.db.refresh(domain)
        except Exception:
            self.db.rollback()
            raise

        return True

    def deactivate_agent(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        audit_ctx=None,                    # Step 24.5
        luciel_instance_service=None,      # Step 24.5
        updated_by: str | None = None,
    ) -> bool:
        """Soft-deactivate a legacy AgentConfig row.

        Step 24.5: if luciel_instance_service is provided, also cascade-
        deactivate every agent-scoped LucielInstance owned by this agent.
        (The new-table Agent row, if it exists, is handled by a separate
        route — POST /admin/agents/{tenant}/{agent}/deactivate in File 10.
        This legacy path only touches agent_configs and optionally the
        agent-scoped Luciels that reference the same agent_id.)

        audit_ctx / luciel_instance_service are optional for legacy callers.
        """
        from app.models.admin_audit_log import (
            ACTION_DEACTIVATE,
            RESOURCE_AGENT,
        )
        from app.repositories.admin_audit_repository import AdminAuditRepository

        agent = self.get_agent_config(tenant_id, agent_id)
        if not agent:
            return False

        was_active = bool(agent.active)

        try:
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
                    domain_id=getattr(agent, "domain_id", None),
                    agent_id=agent_id,
                    before={"active": True},
                    after={"active": False},
                    autocommit=False,
                )

            # Memory cascade: soft-deactivate agent-scoped memory_items.
            # Same audit-ctx-required contract as the leaf method.
            # autocommit=False -- this method commits the whole transaction.
            if audit_ctx is not None:
                self.bulk_soft_deactivate_memory_items_for_agent(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    audit_ctx=audit_ctx,
                    updated_by=updated_by,
                    autocommit=False,
                )

            # Step 24.5 LucielInstance cascade (optional).
            if (
                luciel_instance_service is not None
                and audit_ctx is not None
                and getattr(agent, "domain_id", None) is not None
            ):
                luciel_instance_service.cascade_on_agent_deactivate(
                    audit_ctx=audit_ctx,
                    tenant_id=tenant_id,
                    domain_id=agent.domain_id,
                    agent_id=agent_id,
                    updated_by=updated_by,
                )

            self.db.commit()
            self.db.refresh(agent)
        except Exception:
            self.db.rollback()
            raise

        return True


    def bulk_soft_deactivate_memory_items_for_tenant(
        self,
        tenant_id: str,
        *,
        audit_ctx,
        updated_by: str | None = None,
        autocommit: bool = True,
    ) -> int:
        """Soft-deactivate every active memory_items row for a tenant.

        Used by deactivate_tenant_with_cascade and (indirectly) by the
        Pattern S walker. Mirrors the platform's general soft-delete
        model (recap section 3): memory_items.active flips to False;
        rows persist with active=False until a separate retention job
        hard-purges them.

        PIPEDA Principle 5 (limit retention) is satisfied because the
        application layer filters active=False rows out of every read
        path. A future scheduled job hard-purges inactive rows after
        the configured retention window.

        Returns count of rows deactivated. Always emits one audit row
        with action=ACTION_CASCADE_DEACTIVATE -- even when count == 0 --
        so the audit trail records that this scope was visited on
        every (idempotent) re-run. The after_json carries a per-(agent,
        instance) breakdown for granular forensic queries.

        audit_ctx is REQUIRED.
        """
        from sqlalchemy import func
        from app.models.memory import MemoryItem
        from app.repositories.admin_audit_repository import AdminAuditRepository
        from app.models.admin_audit_log import (
            ACTION_CASCADE_DEACTIVATE,
            RESOURCE_MEMORY,
        )

        if audit_ctx is None:
            raise ValueError(
                "bulk_soft_deactivate_memory_items_for_tenant requires audit_ctx"
            )

        try:
            # Pre-deactivation breakdown for forensic granularity in audit.
            breakdown_rows = (
                self.db.query(
                    MemoryItem.agent_id,
                    MemoryItem.luciel_instance_id,
                    func.count().label("row_count"),
                )
                .filter(
                    MemoryItem.tenant_id == tenant_id,
                    MemoryItem.active.is_(True),
                )
                .group_by(MemoryItem.agent_id, MemoryItem.luciel_instance_id)
                .all()
            )
            breakdown = [
                {
                    "agent_id": agent_id,
                    "luciel_instance_id": luciel_instance_id,
                    "count": row_count,
                }
                for (agent_id, luciel_instance_id, row_count) in breakdown_rows
            ]

            # Bulk single-pass deactivation.
            count = (
                self.db.query(MemoryItem)
                .filter(
                    MemoryItem.tenant_id == tenant_id,
                    MemoryItem.active.is_(True),
                )
                .update(
                    {"active": False},
                    synchronize_session=False,
                )
            )

            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                tenant_id=tenant_id,
                action=ACTION_CASCADE_DEACTIVATE,
                resource_type=RESOURCE_MEMORY,
                resource_pk=None,
                resource_natural_id=None,
                after={
                    "count": count,
                    "scope": "tenant",
                    "tenant_id": tenant_id,
                    "breakdown": breakdown,
                    "trigger": "tenant_deactivate_cascade",
                    "updated_by": updated_by,
                },
                note=(
                    f"Cascade memory_items deactivation from tenant "
                    f"{tenant_id} deactivation (PIPEDA P5)"
                ),
                autocommit=False,
            )

            if autocommit:
                self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        return count


    def bulk_soft_deactivate_memory_items_for_agent(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        audit_ctx,
        updated_by: str | None = None,
        autocommit: bool = True,
    ) -> int:
        """Soft-deactivate every active memory_items row for a single agent.

        Called from deactivate_agent (cascade) when an agent is
        deactivated standalone (not as part of a tenant or domain
        cascade). Memory rows scoped to this agent under this tenant
        flip to active=False.

        Returns count deactivated. Always emits one
        ACTION_CASCADE_DEACTIVATE audit row even when count == 0.
        Breakdown by luciel_instance_id is captured in after_json
        for forensic granularity.

        audit_ctx is REQUIRED.
        """
        from sqlalchemy import func
        from app.models.memory import MemoryItem
        from app.repositories.admin_audit_repository import AdminAuditRepository
        from app.models.admin_audit_log import (
            ACTION_CASCADE_DEACTIVATE,
            RESOURCE_MEMORY,
        )

        if audit_ctx is None:
            raise ValueError(
                "bulk_soft_deactivate_memory_items_for_agent requires audit_ctx"
            )

        try:
            breakdown_rows = (
                self.db.query(
                    MemoryItem.luciel_instance_id,
                    func.count().label("row_count"),
                )
                .filter(
                    MemoryItem.tenant_id == tenant_id,
                    MemoryItem.agent_id == agent_id,
                    MemoryItem.active.is_(True),
                )
                .group_by(MemoryItem.luciel_instance_id)
                .all()
            )
            breakdown = [
                {
                    "luciel_instance_id": luciel_instance_id,
                    "count": row_count,
                }
                for (luciel_instance_id, row_count) in breakdown_rows
            ]

            count = (
                self.db.query(MemoryItem)
                .filter(
                    MemoryItem.tenant_id == tenant_id,
                    MemoryItem.agent_id == agent_id,
                    MemoryItem.active.is_(True),
                )
                .update(
                    {"active": False},
                    synchronize_session=False,
                )
            )

            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                tenant_id=tenant_id,
                action=ACTION_CASCADE_DEACTIVATE,
                resource_type=RESOURCE_MEMORY,
                resource_pk=None,
                resource_natural_id=None,
                agent_id=agent_id,
                after={
                    "count": count,
                    "scope": "agent",
                    "tenant_id": tenant_id,
                    "agent_id": agent_id,
                    "breakdown": breakdown,
                    "trigger": "agent_deactivate_cascade",
                    "updated_by": updated_by,
                },
                note=(
                    f"Cascade memory_items deactivation from agent "
                    f"{tenant_id}/{agent_id} deactivation (PIPEDA P5)"
                ),
                autocommit=False,
            )

            if autocommit:
                self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        return count


    def bulk_soft_deactivate_memory_items_for_luciel_instance(
        self,
        tenant_id: str,
        luciel_instance_id: int,
        *,
        audit_ctx,
        updated_by: str | None = None,
        autocommit: bool = True,
    ) -> int:
        """Soft-deactivate every active memory_items row for a luciel_instance.

        Called from LucielInstanceService cascade methods when a
        single luciel_instance is deactivated. Memory rows scoped to
        this instance under this tenant flip to active=False.

        Returns count deactivated. Always emits one
        ACTION_CASCADE_DEACTIVATE audit row even when count == 0.
        Breakdown by agent_id is captured in after_json.

        audit_ctx is REQUIRED.
        """
        from sqlalchemy import func
        from app.models.memory import MemoryItem
        from app.repositories.admin_audit_repository import AdminAuditRepository
        from app.models.admin_audit_log import (
            ACTION_CASCADE_DEACTIVATE,
            RESOURCE_MEMORY,
        )

        if audit_ctx is None:
            raise ValueError(
                "bulk_soft_deactivate_memory_items_for_luciel_instance "
                "requires audit_ctx"
            )

        try:
            breakdown_rows = (
                self.db.query(
                    MemoryItem.agent_id,
                    func.count().label("row_count"),
                )
                .filter(
                    MemoryItem.tenant_id == tenant_id,
                    MemoryItem.luciel_instance_id == luciel_instance_id,
                    MemoryItem.active.is_(True),
                )
                .group_by(MemoryItem.agent_id)
                .all()
            )
            breakdown = [
                {
                    "agent_id": agent_id,
                    "count": row_count,
                }
                for (agent_id, row_count) in breakdown_rows
            ]

            count = (
                self.db.query(MemoryItem)
                .filter(
                    MemoryItem.tenant_id == tenant_id,
                    MemoryItem.luciel_instance_id == luciel_instance_id,
                    MemoryItem.active.is_(True),
                )
                .update(
                    {"active": False},
                    synchronize_session=False,
                )
            )

            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                tenant_id=tenant_id,
                action=ACTION_CASCADE_DEACTIVATE,
                resource_type=RESOURCE_MEMORY,
                resource_pk=None,
                resource_natural_id=None,
                luciel_instance_id=luciel_instance_id,
                after={
                    "count": count,
                    "scope": "luciel_instance",
                    "tenant_id": tenant_id,
                    "luciel_instance_id": luciel_instance_id,
                    "breakdown": breakdown,
                    "trigger": "luciel_instance_deactivate_cascade",
                    "updated_by": updated_by,
                },
                note=(
                    f"Cascade memory_items deactivation from luciel_instance "
                    f"{luciel_instance_id} (tenant {tenant_id}) deactivation "
                    f"(PIPEDA P5)"
                ),
                autocommit=False,
            )

            if autocommit:
                self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        # Step 28 C10 (P3-Q): return count, not bare return. Pre-fix
        # this method returned None despite the -> int annotation. The
        # current call sites all drop the return value, so this did not
        # cause a runtime failure, but it violates the type contract
        # and breaks any future caller that reads the return value
        # (e.g. structured cascade summary at the route level).
        return count


    def bulk_soft_deactivate_memory_items_for_domain(
        self,
        tenant_id: str,
        domain_id: str,
        *,
        audit_ctx,
        updated_by: str | None = None,
        autocommit: bool = True,
    ) -> int:
        """Soft-deactivate every active memory_items row for a domain.

        memory_items has no direct domain_id column -- domain
        attribution is via the agent the memory was scoped to:
            memory_items.agent_id -> agents.agent_id (within tenant)
            agents.domain_id == :domain_id

        Tenant-scoped memories with agent_id=NULL are NOT touched
        here -- they have no domain attribution. They survive a
        domain-only deactivation and are only cleaned up by tenant
        deactivation.

        Called from deactivate_domain (cascade). Filters agents by
        domain_id only (not by Agent.active) so a re-run after
        agents have already been deactivated still finds the right
        memory rows.

        Returns count deactivated. Always emits one
        ACTION_CASCADE_DEACTIVATE audit row even when count == 0.
        Breakdown by agent_id is captured in after_json.

        audit_ctx is REQUIRED.
        """
        from sqlalchemy import func
        from app.models.memory import MemoryItem
        from app.models.agent import Agent
        from app.repositories.admin_audit_repository import AdminAuditRepository
        from app.models.admin_audit_log import (
            ACTION_CASCADE_DEACTIVATE,
            RESOURCE_MEMORY,
        )

        if audit_ctx is None:
            raise ValueError(
                "bulk_soft_deactivate_memory_items_for_domain requires audit_ctx"
            )

        try:
            # Subquery: agent_id slugs in this domain.
            agent_ids_subquery = (
                self.db.query(Agent.agent_id)
                .filter(
                    Agent.tenant_id == tenant_id,
                    Agent.domain_id == domain_id,
                )
                .subquery()
            )

            # Pre-deactivation breakdown.
            breakdown_rows = (
                self.db.query(
                    MemoryItem.agent_id,
                    func.count().label("row_count"),
                )
                .filter(
                    MemoryItem.tenant_id == tenant_id,
                    MemoryItem.agent_id.in_(agent_ids_subquery),
                    MemoryItem.active.is_(True),
                )
                .group_by(MemoryItem.agent_id)
                .all()
            )
            breakdown = [
                {"agent_id": agent_id, "count": row_count}
                for (agent_id, row_count) in breakdown_rows
            ]

            count = (
                self.db.query(MemoryItem)
                .filter(
                    MemoryItem.tenant_id == tenant_id,
                    MemoryItem.agent_id.in_(agent_ids_subquery),
                    MemoryItem.active.is_(True),
                )
                .update(
                    {"active": False},
                    synchronize_session=False,
                )
            )

            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                tenant_id=tenant_id,
                action=ACTION_CASCADE_DEACTIVATE,
                resource_type=RESOURCE_MEMORY,
                resource_pk=None,
                resource_natural_id=None,
                domain_id=domain_id,
                after={
                    "count": count,
                    "scope": "domain",
                    "tenant_id": tenant_id,
                    "domain_id": domain_id,
                    "breakdown": breakdown,
                    "trigger": "domain_deactivate_cascade",
                    "updated_by": updated_by,
                },
                note=(
                    f"Cascade memory_items deactivation from domain "
                    f"{tenant_id}/{domain_id} deactivation "
                    f"(via agents subquery; PIPEDA P5)"
                ),
                autocommit=False,
            )

            if autocommit:
                self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        return count
    

    def deactivate_tenant_with_cascade(
        self,
        tenant_id: str,
        *,
        audit_ctx,
        luciel_instance_service,
        agent_repo,
        updated_by: str | None = None,
        autocommit: bool = True,
    ) -> bool:
        """Soft-deactivate a tenant and cascade leaf-first to every child.

        Cascade order (all in a single transaction):
          1. memory_items (broadest -- soft-deactivate every active row)
          2. api_keys
          3. luciel_instances (all scope levels: tenant/domain/agent)
          4. agents (new-table, Step 24.5)
          5. agent_configs (legacy)
          6. domain_configs
          7. tenant_config itself (active=False)

        Each step emits its own audit row(s). Any step failure rolls back
        the entire cascade -- no partial deactivation is possible.

        audit_ctx is REQUIRED. Tenant deactivation is the most privileged
        mutation in the platform; an audit trail is non-negotiable.

        luciel_instance_service / agent_repo are required injected
        dependencies (mirrors the deactivate_domain pattern). ApiKeyService
        has no FastAPI dep factory and is instantiated inline; it shares
        self.db so transactional atomicity is preserved.

        Returns True if the tenant was found and deactivated. Returns False
        if the tenant config row does not exist. Idempotent on re-run --
        children already inactive are skipped by the existing repo/service
        cascade methods (they filter active=True).

        autocommit=True by default for standalone callers (admin route).
        Future callers that wrap this in a larger transaction (Stripe
        billing webhook, GDPR deletion endpoint) can pass autocommit=False.
        """
        from app.models.agent_config import AgentConfig
        from app.models.domain_config import DomainConfig
        from app.services.api_key_service import ApiKeyService
        from app.repositories.admin_audit_repository import AdminAuditRepository
        from app.models.admin_audit_log import (
            ACTION_CASCADE_DEACTIVATE,
            ACTION_DEACTIVATE,
            RESOURCE_AGENT,
            RESOURCE_DOMAIN,
            RESOURCE_TENANT,
        )

        if audit_ctx is None:
            raise ValueError(
                "deactivate_tenant_with_cascade requires audit_ctx -- "
                "tenant deactivation must always be audited."
            )

        tenant = self.get_tenant_config(tenant_id)
        if tenant is None:
            return False

        was_active = bool(tenant.active)

        try:
            # --- 1. memory_items cascade (broadest leaf) ---------------
            self.bulk_soft_deactivate_memory_items_for_tenant(
                tenant_id=tenant_id,
                audit_ctx=audit_ctx,
                updated_by=updated_by,
                autocommit=False,
            )

            # --- 2. api_keys cascade -----------------------------------
            # ApiKeyService instantiated inline (no FastAPI dep factory
            # exists for it). Shares self.db -- transaction atomic.
            ApiKeyService(self.db).deactivate_all_for_tenant(
                tenant_id=tenant_id,
                audit_ctx=audit_ctx,
                autocommit=False,
            )

            # --- 3. luciel_instances cascade (all scope levels) --------
            luciel_instance_service.repo.deactivate_all_for_tenant(
                tenant_id=tenant_id,
                updated_by=updated_by,
                audit_ctx=audit_ctx,
                autocommit=False,
            )

            # --- 4. agents (new-table) cascade -------------------------
            agent_repo.deactivate_all_for_tenant(
                tenant_id=tenant_id,
                updated_by=updated_by,
                audit_ctx=audit_ctx,
                autocommit=False,
            )

            # --- 5. agent_configs (legacy) cascade (inline) ------------
            affected_agent_configs = (
                self.db.query(AgentConfig.id, AgentConfig.agent_id)
                .filter(
                    AgentConfig.tenant_id == tenant_id,
                    AgentConfig.active.is_(True),
                )
                .all()
            )
            ac_pks = [pk for pk, _ in affected_agent_configs]
            ac_ids = [nid for _, nid in affected_agent_configs]
            ac_updated = (
                self.db.query(AgentConfig)
                .filter(
                    AgentConfig.tenant_id == tenant_id,
                    AgentConfig.active.is_(True),
                )
                .update(
                    {
                        AgentConfig.active: False,
                        AgentConfig.updated_by: updated_by,
                    },
                    synchronize_session=False,
                )
            )
            if ac_updated:
                AdminAuditRepository(self.db).record(
                    ctx=audit_ctx,
                    tenant_id=tenant_id,
                    action=ACTION_CASCADE_DEACTIVATE,
                    resource_type=RESOURCE_AGENT,
                    resource_pk=None,
                    resource_natural_id=None,
                    after={
                        "count": int(ac_updated),
                        "affected_pks": ac_pks,
                        "affected_agent_ids": ac_ids,
                        "table": "agent_configs",
                        "trigger": "tenant_deactivate",
                    },
                    note=(
                        f"Cascade legacy agent_configs from tenant "
                        f"{tenant_id} deactivation"
                    ),
                    autocommit=False,
                )

            # --- 6. domain_configs cascade (inline) --------------------
            affected_domains = (
                self.db.query(DomainConfig.id, DomainConfig.domain_id)
                .filter(
                    DomainConfig.tenant_id == tenant_id,
                    DomainConfig.active.is_(True),
                )
                .all()
            )
            dc_pks = [pk for pk, _ in affected_domains]
            dc_ids = [nid for _, nid in affected_domains]
            dc_updated = (
                self.db.query(DomainConfig)
                .filter(
                    DomainConfig.tenant_id == tenant_id,
                    DomainConfig.active.is_(True),
                )
                .update(
                    {
                        DomainConfig.active: False,
                        DomainConfig.updated_by: updated_by,
                    },
                    synchronize_session=False,
                )
            )
            if dc_updated:
                AdminAuditRepository(self.db).record(
                    ctx=audit_ctx,
                    tenant_id=tenant_id,
                    action=ACTION_CASCADE_DEACTIVATE,
                    resource_type=RESOURCE_DOMAIN,
                    resource_pk=None,
                    resource_natural_id=None,
                    after={
                        "count": int(dc_updated),
                        "affected_pks": dc_pks,
                        "affected_domain_ids": dc_ids,
                        "trigger": "tenant_deactivate",
                    },
                    note=(
                        f"Cascade domain_configs from tenant "
                        f"{tenant_id} deactivation"
                    ),
                    autocommit=False,
                )

            # --- 7. tenant_config itself -------------------------------
            tenant.active = False
            if updated_by is not None:
                tenant.updated_by = updated_by

            if was_active:
                AdminAuditRepository(self.db).record(
                    ctx=audit_ctx,
                    tenant_id=tenant_id,
                    action=ACTION_DEACTIVATE,
                    resource_type=RESOURCE_TENANT,
                    resource_pk=tenant.id,
                    resource_natural_id=tenant_id,
                    before={"active": True},
                    after={"active": False},
                    note=(
                        f"Tenant {tenant_id} deactivated with full cascade "
                        f"(PIPEDA P5 retention)"
                    ),
                    autocommit=False,
                )

            if autocommit:
                self.db.commit()
                self.db.refresh(tenant)
        except Exception:
            self.db.rollback()
            raise

        return True