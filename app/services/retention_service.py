"""
Retention service.

Handles CRUD for retention policies and executes data purging
(deletion or anonymization) based on configured retention periods.

PIPEDA Principles addressed:
  4.5   — Limiting Use, Disclosure, and Retention
  4.5.2 — Retained long enough for individual access after decisions
  4.5.3 — Destroy, erase, or anonymize when no longer needed

Gap fix: Purges run in batches of BATCH_SIZE to avoid long table
locks on large datasets. Each batch commits independently.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update, delete, func
from sqlalchemy.orm import Session

from app.models.deletion_log import DeletionLog
from app.models.memory import MemoryItem
from app.models.message import MessageModel
from app.models.retention_policy import RetentionPolicy
from app.models.session import SessionModel
from app.models.trace import Trace

logger = logging.getLogger(__name__)

# How many rows to process per batch during purge/anonymize.
# Keeps table locks short so normal chat requests aren't blocked.
BATCH_SIZE = 5000

# Maps data_category to (model, date_column, columns to anonymize).
# For messages: we join through session to get tenant_id (see _build_message_tenant_filter).
_CATEGORY_MAP = {
    "sessions": {
        "model": SessionModel,
        "date_col": "created_at",
        "has_tenant_id": True,
        "anon_cols": {"user_id": "[redacted]"},
    },
    "messages": {
        "model": MessageModel,
        "date_col": "created_at",
        "has_tenant_id": False,  # messages link to sessions via session_id
        "anon_cols": {"content": "[redacted]"},
    },
    "memory_items": {
        "model": MemoryItem,
        "date_col": "created_at",
        "has_tenant_id": True,
        "anon_cols": {"user_id": "[redacted]", "content": "[redacted]"},
    },
    "traces": {
        "model": Trace,
        "date_col": "created_at",
        "has_tenant_id": True,
        "anon_cols": {"user_message": "[redacted]", "assistant_reply": "[redacted]", "user_id": "[redacted]"},
    },
}


class RetentionService:

    def __init__(self, db: Session) -> None:
        self.db = db

    # ----------------------------------------------------------------
    # CRUD for retention policies
    # ----------------------------------------------------------------

    def create_policy(self, data: dict) -> RetentionPolicy:
        policy = RetentionPolicy(**data)
        self.db.add(policy)
        self.db.commit()
        self.db.refresh(policy)
        return policy

    def get_policy(self, policy_id: int) -> RetentionPolicy | None:
        return self.db.get(RetentionPolicy, policy_id)

    def list_policies(self, tenant_id: str | None = None) -> list[RetentionPolicy]:
        stmt = select(RetentionPolicy).order_by(RetentionPolicy.data_category)
        if tenant_id is not None:
            stmt = stmt.where(RetentionPolicy.tenant_id == tenant_id)
        return list(self.db.scalars(stmt).all())

    def update_policy(self, policy_id: int, data: dict) -> RetentionPolicy | None:
        policy = self.db.get(RetentionPolicy, policy_id)
        if policy is None:
            return None
        for key, value in data.items():
            setattr(policy, key, value)
        self.db.commit()
        self.db.refresh(policy)
        return policy

    def delete_policy(self, policy_id: int) -> bool:
        policy = self.db.get(RetentionPolicy, policy_id)
        if policy is None:
            return False
        self.db.delete(policy)
        self.db.commit()
        return True

    # ----------------------------------------------------------------
    # Deletion log (read-only)
    # ----------------------------------------------------------------

    def list_deletion_logs(
        self,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[DeletionLog]:
        stmt = (
            select(DeletionLog)
            .order_by(DeletionLog.created_at.desc())
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(DeletionLog.tenant_id == tenant_id)
        return list(self.db.scalars(stmt).all())

    # ----------------------------------------------------------------
    # Enforcement
    # ----------------------------------------------------------------

    def enforce_all_policies(self, triggered_by: str = "scheduler") -> list[dict]:
        """
        Run all active retention policies and purge/anonymize expired data.
        Returns a summary of actions taken.
        """
        policies = self.list_policies()
        results = []

        for policy in policies:
            if policy.retention_days == 0:
                continue

            category_info = _CATEGORY_MAP.get(policy.data_category)
            if category_info is None:
                logger.warning("Unknown data category: %s", policy.data_category)
                continue

            try:
                affected = self._enforce_single_policy(
                    policy=policy,
                    category_info=category_info,
                    triggered_by=triggered_by,
                )
                results.append({
                    "policy_id": policy.id,
                    "data_category": policy.data_category,
                    "tenant_id": policy.tenant_id,
                    "action": policy.action,
                    "records_affected": affected,
                })
            except Exception as exc:
                logger.error(
                    "Retention enforcement failed for policy %s: %s",
                    policy.id, exc,
                )
                self.db.rollback()

        return results

    def manual_purge(
        self,
        *,
        tenant_id: str | None,
        data_category: str,
        reason: str,
    ) -> int:
        """Admin-triggered manual purge."""
        policy = self._resolve_policy(tenant_id, data_category)

        category_info = _CATEGORY_MAP.get(data_category)
        if category_info is None:
            raise ValueError(f"Unknown data category: {data_category}")

        retention_days = policy.retention_days if policy else 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

        affected = self._execute_purge_batched(
            category_info=category_info,
            cutoff=cutoff,
            tenant_id=tenant_id,
        )

        self._log_action(
            tenant_id=tenant_id,
            data_category=data_category,
            action_taken="deleted",
            records_affected=affected,
            retention_days_applied=retention_days,
            triggered_by="admin",
            reason=reason,
        )

        return affected

    # ----------------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------------

    def _resolve_policy(
        self,
        tenant_id: str | None,
        data_category: str,
    ) -> RetentionPolicy | None:
        """Tenant-level overrides platform defaults."""
        stmt = select(RetentionPolicy).where(
            RetentionPolicy.data_category == data_category,
        )

        if tenant_id:
            tenant_stmt = stmt.where(RetentionPolicy.tenant_id == tenant_id)
            policy = self.db.scalars(tenant_stmt).first()
            if policy:
                return policy

        default_stmt = stmt.where(RetentionPolicy.tenant_id.is_(None))
        return self.db.scalars(default_stmt).first()

    def _enforce_single_policy(
        self,
        *,
        policy: RetentionPolicy,
        category_info: dict,
        triggered_by: str,
    ) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=policy.retention_days)

        if policy.action == "anonymize":
            affected = self._execute_anonymize_batched(
                category_info=category_info,
                cutoff=cutoff,
                tenant_id=policy.tenant_id,
            )
            action_taken = "anonymized"
        else:
            affected = self._execute_purge_batched(
                category_info=category_info,
                cutoff=cutoff,
                tenant_id=policy.tenant_id,
            )
            action_taken = "deleted"

        if affected > 0:
            self._log_action(
                tenant_id=policy.tenant_id,
                data_category=policy.data_category,
                action_taken=action_taken,
                records_affected=affected,
                retention_days_applied=policy.retention_days,
                triggered_by=triggered_by,
            )

        return affected

    # ----------------------------------------------------------------
    # Batched delete — processes BATCH_SIZE rows at a time
    # ----------------------------------------------------------------

    def _execute_purge_batched(
        self,
        *,
        category_info: dict,
        cutoff: datetime,
        tenant_id: str | None,
    ) -> int:
        """Delete rows older than cutoff in batches."""
        model = category_info["model"]
        date_col = getattr(model, category_info["date_col"])
        has_tenant_id = category_info["has_tenant_id"]
        total_affected = 0

        while True:
            # Find IDs for the next batch
            id_col = model.id
            stmt = select(id_col).where(date_col < cutoff).limit(BATCH_SIZE)

            if tenant_id and has_tenant_id:
                stmt = stmt.where(model.tenant_id == tenant_id)
            elif tenant_id and not has_tenant_id:
                # Messages: filter through session relationship
                stmt = self._apply_message_tenant_filter(stmt, tenant_id, cutoff)

            batch_ids = list(self.db.scalars(stmt).all())
            if not batch_ids:
                break

            del_stmt = delete(model).where(id_col.in_(batch_ids))
            result = self.db.execute(del_stmt)
            self.db.commit()
            total_affected += result.rowcount

            if len(batch_ids) < BATCH_SIZE:
                break  # Last batch

        return total_affected

    # ----------------------------------------------------------------
    # Batched anonymize — processes BATCH_SIZE rows at a time
    # ----------------------------------------------------------------

    def _execute_anonymize_batched(
        self,
        *,
        category_info: dict,
        cutoff: datetime,
        tenant_id: str | None,
    ) -> int:
        """Replace PII columns with [redacted] in batches."""
        model = category_info["model"]
        date_col = getattr(model, category_info["date_col"])
        anon_cols = category_info["anon_cols"]
        has_tenant_id = category_info["has_tenant_id"]
        total_affected = 0

        # For anonymization, we need to exclude already-anonymized rows
        # to avoid counting them again on subsequent batches.
        # Check the first anon column — if it's already [redacted], skip.
        first_anon_col_name = next(iter(anon_cols))
        first_anon_col = getattr(model, first_anon_col_name)

        while True:
            id_col = model.id
            stmt = (
                select(id_col)
                .where(date_col < cutoff)
                .where(first_anon_col != "[redacted]")
                .limit(BATCH_SIZE)
            )

            if tenant_id and has_tenant_id:
                stmt = stmt.where(model.tenant_id == tenant_id)
            elif tenant_id and not has_tenant_id:
                stmt = self._apply_message_tenant_filter(stmt, tenant_id, cutoff)

            batch_ids = list(self.db.scalars(stmt).all())
            if not batch_ids:
                break

            upd_stmt = (
                update(model)
                .where(id_col.in_(batch_ids))
                .values(**anon_cols)
            )
            result = self.db.execute(upd_stmt)
            self.db.commit()
            total_affected += result.rowcount

            if len(batch_ids) < BATCH_SIZE:
                break

        return total_affected

    # ----------------------------------------------------------------
    # Message tenant scoping (messages don't have tenant_id directly)
    # ----------------------------------------------------------------

    def _apply_message_tenant_filter(self, stmt, tenant_id: str, cutoff):
        """
        Messages don't have tenant_id. We find matching session IDs
        for the tenant, then filter messages by those session IDs.
        """
        # Get session IDs belonging to this tenant
        session_ids_stmt = (
            select(SessionModel.id)
            .where(SessionModel.tenant_id == tenant_id)
        )
        session_ids = list(self.db.scalars(session_ids_stmt).all())

        if not session_ids:
            # Return the original stmt with an impossible condition
            return stmt.where(MessageModel.id < 0)

        return stmt.where(MessageModel.session_id.in_(session_ids))

    # ----------------------------------------------------------------
    # Audit log
    # ----------------------------------------------------------------

    def _log_action(
        self,
        *,
        tenant_id: str | None,
        data_category: str,
        action_taken: str,
        records_affected: int,
        retention_days_applied: int,
        triggered_by: str,
        reason: str | None = None,
    ) -> None:
        log = DeletionLog(
            tenant_id=tenant_id,
            data_category=data_category,
            action_taken=action_taken,
            records_affected=records_affected,
            retention_days_applied=retention_days_applied,
            triggered_by=triggered_by,
            reason=reason,
        )
        self.db.add(log)
        self.db.commit()
