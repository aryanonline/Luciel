"""
Retention repository.

Handles persistence for retention policies and deletion logs.

RESCAN TIER-DE(ent) — tier-default layer:
The ``get_effective_retention_days`` method implements the three-layer
resolution order documented in ``app/policy/retention_rules.py``:
    1. Tenant override  (RetentionPolicy row with admin_id = <admin>)
    2. Tier default     (TIER_RETENTION_DEFAULTS[tier][category])
    3. Platform default (RetentionPolicy row with admin_id IS NULL)
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.retention import DeletionLog, RetentionPolicy
from app.policy.retention_rules import resolve_retention_days


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
        admin_id: str | None = None,
    ) -> RetentionPolicy | None:
        """
        Get the effective policy for a data category.
        Tenant-specific policy takes priority over platform default.
        """
        # Try tenant-specific first
        if admin_id:
            stmt = select(RetentionPolicy).where(
                RetentionPolicy.admin_id == admin_id,
                RetentionPolicy.data_category == data_category,
                RetentionPolicy.active == True,
            )
            result = self.db.scalars(stmt).first()
            if result:
                return result

        # Fall back to platform default (admin_id IS NULL)
        stmt = select(RetentionPolicy).where(
            RetentionPolicy.admin_id.is_(None),
            RetentionPolicy.data_category == data_category,
            RetentionPolicy.active == True,
        )
        return self.db.scalars(stmt).first()

    def list_policies(
        self,
        *,
        admin_id: str | None = None,
        include_defaults: bool = True,
    ) -> list[RetentionPolicy]:
        stmt = select(RetentionPolicy).where(
            RetentionPolicy.active == True,
        ).order_by(RetentionPolicy.data_category)

        if admin_id and include_defaults:
            # Return both tenant-specific and platform defaults
            from sqlalchemy import or_
            stmt = stmt.where(
                or_(
                    RetentionPolicy.admin_id == admin_id,
                    RetentionPolicy.admin_id.is_(None),
                )
            )
        elif admin_id:
            stmt = stmt.where(RetentionPolicy.admin_id == admin_id)
        else:
            stmt = stmt.where(RetentionPolicy.admin_id.is_(None))

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

    def get_effective_retention_days(
        self,
        *,
        data_category: str,
        admin_id: str | None = None,
        tier: str | None = None,
    ) -> int | None:
        """Resolve the effective retention_days for a (category, admin, tier).

        Implements the three-layer resolution order from
        ``app/policy/retention_rules.py`` (RESCAN TIER-DE(ent)):

            1. Tenant override  — active RetentionPolicy row with
               admin_id = <admin_id>. Represents an explicit per-tenant
               configuration that always wins.
            2. Tier default     — TIER_RETENTION_DEFAULTS[tier][category]
               from ``retention_rules.py``. The new tier-aware middle layer
               that replaces the flat 730-day platform default for
               transcript/summary categories.
            3. Platform default — active RetentionPolicy row with
               admin_id IS NULL (seeded from PLATFORM_DEFAULTS).

        Returns None if no layer provides a value (treat as no auto-purge).
        """
        # Layer 1: tenant-specific override
        tenant_days: int | None = None
        if admin_id:
            stmt = select(RetentionPolicy).where(
                RetentionPolicy.admin_id == admin_id,
                RetentionPolicy.data_category == data_category,
                RetentionPolicy.active == True,
            )
            tenant_policy = self.db.scalars(stmt).first()
            if tenant_policy is not None:
                tenant_days = tenant_policy.retention_days

        # Layer 3: platform default (fetched regardless, used only if layers
        # 1+2 don't yield a value)
        platform_days: int | None = None
        stmt = select(RetentionPolicy).where(
            RetentionPolicy.admin_id.is_(None),
            RetentionPolicy.data_category == data_category,
            RetentionPolicy.active == True,
        )
        platform_policy = self.db.scalars(stmt).first()
        if platform_policy is not None:
            platform_days = platform_policy.retention_days

        # Delegate to the policy-layer resolver (layers 1, 2, 3).
        return resolve_retention_days(
            data_category=data_category,
            tier=tier,
            tenant_override_days=tenant_days,
            platform_default_days=platform_days,
        )

    # ---- Deletion Logs ----

    def log_deletion(self, log: DeletionLog) -> DeletionLog:
        self.db.add(log)
        self.db.commit()
        self.db.refresh(log)
        return log

    def list_deletion_logs(
        self,
        *,
        admin_id: str | None = None,
        data_category: str | None = None,
        limit: int = 100,
    ) -> list[DeletionLog]:
        stmt = (
            select(DeletionLog)
            .order_by(DeletionLog.created_at.desc())
            .limit(limit)
        )
        if admin_id:
            stmt = stmt.where(DeletionLog.admin_id == admin_id)
        if data_category:
            stmt = stmt.where(DeletionLog.data_category == data_category)
        return list(self.db.scalars(stmt).all())