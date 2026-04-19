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