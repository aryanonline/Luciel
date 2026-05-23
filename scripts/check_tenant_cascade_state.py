"""Read-only check of subscription + tenant state for a given tenant_id.

Designed for ECS-exec execution from inside the backend container so it
uses the same DB credentials the running task has.

Usage (inside container):
    python /app/scripts/check_tenant_cascade_state.py co-354c5056

Output is a single-line key=value report so a human reading the PowerShell
console can verify cascade landing at a glance:

    tenant_id=co-354c5056
    sub.status=canceled
    sub.active=False
    sub.cancel_at_period_end=False
    sub.canceled_at=2026-05-20T13:48:02+00:00
    sub.last_event_id=evt_1Txxxx
    tenant.is_active=False
    tenant.deactivated_at=2026-05-20T13:48:02+00:00
    children.active_luciel_instances=0
    children.active_agents=0
    children.active_domains=0
    children.active_api_keys=0
    verdict=CASCADE_COMPLETE
"""
from __future__ import annotations

import sys
from sqlalchemy import select, func

from app.core.database import get_engine
from sqlalchemy.orm import Session

from app.models.subscription import Subscription
from app.models.aliases import TenantConfig
from app.models.aliases import LucielInstance
from app.models.aliases import Agent
from app.models.aliases import DomainConfig
from app.models.api_key import ApiKey


def main(tenant_id: str) -> int:
    engine = get_engine()
    with Session(engine) as db:
        sub = db.execute(
            select(Subscription).where(Subscription.tenant_id == tenant_id)
        ).scalars().first()
        tenant = db.execute(
            select(TenantConfig).where(TenantConfig.tenant_id == tenant_id)
        ).scalars().first()

        if tenant is None:
            print(f"tenant_id={tenant_id}")
            print("verdict=TENANT_NOT_FOUND")
            return 2

        active_instances = db.execute(
            select(func.count(LucielInstance.id)).where(
                LucielInstance.tenant_id == tenant_id,
                LucielInstance.active.is_(True),
            )
        ).scalar() or 0
        active_agents = db.execute(
            select(func.count(Agent.id)).where(
                Agent.tenant_id == tenant_id,
                Agent.active.is_(True),
            )
        ).scalar() or 0
        active_domains = db.execute(
            select(func.count(DomainConfig.id)).where(
                DomainConfig.tenant_id == tenant_id,
                DomainConfig.active.is_(True),
            )
        ).scalar() or 0
        active_keys = db.execute(
            select(func.count(ApiKey.id)).where(
                ApiKey.tenant_id == tenant_id,
                ApiKey.active.is_(True),
            )
        ).scalar() or 0

        print(f"tenant_id={tenant_id}")
        if sub is None:
            print("sub.status=NO_SUBSCRIPTION_ROW")
        else:
            print(f"sub.status={sub.status}")
            print(f"sub.active={sub.active}")
            print(f"sub.cancel_at_period_end={sub.cancel_at_period_end}")
            print(f"sub.canceled_at={sub.canceled_at.isoformat() if sub.canceled_at else None}")
            print(f"sub.last_event_id={sub.last_event_id}")
        print(f"tenant.is_active={tenant.active}")
        print(f"tenant.deactivated_at={tenant.deactivated_at.isoformat() if tenant.deactivated_at else None}")
        print(f"children.active_luciel_instances={active_instances}")
        print(f"children.active_agents={active_agents}")
        print(f"children.active_domains={active_domains}")
        print(f"children.active_api_keys={active_keys}")

        cascade_complete = (
            tenant.active is False
            and active_instances == 0
            and active_agents == 0
            and active_domains == 0
            and active_keys == 0
        )
        if cascade_complete:
            print("verdict=CASCADE_COMPLETE")
            return 0
        else:
            print("verdict=CASCADE_INCOMPLETE")
            return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python check_tenant_cascade_state.py <tenant_id>", file=sys.stderr)
        sys.exit(64)
    sys.exit(main(sys.argv[1]))
