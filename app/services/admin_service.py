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