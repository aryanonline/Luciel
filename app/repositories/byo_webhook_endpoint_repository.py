"""ByoWebhookEndpoint repository — Arc 12 WU6.

Pure CRUD against the ``byo_webhook_endpoints`` table. The
subprocess sandbox (``app/tools/byo_sandbox.py``) reads through this
repo at dispatch time; the admin tool-config API (out of scope for
WU6 — see WU7/admin-UX work) writes through it.

All read methods filter on ``(admin_id, instance_id)`` — Wall-1 +
Wall-3.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from app.models.byo_webhook_endpoint import ByoWebhookEndpoint


class ByoWebhookEndpointRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_live_by_id(
        self,
        *,
        admin_id: str,
        instance_id: int,
        endpoint_id: int,
    ) -> Optional[ByoWebhookEndpoint]:
        """Return the live BYO endpoint row for the tuple, or None."""
        stmt = (
            select(ByoWebhookEndpoint)
            .where(
                and_(
                    ByoWebhookEndpoint.id == endpoint_id,
                    ByoWebhookEndpoint.admin_id == admin_id,
                    ByoWebhookEndpoint.instance_id == instance_id,
                    ByoWebhookEndpoint.revoked_at.is_(None),
                )
            )
            .limit(1)
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def register(
        self,
        *,
        admin_id: str,
        instance_id: int,
        endpoint_url: str,
        input_schema: dict,
        output_schema: dict,
        allowed_domains: list[str],
        autocommit: bool = True,
    ) -> ByoWebhookEndpoint:
        """Register a new BYO endpoint (admin tool-config time)."""
        row = ByoWebhookEndpoint(
            admin_id=admin_id,
            instance_id=instance_id,
            endpoint_url=endpoint_url,
            input_schema=input_schema,
            output_schema=output_schema,
            allowed_domains=allowed_domains,
        )
        self.db.add(row)
        if autocommit:
            self.db.commit()
            self.db.refresh(row)
        else:
            self.db.flush()
        return row

    def revoke(
        self,
        *,
        admin_id: str,
        instance_id: int,
        endpoint_id: int,
        autocommit: bool = True,
    ) -> bool:
        now = datetime.now(timezone.utc)
        stmt = (
            update(ByoWebhookEndpoint)
            .where(
                and_(
                    ByoWebhookEndpoint.id == endpoint_id,
                    ByoWebhookEndpoint.admin_id == admin_id,
                    ByoWebhookEndpoint.instance_id == instance_id,
                    ByoWebhookEndpoint.revoked_at.is_(None),
                )
            )
            .values(revoked_at=now, updated_at=now)
        )
        result = self.db.execute(stmt)
        if autocommit:
            self.db.commit()
        return result.rowcount > 0

    def list_for_instance(
        self,
        *,
        admin_id: str,
        instance_id: int,
        include_revoked: bool = False,
    ) -> list[ByoWebhookEndpoint]:
        conditions = [
            ByoWebhookEndpoint.admin_id == admin_id,
            ByoWebhookEndpoint.instance_id == instance_id,
        ]
        if not include_revoked:
            conditions.append(ByoWebhookEndpoint.revoked_at.is_(None))
        stmt = (
            select(ByoWebhookEndpoint)
            .where(and_(*conditions))
            .order_by(ByoWebhookEndpoint.created_at.desc())
        )
        return list(self.db.execute(stmt).scalars())
