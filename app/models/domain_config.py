"""
Domain configuration model.

Stores role-specific settings for a tenant+domain combination.
Each row represents one child Luciel configuration.

Audit fields (created_by, updated_by) track who made changes.
Full change history (audit_logs table) will be added later
for enterprise-grade auditability.
"""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, String, Text, UniqueConstraint, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class DomainConfig(Base, TimestampMixin):
    __tablename__ = "domain_configs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Which tenant this config belongs to.
    tenant_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)

    # Which domain/role this config is for.
    domain_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)

    # Human-readable name for this role.
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Description of what this child Luciel does.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Role-specific system prompt additions.
    system_prompt_additions: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- Step 25b: optional chunking config overrides (NULL = inherit from tenant) ----
    chunk_size: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    """Override of TenantConfig.chunk_size for this domain. NULL = inherit."""

    chunk_overlap: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    """Override of TenantConfig.chunk_overlap for this domain. NULL = inherit."""

    chunk_strategy: Mapped[str | None] = mapped_column(
        String(length=20), nullable=True
    )
    """Override of TenantConfig.chunk_strategy for this domain. NULL = inherit."""

    # Which tools this role is allowed to use.
    allowed_tools: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Escalation contact specific to this role.
    escalation_contact: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Role-specific policy overrides.
    policy_overrides: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Preferred LLM provider for this role.
    preferred_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Whether this domain config is currently active.
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # --- Audit fields ---
    # Who created this config.
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Who last updated this config.
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Ensure one config per tenant+domain combination.
    __table_args__ = (
        UniqueConstraint("tenant_id", "domain_id", name="uq_tenant_domain"),
    )