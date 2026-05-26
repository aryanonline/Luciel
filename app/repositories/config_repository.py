"""
Config repository.

Arc 5 Path A (V2 collapse): the legacy three-level configuration chain
(TenantConfig → DomainConfig → AgentConfig) was eliminated. V2's
hierarchy is Admin → Instance → Lead; configuration is owned by the
Admin row directly (instance-level overrides come from the Instance
model, see ``app.knowledge.chunker.resolve_effective_config``).

The ``get_domain_config`` method has been removed. The legacy
``get_tenant_config`` and ``get_agent_config`` methods survive in
collapsed form so any straggler caller in scripts/tests compiles;
new code must use ``InstanceRepository`` or ``Admin`` directly.

The legacy ``agent_configs`` table is dropped at Revision C.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.admin import Admin
from app.models.agent_config import AgentConfig

# V2 alias: TenantConfig is Admin.
TenantConfig = Admin


class ConfigRepository:

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_tenant_config(self, admin_id: str) -> Admin | None:
        """Look up the Admin row (legacy name preserved)."""
        stmt = select(Admin).where(
            Admin.id == admin_id,
            Admin.active.is_(True),
        )
        return self.db.scalars(stmt).first()

    def get_agent_config(
        self, admin_id: str, agent_id: str
    ) -> AgentConfig | None:
        """Look up the legacy agent_configs row.

        Dropped at Revision C; preserved here for transition-period
        callers that still touch the legacy table.
        """
        stmt = select(AgentConfig).where(
            AgentConfig.tenant_id == tenant_id,
            AgentConfig.agent_id == agent_id,
            AgentConfig.active.is_(True),
        )
        return self.db.scalars(stmt).first()
