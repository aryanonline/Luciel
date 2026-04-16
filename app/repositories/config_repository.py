"""
Config repository.

Handles database lookups for tenant, domain, and agent configurations.
These are the structured settings that control how a child Luciel
behaves at each level of the hierarchy.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.agent_config import AgentConfig
from app.models.domain_config import DomainConfig
from app.models.tenant import TenantConfig


class ConfigRepository:

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_tenant_config(self, tenant_id: str) -> TenantConfig | None:
        """Look up the config for a tenant."""
        stmt = select(TenantConfig).where(
            TenantConfig.tenant_id == tenant_id,
            TenantConfig.active.is_(True),
        )
        return self.db.scalars(stmt).first()

    def get_domain_config(
        self, tenant_id: str, domain_id: str
    ) -> DomainConfig | None:
        """Look up the config for a specific tenant/domain combination."""
        stmt = select(DomainConfig).where(
            DomainConfig.tenant_id == tenant_id,
            DomainConfig.domain_id == domain_id,
            DomainConfig.active.is_(True),
        )
        return self.db.scalars(stmt).first()

    def get_agent_config(
        self, tenant_id: str, agent_id: str
    ) -> AgentConfig | None:
        """Look up the config for a specific agent within a tenant."""
        stmt = select(AgentConfig).where(
            AgentConfig.tenant_id == tenant_id,
            AgentConfig.agent_id == agent_id,
            AgentConfig.active.is_(True),
        )
        return self.db.scalars(stmt).first()