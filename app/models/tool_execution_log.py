"""Tool execution log ORM — Arc 12 WU6 (general-purpose for WU5 reuse).

Mirrors ``tool_execution_log`` created at
``alembic/versions/arc12_wu6_byo_webhook_and_tool_execution_log.py``.

One row per tool dispatch. The schema is intentionally tool-agnostic
(``tool_id`` is a free string) so the WU5 sibling-runtime audit and
the WU6 BYO subprocess audit can both write here.

Columns:

* ``execution_mode`` — ``"in_process"`` | ``"subprocess"``.
* ``input_hash`` / ``output_hash`` — SHA-256 hex of canonicalised
  payloads (no PII).
* ``latency_ms`` — dispatch wall-clock.
* ``error_class`` — short code; NULL on success.
* ``circuit_breaker_state`` — ``closed`` | ``half_open`` | ``open``
  at the moment dispatch was attempted; NULL for tools without a
  breaker (most catalog tools).
* ``error_message`` — short human-readable description; optional.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


# Public taxonomy — keep these strings stable; audit log readers
# branch on them.
ERROR_CLASS_TRANSPORT = "transport"
ERROR_CLASS_TIMEOUT = "timeout"
ERROR_CLASS_SCHEMA_INPUT = "schema_input"
ERROR_CLASS_SCHEMA_OUTPUT = "schema_output"
ERROR_CLASS_CIRCUIT_OPEN = "circuit_open"
ERROR_CLASS_EGRESS_DENIED = "egress_denied"
ERROR_CLASS_HTTP_ERROR = "http_error"
ERROR_CLASS_OTHER = "other"

CB_STATE_CLOSED = "closed"
CB_STATE_HALF_OPEN = "half_open"
CB_STATE_OPEN = "open"


class ToolExecutionLog(Base):
    __tablename__ = "tool_execution_log"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    admin_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    instance_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("instances.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    tool_id: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_mode: Mapped[str] = mapped_column(
        String(20), nullable=False
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    error_class: Mapped[str | None] = mapped_column(
        String(40), nullable=True
    )
    circuit_breaker_state: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        index=True,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ToolExecutionLog id={self.id} admin={self.admin_id} "
            f"instance={self.instance_id} tool={self.tool_id} "
            f"mode={self.execution_mode} latency={self.latency_ms}ms "
            f"err={self.error_class!r} cb={self.circuit_breaker_state!r}>"
        )


__all__ = [
    "ToolExecutionLog",
    "ERROR_CLASS_TRANSPORT",
    "ERROR_CLASS_TIMEOUT",
    "ERROR_CLASS_SCHEMA_INPUT",
    "ERROR_CLASS_SCHEMA_OUTPUT",
    "ERROR_CLASS_CIRCUIT_OPEN",
    "ERROR_CLASS_EGRESS_DENIED",
    "ERROR_CLASS_HTTP_ERROR",
    "ERROR_CLASS_OTHER",
    "CB_STATE_CLOSED",
    "CB_STATE_HALF_OPEN",
    "CB_STATE_OPEN",
]
