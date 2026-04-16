"""
Tenant configuration model.

Stores tenant-wide settings that apply across all domains
for a given tenant. This is the structured config layer —
the vector DB holds knowledge, this table holds settings.

Audit fields (created_by, updated_by) track who made changes.
Full change history (audit_logs table) will be added later
for enterprise-grade auditability.
"""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class TenantConfig(Base, TimestampMixin):
    __tablename__ = "tenant_configs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Unique tenant identifier. Must match tenant_id used in sessions.
    tenant_id: Mapped[str] = mapped_column(
        String(100), unique=True, index=True, nullable=False
    )

    # Human-readable tenant name for dashboards and logs.
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Brief description of the tenant's business.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Default escalation contact for this tenant.
    escalation_contact: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # List of domain_ids this tenant is allowed to use.
    allowed_domains: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Tenant-wide system prompt additions.
    system_prompt_additions: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Whether this tenant is currently active.
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # --- Audit fields ---
    # Who created this config. Could be an admin user ID or "system".
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Who last updated this config.
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)