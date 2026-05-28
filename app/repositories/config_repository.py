"""
Config repository.

Anchored to Vision v1 §3 (five configuration pillars: channels, tools,
knowledge, escalation, personality) and Architecture v1 §3.2 (Instance
subsystem). V2 hierarchy is Admin -> Instance -> Lead; configuration
is owned by the Admin row directly, with per-instance overrides
sourced from the Instance row.

Arc 10.5 cleanup: removed ``get_agent_config`` since the underlying
``agent_configs`` table was dropped before Arc 10 and the legacy
three-level configuration chain
(TenantConfig -> DomainConfig -> AgentConfig) is gone. The only
surviving method is ``get_tenant_config`` (looks up the Admin row),
retained under its legacy name because Stripe webhook handlers and
the widget E2E harness consume the name. New code should use
``InstanceRepository`` or read the Admin row directly.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.admin import Admin


# V2 alias: TenantConfig is Admin. Preserved for legacy callers.
TenantConfig = Admin


class ConfigRepository:

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_tenant_config(self, admin_id: str) -> Admin | None:
        """Look up the Admin row.

        Method name retained for legacy callers (Stripe webhook
        handlers, widget E2E). New code should read the Admin row
        via SQLAlchemy directly or via InstanceRepository for the
        instance scope.
        """
        stmt = select(Admin).where(
            Admin.id == admin_id,
            Admin.active.is_(True),
        )
        return self.db.scalars(stmt).first()
