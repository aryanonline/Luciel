"""
Trace model.

Stores a structured record of what happened during each chat turn.
This is Luciel's observability layer — every request produces a trace
so you can debug, audit, and improve the system over time.

Includes tenant/domain/agent config references so you can see which
child Luciel configuration was active for each request.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Trace(Base, TimestampMixin):
    __tablename__ = "traces"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # --- Request context ---
    trace_id: Mapped[str] = mapped_column(
        String(100), unique=True, index=True, nullable=False
    )
    """Unique trace identifier for this request."""

    session_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(100), nullable=False)
    domain_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """Agent ID for per-agent audit trail."""

    # --- Input/Output ---
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    assistant_reply: Mapped[str] = mapped_column(Text, nullable=False)

    # --- LLM details ---
    llm_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # --- Memory ---
    memories_retrieved: Mapped[int] = mapped_column(default=0, nullable=False)
    memories_used: Mapped[list | None] = mapped_column(JSON, nullable=True)
    memories_extracted: Mapped[int] = mapped_column(default=0, nullable=False)

    # --- Tools ---
    tool_called: Mapped[bool] = mapped_column(default=False, nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # --- Policy ---
    escalated: Mapped[bool] = mapped_column(default=False, nullable=False)
    policy_flags: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # --- Config references ---
    tenant_config_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    domain_config_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_config_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Which agent config was active. Enables per-agent audit."""

    # Step 24.5 File 15 — LucielInstance that served this chat turn.
    # NULL = legacy/unbound (chat resolved via tenant/domain/agent config path).
    luciel_instance_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "luciel_instances.id",
            ondelete="SET NULL",
            name="fk_traces_luciel_instance_id",
        ),
        nullable=True,
        index=True,
    )