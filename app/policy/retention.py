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
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
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
                # Step 28 Phase 2 Commit 8: NO db.rollback() here.
                # _enforce_single now commits per batch and writes its
                # own DeletionLog (auto-committing) before re-raising on
                # partial failure. A rollback at this layer would only
                # discard uncommitted work that doesn't exist — and if a
                # future change ever stages pre-commit state in this
                # session, a rollback could silently undo the
                # partial-failure audit trail. Loop continues to the
                # next policy; the failure is captured below.
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
        """Apply a single retention policy in batched chunks.

        Step 28 Phase 2 Commit 8 — the actual DELETE/UPDATE is done
        in bounded batches via _batched_delete / _batched_anonymize
        rather than one unbounded statement. See app.core.config for
        the batching knobs (retention_batch_size,
        retention_batch_sleep_seconds, retention_max_batches_per_run).

        Public return shape is preserved — callers get the same
        {policy_id, data_category, action, rows_affected,
         cutoff_date, tenant_id} dict as before. rows_affected is now
        the SUM across all batches.

        Failure semantics: if a batch raises, batches that already
        committed are durable. We still write a DeletionLog row
        capturing the partial total before re-raising, so the audit
        trail reflects what actually happened. Pre-Commit-8 behavior
        was atomic-or-nothing; post-Commit-8 it's batched-with-audit.
        For tenant-scope purges this is a strict improvement — a
        partial purge is closer to the PIPEDA goal than a full
        rollback when the user requested deletion.
        """
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

        # ----- Batched execution -----
        rows = 0
        partial_failure: Exception | None = None
        try:
            if action == "delete":
                rows = self._batched_delete(
                    table=table, where=where, params=params,
                )
            elif action == "anonymize":
                anon_cols = config["anon_cols"]
                if not anon_cols:
                    # No anon columns declared → anonymize degenerates
                    # to delete (e.g. knowledge_embeddings: no PII
                    # surface to redact, row is the data, so we
                    # hard-delete expired rows).
                    rows = self._batched_delete(
                        table=table, where=where, params=params,
                    )
                else:
                    rows = self._batched_anonymize(
                        table=table, where=where, params=params,
                        anon_cols=anon_cols,
                    )
            else:
                raise ValueError(f"Unknown action: {action}")
        except Exception as exc:
            # Batches that already committed are durable. Capture so
            # the audit log row records the partial total, then
            # re-raise after logging.
            partial_failure = exc

        # ----- Audit log (always, even on partial failure) -----
        # Run in its own transaction so a failure here doesn't mask
        # the partial-failure exception path.
        try:
            log = DeletionLog(
                tenant_id=effective_tenant,
                data_category=category,
                action_taken=action + "d",
                rows_affected=rows,
                cutoff_date=cutoff_str,
                triggered_by=triggered_by,
                reason=(
                    reason if partial_failure is None
                    else f"{reason or ''} | PARTIAL: "
                         f"{type(partial_failure).__name__}: "
                         f"{partial_failure}"[:500]
                ),
            )
            self.repository.log_deletion(log)
        except Exception as audit_exc:
            logger.error(
                "Retention: failed to write DeletionLog row for "
                "category=%s tenant=%s rows=%d: %s",
                category, effective_tenant, rows, audit_exc,
            )

        if partial_failure is not None:
            logger.error(
                "Retention: %s on %s for tenant=%s FAILED after %d rows: %s",
                action, table, effective_tenant, rows, partial_failure,
            )
            raise partial_failure

        logger.info(
            "Retention: %s %d rows from %s (cutoff=%s, tenant=%s, batched)",
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

    # ------------------------------------------------------------------
    # Batched executors (Step 28 Phase 2 Commit 8)
    # ------------------------------------------------------------------
    #
    # Both helpers commit AFTER each batch. FOR UPDATE SKIP LOCKED on
    # the inner SELECT lets the purge run alongside live chat traffic
    # without blocking writers — if a row is locked by an active
    # transaction (e.g. a chat handler reading messages), this run
    # skips it and picks it up on the next batch.
    #
    # PK column assumption: every retention table uses `id` as the
    # primary key (verified for sessions, messages, memory_items,
    # traces, knowledge_embeddings). New categories must follow this
    # convention; if not, the inner SELECT below will raise loudly at
    # first batch attempt rather than silently corrupt.

    def _batched_delete(
        self,
        *,
        table: str,
        where: str,
        params: dict,
    ) -> int:
        """DELETE WHERE <where> in chunks. Returns total rows deleted."""
        batch_size = settings.retention_batch_size
        max_batches = settings.retention_max_batches_per_run
        sleep_s = settings.retention_batch_sleep_seconds

        total = 0
        # Each batch needs the static params (cutoff, tenant_id, …)
        # plus the dynamic :batch_size. Build a fresh dict per batch
        # to avoid mutation surprises if the caller's params escape.
        sql = (
            f"DELETE FROM {table} "
            f"WHERE id IN ("
            f"  SELECT id FROM {table} "
            f"  WHERE {where} "
            f"  ORDER BY id "
            f"  LIMIT :batch_size "
            f"  FOR UPDATE SKIP LOCKED"
            f")"
        )
        for batch_num in range(max_batches):
            batch_params = dict(params)
            batch_params["batch_size"] = batch_size
            result = self.db.execute(text(sql), batch_params)
            affected = result.rowcount
            self.db.commit()
            total += affected
            if affected < batch_size:
                # Last batch: caught up to the date+tenant horizon.
                return total
            if sleep_s > 0:
                time.sleep(sleep_s)
        logger.warning(
            "Retention: %s _batched_delete hit max_batches=%d cap "
            "(total=%d). Either lower retention_days or raise the cap.",
            table, max_batches, total,
        )
        return total

    def _batched_anonymize(
        self,
        *,
        table: str,
        where: str,
        params: dict,
        anon_cols: dict,
    ) -> int:
        """UPDATE SET ...anon_vals WHERE <where> in chunks.

        Returns total rows anonymized. Mirrors _batched_delete's
        chunking pattern — inner SELECT FOR UPDATE SKIP LOCKED bounds
        each batch's lock footprint.
        """
        batch_size = settings.retention_batch_size
        max_batches = settings.retention_max_batches_per_run
        sleep_s = settings.retention_batch_sleep_seconds

        # Defense: empty anon_cols would produce a malformed SET clause.
        if not anon_cols:
            raise ValueError(
                "_batched_anonymize requires anon_cols; caller should "
                "have routed to _batched_delete instead."
            )

        set_clauses = ", ".join(
            f"{col} = :anon_{col}" for col in anon_cols
        )
        sql = (
            f"UPDATE {table} SET {set_clauses} "
            f"WHERE id IN ("
            f"  SELECT id FROM {table} "
            f"  WHERE {where} "
            f"  ORDER BY id "
            f"  LIMIT :batch_size "
            f"  FOR UPDATE SKIP LOCKED"
            f")"
        )

        total = 0
        for batch_num in range(max_batches):
            batch_params = dict(params)
            batch_params["batch_size"] = batch_size
            for col, val in anon_cols.items():
                batch_params[f"anon_{col}"] = val
            result = self.db.execute(text(sql), batch_params)
            affected = result.rowcount
            self.db.commit()
            total += affected
            if affected < batch_size:
                return total
            if sleep_s > 0:
                time.sleep(sleep_s)
        logger.warning(
            "Retention: %s _batched_anonymize hit max_batches=%d cap "
            "(total=%d). Either lower retention_days or raise the cap.",
            table, max_batches, total,
        )
        return total