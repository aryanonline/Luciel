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

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, String, Text, Integer
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

    # ---- Step 25b: knowledge ingestion chunking config (defaults) ----
    chunk_size: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="500"
    )
    """Default chunk size in tokens for knowledge ingestion. Inherited by
    domains/instances unless overridden."""

    chunk_overlap: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="50"
    )
    """Default chunk overlap in tokens for knowledge ingestion."""

    chunk_strategy: Mapped[str] = mapped_column(
        String(length=20), nullable=False, server_default="paragraph"
    )
    """Default chunking strategy: 'paragraph' | 'sentence' | 'fixed' | 'semantic'."""

    # Whether this tenant is currently active.
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Step 30a.2 — deactivation timestamp for retention worker.
    # Stamped by admin_service.deactivate_tenant_with_cascade in the
    # same UPDATE that flips active=false. Read by the nightly Celery
    # beat job (app.worker.tasks.retention.run_retention_purge) to
    # compute the 90-day hard-purge cutoff. NULL on rows that have
    # never been deactivated (the vast majority); NULL also on rows
    # deactivated before Step 30a.2 — those tenants are intentionally
    # excluded from automated purge until next deactivation. See
    # ARCHITECTURE §3.2.13 (cascade extension) and the Step 30a.2
    # design plan §2.
    deactivated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # --- Audit fields ---
    # Who created this config. Could be an admin user ID or "system".
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Who last updated this config.
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)