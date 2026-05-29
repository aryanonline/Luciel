"""ToolExecutionLog repository — Arc 12 WU6 (general-purpose).

One row per tool dispatch. WU6 BYO writes here per invocation; WU5
sibling-runtime writes here too (tool_id='call_sibling_luciel').
The repository surface is intentionally minimal — write + scoped
list — because the table is append-only audit.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models.tool_execution_log import ToolExecutionLog

logger = logging.getLogger(__name__)


class ToolExecutionLogRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def record(
        self,
        *,
        admin_id: str,
        instance_id: int,
        tool_id: str,
        execution_mode: str,
        input_hash: str,
        output_hash: Optional[str],
        latency_ms: int,
        error_class: Optional[str],
        circuit_breaker_state: Optional[str],
        error_message: Optional[str] = None,
        autocommit: bool = True,
    ) -> ToolExecutionLog:
        # Defensive truncation — the column caps at 500 chars and the
        # caller may pass an unbounded exception string.
        if error_message is not None and len(error_message) > 500:
            error_message = error_message[:497] + "..."

        row = ToolExecutionLog(
            admin_id=admin_id,
            instance_id=instance_id,
            tool_id=tool_id,
            execution_mode=execution_mode,
            input_hash=input_hash,
            output_hash=output_hash,
            latency_ms=latency_ms,
            error_class=error_class,
            circuit_breaker_state=circuit_breaker_state,
            error_message=error_message,
        )
        self.db.add(row)
        try:
            if autocommit:
                self.db.commit()
                self.db.refresh(row)
            else:
                self.db.flush()
        except Exception:  # noqa: BLE001
            # Audit logging MUST NOT take down the call path. Roll back
            # and continue — the tool result is already determined.
            logger.exception(
                "ToolExecutionLog write failed (tool=%s admin=%s "
                "instance=%s) — continuing.",
                tool_id, admin_id, instance_id,
            )
            try:
                self.db.rollback()
            except Exception:  # noqa: BLE001  # pragma: no cover
                pass
        return row

    def list_for_instance(
        self,
        *,
        admin_id: str,
        instance_id: int,
        tool_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[ToolExecutionLog]:
        conditions = [
            ToolExecutionLog.admin_id == admin_id,
            ToolExecutionLog.instance_id == instance_id,
        ]
        if tool_id is not None:
            conditions.append(ToolExecutionLog.tool_id == tool_id)
        stmt = (
            select(ToolExecutionLog)
            .where(and_(*conditions))
            .order_by(ToolExecutionLog.created_at.desc())
            .limit(limit)
        )
        return list(self.db.execute(stmt).scalars())
