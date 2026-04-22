"""
API Key model.

Stores hashed API keys mapped to tenant, domain, and agent configurations.

The raw key is only shown once at creation time — we store a hash
so that even if the database is compromised, keys cannot be recovered.

Each key is scoped to a tenant and optionally to a domain and agent.
  - If domain_id is set, the key can only create sessions for that domain.
  - If domain_id is null, the key works for any domain within the tenant's allowed_domains.
  - If agent_id is set, the key is scoped to a specific agent within the tenant.
  - If agent_id is null, the key works at the tenant level (no agent scoping).
"""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from sqlalchemy import Integer, ForeignKey


class ApiKey(Base, TimestampMixin):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    key_hash: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    """The hashed key. Never store raw keys."""

    key_prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    """First few characters of the raw key, safe to display."""

    tenant_id: Mapped[str | None] = mapped_column(
        String(100), index=True, nullable=True
    )
    """Which tenant this key belongs to. NULL for platform-admin keys (cross-tenant bypass via 'platformadmin' permission per Invariant 5)."""

    domain_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """If null, the key works for any domain in the tenant's allowed_domains."""

    agent_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """If null, the key works at the tenant level (no agent scoping)."""

    # Step 24.5 — optional pin to a specific LucielInstance.
    # When set, this key can only talk to that one Luciel. When NULL,
    # chat resolution falls back to the tenant/domain/agent config path.
    luciel_instance_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "luciel_instances.id",
            ondelete="SET NULL",
            name="fk_api_keys_luciel_instance_id",
        ),
        nullable=True,
        index=True,
    )

    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    """Human-readable label for this key."""

    permissions: Mapped[list] = mapped_column(JSON, nullable=False)
    """e.g., ["chat", "sessions"] or ["chat", "sessions", "admin"]"""

    rate_limit: Mapped[int] = mapped_column(default=1000, nullable=False)
    """Maximum requests per day. 0 = unlimited."""

    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    """Whether this key is currently active."""

    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """Audit field — who created this key."""