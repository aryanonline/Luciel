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
          1. conversations  (NEW Step 30a.2 -- soft-delete, stamp deactivated_at)
          2. identity_claims (NEW Step 30a.2 -- soft-delete, stamp deactivated_at)
          3. memory_items (broadest leaf below)
          4. api_keys
          5. luciel_instances (all scope levels: tenant/domain/agent)
          6. agents (new-table, Step 24.5)
          7. agent_configs (legacy)
          8. domain_configs
          9. tenant_config itself (active=False, deactivated_at=now())

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

        Step 30a.2 -- closes
        D-cancellation-cascade-incomplete-conversations-claims-2026-05-14:
        the cascade now also visits ``conversations`` and
        ``identity_claims`` (both have ``tenant_id`` + ``active`` columns
        and were unreachable in the old 7-layer walk). And the tenant_config
        step itself now stamps ``deactivated_at = now()`` so the retention
        worker can compute the 90d purge cutoff.

        Step 30a.2 -- NOTE on sessions / messages:
        ``sessions`` carries no soft-delete shape (no ``active`` column)
        and ``messages`` has no ``active`` column either. Both are handled
        at retention-time hard-purge via ``hard_delete_tenant_after_retention``
        and SQL FK CASCADE on ``messages.session_id``. See the Step 30a.2
        design plan §2 for the full trace.
        """
        from sqlalchemy import func

        from app.models.agent_config import AgentConfig
        from app.models.conversation import Conversation
        from app.models.domain_config import DomainConfig
        from app.models.identity_claim import IdentityClaim
        from app.services.api_key_service import ApiKeyService
        from app.repositories.admin_audit_repository import AdminAuditRepository
        from app.models.admin_audit_log import (
            ACTION_CASCADE_DEACTIVATE,
            ACTION_DEACTIVATE,
            RESOURCE_AGENT,
            RESOURCE_CONVERSATION,
            RESOURCE_DOMAIN,
            RESOURCE_IDENTITY_CLAIM,
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
            # --- 1. conversations cascade (NEW Step 30a.2) -------------
            # Soft-deactivate every active conversation under this tenant.
            # Stamp deactivated_at = now() in the same UPDATE so future
            # per-conversation retention queries have the timestamp.
            # Uses Conversation directly (no separate repo method) for
            # symmetry with the agent_configs / domain_configs inline
            # cascades below; the table is conceptually identical in
            # treatment (soft-delete + audit row + count).
            affected_conversations = (
                self.db.query(Conversation.id)
                .filter(
                    Conversation.tenant_id == tenant_id,
                    Conversation.active.is_(True),
                )
                .all()
            )
            conv_ids = [str(pk) for (pk,) in affected_conversations]
            conv_updated = (
                self.db.query(Conversation)
                .filter(
                    Conversation.tenant_id == tenant_id,
                    Conversation.active.is_(True),
                )
                .update(
                    {
                        Conversation.active: False,
                        Conversation.deactivated_at: func.now(),
                    },
                    synchronize_session=False,
                )
            )
            if conv_updated:
                AdminAuditRepository(self.db).record(
                    ctx=audit_ctx,
                    tenant_id=tenant_id,
                    action=ACTION_CASCADE_DEACTIVATE,
                    resource_type=RESOURCE_CONVERSATION,
                    resource_pk=None,
                    resource_natural_id=None,
                    after={
                        "count": int(conv_updated),
                        "affected_conversation_ids": conv_ids,
                        "table": "conversations",
                        "trigger": "tenant_deactivate",
                    },
                    note=(
                        f"Cascade conversations from tenant "
                        f"{tenant_id} deactivation (Step 30a.2)"
                    ),
                    autocommit=False,
                )

            # --- 2. identity_claims cascade (NEW Step 30a.2) -----------
            # Soft-deactivate every active identity_claim under this
            # tenant. claim_value is PII (email / phone) so this row
            # must be honored under PIPEDA Principle 5. Audit row
            # records affected count + claim row pks (NOT claim_value,
            # to avoid duplicating PII into the audit chain). The
            # underlying row itself stays in the DB until retention
            # hard-purge -- soft-delete is the PIPEDA-respecting
            # "limited use" shape, hard-delete is the "limited
            # retention" shape.
            affected_claims = (
                self.db.query(IdentityClaim.id)
                .filter(
                    IdentityClaim.tenant_id == tenant_id,
                    IdentityClaim.active.is_(True),
                )
                .all()
            )
            claim_pks = [str(pk) for (pk,) in affected_claims]
            claims_updated = (
                self.db.query(IdentityClaim)
                .filter(
                    IdentityClaim.tenant_id == tenant_id,
                    IdentityClaim.active.is_(True),
                )
                .update(
                    {
                        IdentityClaim.active: False,
                        IdentityClaim.deactivated_at: func.now(),
                    },
                    synchronize_session=False,
                )
            )
            if claims_updated:
                AdminAuditRepository(self.db).record(
                    ctx=audit_ctx,
                    tenant_id=tenant_id,
                    action=ACTION_CASCADE_DEACTIVATE,
                    resource_type=RESOURCE_IDENTITY_CLAIM,
                    resource_pk=None,
                    resource_natural_id=None,
                    after={
                        "count": int(claims_updated),
                        "affected_claim_pks": claim_pks,
                        "table": "identity_claims",
                        "trigger": "tenant_deactivate",
                    },
                    note=(
                        f"Cascade identity_claims from tenant "
                        f"{tenant_id} deactivation (Step 30a.2)"
                    ),
                    autocommit=False,
                )

            # --- 3. memory_items cascade (broadest leaf) ---------------
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

            # --- 9. tenant_config itself -------------------------------
            # Step 30a.2: also stamp deactivated_at = now() so the
            # retention worker can compute the 90d purge cutoff. Only
            # set when was_active=True (idempotent re-runs don't
            # re-stamp -- preserves the original deactivation moment).
            tenant.active = False
            if was_active:
                tenant.deactivated_at = func.now()
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
                    after={
                        "active": False,
                        # "deactivated_at" left as a server-stamped
                        # marker; the actual timestamp lives in the row
                        # itself. Including "now" here would create a
                        # second source of truth that could drift.
                    },
                    note=(
                        f"Tenant {tenant_id} deactivated with full cascade "
                        f"(PIPEDA P5 retention; Step 30a.2 9-layer)"
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

    # ---------------------------------------------------------------
    # Step 30a.2 — retention hard-purge (PIPEDA Principle 5)
    # ---------------------------------------------------------------
    #
    # Companion to deactivate_tenant_with_cascade. The cascade does
    # soft-deletion (active=false + deactivated_at=now); this method
    # does HARD-deletion of every row scoped to a tenant after the
    # 90-day retention window has elapsed.
    #
    # Called by the nightly Celery beat job in
    # app/worker/tasks/retention.py::run_retention_purge.
    #
    # Order matters: we delete leaf-first to satisfy the FK RESTRICT
    # constraints that protect tenant_configs.tenant_id from cascade-
    # delete. ``conversations.tenant_id`` and
    # ``identity_claims.tenant_id`` both have ON DELETE RESTRICT to
    # tenant_configs.tenant_id, so we MUST delete them before the
    # parent tenant_configs row. Same for any other FK-RESTRICT
    # children that may exist; we delete them all explicitly rather
    # than relying on FK behavior so the row-count audit is honest.

    def hard_delete_tenant_after_retention(
        self,
        tenant_id: str,
        *,
        retention_window_days: int = 90,
    ) -> dict[str, int]:
        """Hard-delete every row scoped to ``tenant_id`` after retention.

        Re-verifies the retention predicate (active=false AND
        deactivated_at < now - N days) inside this transaction as an
        idempotency guard. If the row is not eligible (already purged,
        re-activated, or insufficient retention age), returns an empty
        dict and makes no DB changes.

        Order of deletion (leaf-first, RESTRICT-safe):
           1. messages          (via sessions FK CASCADE -- implicit)
           2. sessions          WHERE tenant_id=:tid
           3. conversations     WHERE tenant_id=:tid
           4. identity_claims   WHERE tenant_id=:tid
           5. memory_items      WHERE tenant_id=:tid
           6. api_keys          WHERE tenant_id=:tid
           7. luciel_instances  WHERE tenant_id=:tid
           8. agents            WHERE tenant_id=:tid
           9. agent_configs     WHERE tenant_id=:tid
          10. domain_configs    WHERE tenant_id=:tid
          11. tenant_configs    WHERE tenant_id=:tid
          12. AdminAuditLog row recording the purge (action=
              ACTION_TENANT_HARD_PURGED) with per-table row-count map.

        Subscriptions are intentionally NOT purged -- they carry
        billing history needed for tax/accounting retention which
        has its own clock.

        Returns a dict mapping table name -> row count deleted.
        Empty dict means the row was not eligible (idempotency guard
        fired). The caller (Celery task) is responsible for the
        outer transaction commit; this method does NOT commit -- it
        runs in the caller's transaction so the audit row + DELETEs
        are atomic.

        Raises if the tenant_configs row exists but is still active
        or has NULL deactivated_at -- those are safety-net conditions
        that should never happen if the cascade is the only writer.
        """
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import text as sql_text

        from app.models.admin_audit_log import (
            ACTION_TENANT_HARD_PURGED,
            RESOURCE_TENANT,
        )
        from app.models.tenant import TenantConfig
        from app.repositories.admin_audit_repository import AdminAuditRepository

        # ---- Idempotency guard: re-verify retention predicate ----
        # This is intentionally done inside the same transaction as
        # the DELETEs (not as a pre-flight) so a concurrent reactivate
        # cannot race past us.
        tenant = self.get_tenant_config(tenant_id)
        if tenant is None:
            # Already hard-purged on a prior run, or never existed.
            return {}

        if tenant.active:
            raise RuntimeError(
                f"hard_delete_tenant_after_retention called on ACTIVE "
                f"tenant {tenant_id!r} -- this should never happen. "
                f"The cascade is the only writer of tenant_configs."
                f"active=false; reactivation must roll back deactivated_at."
            )

        if tenant.deactivated_at is None:
            raise RuntimeError(
                f"hard_delete_tenant_after_retention called on tenant "
                f"{tenant_id!r} with NULL deactivated_at -- this row "
                f"was deactivated before Step 30a.2 and is excluded "
                f"from automated purge by design. Manual purge only."
            )

        cutoff = datetime.now(timezone.utc) - timedelta(
            days=retention_window_days
        )
        # tenant.deactivated_at is timezone-aware (timestamptz) so the
        # comparison is well-defined; mixing tz-aware and naive would
        # raise TypeError, which is the correct behavior.
        if tenant.deactivated_at >= cutoff:
            # Eligible per the scan but raced -- another beat or a
            # bug shrank the window. Defensive skip.
            return {}

        # ---- Hard-delete chain ----
        row_counts: dict[str, int] = {}

        # Each DELETE returns an estimated row count via .rowcount;
        # for some dialects this is -1 when the driver can't tell.
        # We coerce to int and store; the audit row reflects what we
        # actually saw, even if -1.
        def _delete(sql: str) -> int:
            res = self.db.execute(sql_text(sql), {"tid": tenant_id})
            return int(res.rowcount or 0)

        # 1. messages cascade via SQL FK on sessions (implicit). We
        #    don't issue a DELETE here -- step 2's DELETE FROM sessions
        #    cascades to messages via ON DELETE CASCADE. We record the
        #    pre-count for the audit row's row-count map though.
        pre_msg_count = int(
            self.db.execute(
                sql_text(
                    "SELECT COUNT(*) FROM messages m "
                    "JOIN sessions s ON s.id = m.session_id "
                    "WHERE s.tenant_id = :tid"
                ),
                {"tid": tenant_id},
            ).scalar()
            or 0
        )
        row_counts["messages"] = pre_msg_count

        # 2. sessions (cascades to messages via FK)
        row_counts["sessions"] = _delete(
            "DELETE FROM sessions WHERE tenant_id = :tid"
        )

        # 3. conversations
        row_counts["conversations"] = _delete(
            "DELETE FROM conversations WHERE tenant_id = :tid"
        )

        # 4. identity_claims
        row_counts["identity_claims"] = _delete(
            "DELETE FROM identity_claims WHERE tenant_id = :tid"
        )

        # 5. memory_items
        row_counts["memory_items"] = _delete(
            "DELETE FROM memory_items WHERE tenant_id = :tid"
        )

        # 6. api_keys
        row_counts["api_keys"] = _delete(
            "DELETE FROM api_keys WHERE tenant_id = :tid"
        )

        # 7. luciel_instances
        row_counts["luciel_instances"] = _delete(
            "DELETE FROM luciel_instances WHERE tenant_id = :tid"
        )

        # 8. agents (new-table, Step 24.5)
        row_counts["agents"] = _delete(
            "DELETE FROM agents WHERE tenant_id = :tid"
        )

        # 9. agent_configs (legacy)
        row_counts["agent_configs"] = _delete(
            "DELETE FROM agent_configs WHERE tenant_id = :tid"
        )

        # 10. domain_configs
        row_counts["domain_configs"] = _delete(
            "DELETE FROM domain_configs WHERE tenant_id = :tid"
        )

        # 11. tenant_configs (the parent row itself)
        row_counts["tenant_configs"] = _delete(
            "DELETE FROM tenant_configs WHERE tenant_id = :tid"
        )

        # 12. Audit row -- write to AdminAuditLog with full row-count
        # manifest. The audit row uses the resource_natural_id field
        # to preserve tenant_id as a searchable string AFTER the
        # tenant_configs row itself is gone; the row_hash chain stays
        # walkable because AdminAuditLog rows are never FK'd to
        # tenant_configs.
        # Note: audit row is written through AuditContext.system()
        # because this is a background-task action with no HTTP caller.
        # The system() factory tags actor_permissions=('system',) and
        # actor_tenant_id=SYSTEM_ACTOR_TENANT so retention rows are
        # distinguishable from worker-task rows (which use ('worker',)).
        from app.repositories.admin_audit_repository import AuditContext

        system_ctx = AuditContext.system(label="retention_worker")
        AdminAuditRepository(self.db).record(
            ctx=system_ctx,
            tenant_id=tenant_id,
            action=ACTION_TENANT_HARD_PURGED,
            resource_type=RESOURCE_TENANT,
            resource_pk=None,
            resource_natural_id=tenant_id,
            after={
                "row_counts": row_counts,
                "retention_window_days": retention_window_days,
                "trigger": "retention_worker",
            },
            note=(
                f"Hard-purge of tenant {tenant_id} after "
                f"{retention_window_days}d retention (PIPEDA P5)"
            ),
            autocommit=False,
        )

        return row_counts

    # ---------------------------------------------------------------
    # Step 30a.1 — tier/scope guard
    # ---------------------------------------------------------------
    #
    # Called from the POST /admin/luciel-instances route (the ONE
    # self-serve creation chokepoint) BEFORE LucielInstanceService.
    # create_instance. Service-layer enforcement is intentional:
    #
    #   * the schema layer cannot know the caller's active subscription
    #     (subscriptions are loaded by tenant_id from a DB lookup);
    #   * the policy layer (ScopePolicy) checks API-key authority, not
    #     billing entitlement, and we want those concerns separate.
    #
    # Outcomes:
    #   * tenant has no active subscription → 402 (treated as Individual
    #     fall-through is too generous; we fail closed here so sales-
    #     assisted tenants without a subscription row cannot use the
    #     self-serve route at all -- they should call admin paths instead).
    #   * requested scope_level not in tier's permitted set → 402
    #   * tenant already at instance_count_cap → 402
    #   * otherwise → silent.

    def _enforce_tier_scope(
        self,
        *,
        tenant_id: str,
        requested_scope_level: str,
    ) -> None:
        """Step 30a.1: assert (tenant.active_subscription, requested_scope_level)
        is a permitted pair AND that the tenant has not exceeded its cap.

        Raises ``TierScopeViolationError`` (mapped to 402 by the route
        layer). On success returns silently.
        """
        # Local imports keep AdminService importable from contexts that
        # don't have the LucielInstance / Subscription stack loaded.
        from app.models.subscription import (
            Subscription,
            TIER_PERMITTED_SCOPES,
        )
        from app.repositories.luciel_instance_repository import (
            LucielInstanceRepository,
        )
        from app.services.luciel_instance_service import TierScopeViolationError

        sub: Subscription | None = (
            self.db.query(Subscription)
            .filter(
                Subscription.tenant_id == tenant_id,
                Subscription.active.is_(True),
            )
            .order_by(Subscription.id.desc())
            .first()
        )
        if sub is None:
            # No active subscription -- fail closed. Sales-assisted /
            # manually-provisioned tenants don't hit this path because
            # they go through admin tooling that bypasses the self-serve
            # cap; the route layer is the only caller of this guard.
            raise TierScopeViolationError(
                f"Tenant {tenant_id!r} has no active subscription; "
                f"cannot create LucielInstance via self-serve path.",
                reason=TierScopeViolationError.REASON_NO_ACTIVE_SUBSCRIPTION,
            )

        permitted = TIER_PERMITTED_SCOPES.get(sub.tier, ())
        if requested_scope_level not in permitted:
            raise TierScopeViolationError(
                f"Subscription tier {sub.tier!r} does not permit scope_level="
                f"{requested_scope_level!r}. Permitted scope levels for this "
                f"tier: {sorted(permitted)}. Upgrade to a higher tier to "
                f"create {requested_scope_level}-scope LucielInstances.",
                reason=TierScopeViolationError.REASON_SCOPE_NOT_PERMITTED,
            )

        cap = int(sub.instance_count_cap or 0)
        if cap > 0:
            used = LucielInstanceRepository(self.db).count_active_for_tenant(tenant_id)
            if used >= cap:
                raise TierScopeViolationError(
                    f"Tenant {tenant_id!r} has reached its instance_count_cap="
                    f"{cap} (currently {used} active LucielInstances). "
                    f"Deactivate an existing Luciel or upgrade your tier.",
                    reason=TierScopeViolationError.REASON_CAP_EXCEEDED,
                )
        # else: cap=0 means "unmetered" (used by sales-assisted tenants we
        # backfill manually). We do not enforce here.