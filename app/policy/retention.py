"""
Data retention service.

Enforces PIPEDA-compliant data lifecycle management.
Lives in app/policy/ because retention is a governance concern.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.retention import DeletionLog, RetentionPolicy
from app.models.session import SessionModel
from app.models.message import MessageModel
from app.models.memory import MemoryItem
from app.models.trace import Trace
from app.models.knowledge import KnowledgeEmbedding
from app.repositories.retention_repository import RetentionRepository

logger = logging.getLogger(__name__)

DATA_CATEGORY_MAP = {
    "sessions": {
        "table": "sessions",
        "date_col": "created_at",
        "anon_cols": {"user_id": "anon"},
        "has_tenant": True,
    },
    "messages": {
        "table": "messages",
        "date_col": "created_at",
        "anon_cols": {"content": "[redacted]"},
        "has_tenant": False,
    },
    "memory_items": {
        "table": "memory_items",
        "date_col": "created_at",
        "anon_cols": {"user_id": "anon", "content": "[redacted]"},
        "has_tenant": True,
    },
    "traces": {
        "table": "traces",
        "date_col": "created_at",
        "anon_cols": {
            "user_id": "anon",
            "user_message": "[redacted]",
            "assistant_reply": "[redacted]",
        },
        "has_tenant": True,
    },
    "knowledge_embeddings": {
        "table": "knowledge_embeddings",
        "date_col": "created_at",
        "anon_cols": {},
        "has_tenant": True,
    },
}

VALID_CATEGORIES = set(DATA_CATEGORY_MAP.keys())
VALID_ACTIONS = {"delete", "anonymize"}


class RetentionService:

    def __init__(self, db: Session, repository: RetentionRepository) -> None:
        self.db = db
        self.repository = repository

    def enforce_all_policies(
        self, *, triggered_by: str = "scheduler",
    ) -> list[dict]:
        policies = self.repository.list_policies()
        results = []
        for policy in policies:
            category = policy.data_category
            policy_id = policy.id
            try:
                result = self._enforce_single(
                    policy=policy, triggered_by=triggered_by,
                )
                results.append(result)
            except Exception as exc:
                self.db.rollback()
                logger.error("Policy %d (%s) failed: %s", policy_id, category, exc)
                results.append({
                    "policy_id": policy_id,
                    "data_category": category,
                    "action": "error",
                    "rows_affected": 0,
                    "error": str(exc),
                })
        return results

    def enforce_for_tenant(
        self, *, tenant_id: str, triggered_by: str = "scheduler",
    ) -> list[dict]:
        results = []
        for category in VALID_CATEGORIES:
            policy = self.repository.get_policy_for_category(
                data_category=category, tenant_id=tenant_id,
            )
            if policy and policy.retention_days > 0:
                try:
                    result = self._enforce_single(
                        policy=policy,
                        triggered_by=triggered_by,
                        scope_tenant_id=tenant_id,
                    )
                    results.append(result)
                except Exception as exc:
                    logger.error(
                        "Tenant %s category %s failed: %s",
                        tenant_id, category, exc,
                    )
        return results

    def manual_purge(
        self,
        *,
        data_category: str,
        tenant_id: str | None = None,
        reason: str,
        triggered_by: str,
    ) -> dict:
        if data_category not in VALID_CATEGORIES:
            raise ValueError(f"Invalid category: {data_category}")

        policy = self.repository.get_policy_for_category(
            data_category=data_category, tenant_id=tenant_id,
        )
        if not policy:
            raise ValueError(f"No active policy for {data_category}")

        return self._enforce_single(
            policy=policy,
            triggered_by=triggered_by,
            scope_tenant_id=tenant_id,
            reason=reason,
        )

    def _enforce_single(
        self,
        *,
        policy: RetentionPolicy,
        triggered_by: str,
        scope_tenant_id: str | None = None,
        reason: str | None = None,
    ) -> dict:
        category = policy.data_category
        action = policy.action
        days = policy.retention_days

        if days <= 0:
            return {
                "policy_id": policy.id,
                "data_category": category,
                "action": "skipped",
                "rows_affected": 0,
            }

        if category not in DATA_CATEGORY_MAP:
            raise ValueError(f"Unknown category: {category}")

        config = DATA_CATEGORY_MAP[category]
        table = config["table"]
        date_col = config["date_col"]
        has_tenant = config["has_tenant"]

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_str = cutoff.isoformat()

        # Build WHERE clause
        conditions = [f"{date_col} < :cutoff"]
        params: dict = {"cutoff": cutoff}

        effective_tenant = scope_tenant_id or policy.tenant_id
        if effective_tenant and has_tenant:
            conditions.append("tenant_id = :tenant_id")
            params["tenant_id"] = effective_tenant

        where = " AND ".join(conditions)

        # Execute action
        if action == "delete":
            sql = f"DELETE FROM {table} WHERE {where}"
            result = self.db.execute(text(sql), params)
            rows = result.rowcount

        elif action == "anonymize":
            anon_cols = config["anon_cols"]
            if not anon_cols:
                sql = f"DELETE FROM {table} WHERE {where}"
                result = self.db.execute(text(sql), params)
                rows = result.rowcount
            else:
                set_clauses = ", ".join(
                    f"{col} = :anon_{col}" for col in anon_cols
                )
                for col, val in anon_cols.items():
                    params[f"anon_{col}"] = val

                sql = f"UPDATE {table} SET {set_clauses} WHERE {where}"
                result = self.db.execute(text(sql), params)
                rows = result.rowcount
        else:
            raise ValueError(f"Unknown action: {action}")

        self.db.commit()

        # Log the action
        log = DeletionLog(
            tenant_id=effective_tenant,
            data_category=category,
            action_taken=action + "d",
            rows_affected=rows,
            cutoff_date=cutoff_str,
            triggered_by=triggered_by,
            reason=reason,
        )
        self.repository.log_deletion(log)

        logger.info(
            "Retention: %s %d rows from %s (cutoff=%s, tenant=%s)",
            action, rows, table, cutoff_str, effective_tenant,
        )

        return {
            "policy_id": policy.id,
            "data_category": category,
            "action": action,
            "rows_affected": rows,
            "cutoff_date": cutoff_str,
            "tenant_id": effective_tenant,
        }