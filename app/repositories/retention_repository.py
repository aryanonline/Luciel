"""
Retention repository.

Handles persistence for retention policies and deletion logs.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.retention import DeletionLog, RetentionPolicy


class RetentionRepository:

    def __init__(self, db: Session) -> None:
        self.db = db

    # ---- Retention Policies ----

    def create_policy(self, policy: RetentionPolicy) -> RetentionPolicy:
        self.db.add(policy)
        self.db.commit()
        self.db.refresh(policy)
        return policy

    def get_policy(self, policy_id: int) -> RetentionPolicy | None:
        return self.db.get(RetentionPolicy, policy_id)

    def get_policy_for_category(
        self,
        *,
        data_category: str,
        tenant_id: str | None = None,
    ) -> RetentionPolicy | None:
        """
        Get the effective policy for a data category.
        Tenant-specific policy takes priority over platform default.
        """
        # Try tenant-specific first
        if tenant_id:
            stmt = select(RetentionPolicy).where(
                RetentionPolicy.tenant_id == tenant_id,
                RetentionPolicy.data_category == data_category,
                RetentionPolicy.active == True,
            )
            result = self.db.scalars(stmt).first()
            if result:
                return result

        # Fall back to platform default (tenant_id IS NULL)
        stmt = select(RetentionPolicy).where(
            RetentionPolicy.tenant_id.is_(None),
            RetentionPolicy.data_category == data_category,
            RetentionPolicy.active == True,
        )
        return self.db.scalars(stmt).first()

    def list_policies(
        self,
        *,
        tenant_id: str | None = None,
        include_defaults: bool = True,
    ) -> list[RetentionPolicy]:
        stmt = select(RetentionPolicy).where(
            RetentionPolicy.active == True,
        ).order_by(RetentionPolicy.data_category)

        if tenant_id and include_defaults:
            # Return both tenant-specific and platform defaults
            from sqlalchemy import or_
            stmt = stmt.where(
                or_(
                    RetentionPolicy.tenant_id == tenant_id,
                    RetentionPolicy.tenant_id.is_(None),
                )
            )
        elif tenant_id:
            stmt = stmt.where(RetentionPolicy.tenant_id == tenant_id)
        else:
            stmt = stmt.where(RetentionPolicy.tenant_id.is_(None))

        return list(self.db.scalars(stmt).all())

    def update_policy(
        self,
        policy_id: int,
        **kwargs,
    ) -> RetentionPolicy | None:
        policy = self.db.get(RetentionPolicy, policy_id)
        if not policy:
            return None
        for key, value in kwargs.items():
            if hasattr(policy, key):
                setattr(policy, key, value)
        self.db.commit()
        self.db.refresh(policy)
        return policy

    def delete_policy(self, policy_id: int) -> bool:
        policy = self.db.get(RetentionPolicy, policy_id)
        if not policy:
            return False
        policy.active = False
        self.db.commit()
        return True

    # ---- Deletion Logs ----

    def log_deletion(self, log: DeletionLog) -> DeletionLog:
        self.db.add(log)
        self.db.commit()
        self.db.refresh(log)
        return log

    def list_deletion_logs(
        self,
        *,
        tenant_id: str | None = None,
        data_category: str | None = None,
        limit: int = 100,
    ) -> list[DeletionLog]:
        stmt = (
            select(DeletionLog)
            .order_by(DeletionLog.created_at.desc())
            .limit(limit)
        )
        if tenant_id:
            stmt = stmt.where(DeletionLog.tenant_id == tenant_id)
        if data_category:
            stmt = stmt.where(DeletionLog.data_category == data_category)
        return list(self.db.scalars(stmt).all())