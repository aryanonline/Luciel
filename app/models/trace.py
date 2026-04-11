"""
Trace model.

Stores a structured record of what happened during each chat turn.
This is Luciel's observability layer — every request produces a trace
so you can debug, audit, and improve the system over time.

Each trace is linked to a session and captures the full decision path:
what was retrieved, what was called, what was flagged, and what was returned.
"""

from __future__ import annotations

from sqlalchemy import JSON, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Trace(Base, TimestampMixin):
    __tablename__ = "traces"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Unique trace identifier for this request.
    trace_id: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)

    # Which session and user this trace belongs to.
    session_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(100), nullable=False)

    # The user's input message.
    user_message: Mapped[str] = mapped_column(Text, nullable=False)

    # The final reply sent to the user.
    assistant_reply: Mapped[str] = mapped_column(Text, nullable=False)

    # Which LLM provider and model handled the request.
    llm_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # How many memories were retrieved and injected.
    memories_retrieved: Mapped[int] = mapped_column(default=0, nullable=False)

    # Tool call details.
    tool_called: Mapped[bool] = mapped_column(default=False, nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Policy decisions.
    escalated: Mapped[bool] = mapped_column(default=False, nullable=False)
    policy_flags: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # How many new memories were extracted after this turn.
    memories_extracted: Mapped[int] = mapped_column(default=0, nullable=False)