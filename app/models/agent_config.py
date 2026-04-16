"""
Agent configuration model.

Stores agent-specific settings within a tenant.
This is the third level of the hierarchy:
  Domain → Tenant → Agent

Each agent can have their own Luciel with a custom name,
persona additions, and scoped data access.
"""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AgentConfig(Base, TimestampMixin):
    __tablename__ = "agent_configs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    tenant_id: Mapped[str] = mapped_column(
        String(100), index=True, nullable=False
    )
    """Which tenant this agent belongs to."""

    agent_id: Mapped[str] = mapped_column(
        String(100), index=True, nullable=False
    )
    """Unique identifier for this agent within the tenant."""

    display_name: Mapped[str] = mapped_column(
        String(200), nullable=False
    )
    """The name this child Luciel presents itself as (e.g. 'Luna', 'Sarah's Assistant')."""

    description: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    """Description of this agent."""

    system_prompt_additions: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    """Agent-specific system prompt additions."""

    escalation_contact: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    """Escalation contact specific to this agent."""

    allowed_domains: Mapped[list | None] = mapped_column(
        JSON, nullable=True
    )
    """Which domains this agent can operate in."""

    policy_overrides: Mapped[dict | None] = mapped_column(
        JSON, nullable=True
    )
    """Agent-specific policy overrides."""

    preferred_provider: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    """Preferred LLM provider for this agent."""

    active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    """Whether this agent config is currently active."""

    created_by: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )

    updated_by: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "agent_id", name="uq_tenant_agent"),
    )   