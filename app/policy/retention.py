"""
Data retention service.

Enforces PIPEDA-compliant data lifecycle management.
Lives in app/policy/ because retention is a governance concern.

Step 27a fix: tenant_scope is now declarative per category. Pre-27a,
_enforce_single only applied a tenant predicate when
`config.get("tenant_via_session")` was truthy, which meant that
`memory_items`, `traces`, `knowledge_embeddings`, and `messages` all
silently cross-contaminated on tenant-scoped purges (manual AND
scheduled). A tenant requesting their own data be purged would
destroy other tenants' data in the same category — a PIPEDA
Article 4.5 (Limiting Retention) violation surface.

Fix: each category declares an explicit tenant_scope strategy:
  - ("direct", "l>")
        WHERE l> = :tenant_id
  - ("via_fk", "<fk_col>", "<ref_table>", "<ref_tenant_col>")
        WHERE <fk_col> IN (SELECT id FROM <ref_table> WHERE <ref_tenant_col> = :tenant_id)

Strategy is enforced for every effective_tenant; no silent fallthrough.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.retention import DeletionLog, RetentionPolicy
from app.models.session import SessionModel  # noqa: F401 — kept for migration compat
from app.models.message import MessageModel  # noqa: F401
from app.models.memory import MemoryItem  # noqa: F401
from app.models.trace import Trace  # noqa: F401
from app.models.knowledge import KnowledgeEmbedding  # noqa: F401
from app.repositories.retention_repository import RetentionRepository

logger = logging.getLogger(__name__)


# Step 27a: declarative tenant-scope strategy per category.
# Every category that is ever purged with a tenant scope MUST declare a strategy.
# Missing strategy + effective_tenant -> _enforce_single raises loudly (Invariant 5).
DATA_CATEGORY_MAP: dict[str, dict] = {
    "sessions": {
        "table": "sessions",
        "date_col": "created_at",
        "anon_cols": {"user_id": "anon"},
        "tenant_scope": ("direct", "tenant_id"),
    },
    "messages": {
        "table": "messages",
        "date_col": "created_at",
        "anon_cols": {"content": "[redacted]"},
        # messages has no tenant_id column; scope via its session FK.
        "tenant_scope": ("via_fk", "session_id", "sessions", "tenant_id"),
    },
    "memory_items": {
        "table": "memory_items",
        "date_col": "created_at",
        "anon_cols": {"user_id": "anon", "content": "[redacted]"},
        "tenant_scope": ("direct", "tenant_id"),
    },
    "traces": {
        "table": "traces",
        "date_col": "created_at",
        "anon_cols": {
            "user_id": "anon",
            "user_message": "[redacted]",
            "assistant_reply": "[redacted]",
        },
        "tenant_scope": ("direct", "tenant_id"),
    },
    "knowledge_embeddings": {
        "table": "knowledge_embeddings",
        "date_col": "created_at",
        "anon_cols": {},
        "tenant_scope": ("direct", "tenant_id"),
    },
}

VALID_CATEGORIES: set[str] = set(DATA_CATEGORY_MAP.keys())
VALID_ACTIONS: set[str] = {"delete", "anonymize"}


def _build_tenant_predicate(
    strategy: tuple, effective_tenant: str
) -> tuple[str, dict]:
    """
    Step 27a: translate a declarative tenant_scope strategy into a SQL
    predicate fragment + param dict. Raises ValueError on unknown strategy.
    """
    kind = strategy[0]
    if kind == "direct":
        col = strategy[1]
        return f"{col} = :tenant_id", {"tenant_id": effective_tenant}
    if kind == "via_fk":
        _, fk_col, ref_table, ref_tenant_col = strategy
        return (
            f"{fk_col} IN (SELECT id FROM {ref_table} "
            f"WHERE {ref_tenant_col} = :tenant_id)",
            {"tenant_id": effective_tenant},
        )
    raise ValueError(f"Unknown tenant_scope strategy kind: {kind!r}")


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

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_str = cutoff.isoformat()

        # Build WHERE clause: date floor is unconditional.
        conditions = [f"{date_col} < :cutoff"]
        params: dict = {"cutoff": cutoff}

        # Step 27a: tenant predicate is MANDATORY whenever an effective_tenant
        # exists. If a category has no declared strategy, we raise rather than
        # silently fall through — Invariant 5 (scope arithmetic only) forbids
        # a tenant-scoped purge that ignores the tenant.
        effective_tenant = scope_tenant_id or policy.tenant_id
        if effective_tenant:
            strategy = config.get("tenant_scope")
            if strategy is None:
                raise ValueError(
                    f"Category {category!r} has no tenant_scope strategy; "
                    f"refusing tenant-scoped purge (Invariant 5)."
                )
            predicate, predicate_params = _build_tenant_predicate(
                strategy, effective_tenant
            )
            conditions.append(predicate)
            params.update(predicate_params)

        where = " AND ".join(conditions)

        # Execute action
        if action == "delete":
            sql = f"DELETE FROM {table} WHERE {where}"
            result = self.db.execute(text(sql), params)
            rows = result.rowcount

        elif action == "anonymize":
            anon_cols = config["anon_cols"]
            if not anon_cols:
                # No anon columns declared → anonymize degenerates to delete
                # (e.g. knowledge_embeddings: no PII surface to redact, row
                # is the data, so we hard-delete expired rows).
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