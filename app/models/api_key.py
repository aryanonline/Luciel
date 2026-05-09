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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
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
    """Which tenant this key belongs to. NULL for platform-admin keys (cross-tenant bypass via 'platform_admin' permission per Invariant 5; canonical constant defined as PLATFORM_ADMIN in app/policy/scope.py)."""

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

    # Step 30b — chat widget embed keys.
    # See alembic/versions/a7c1f4e92b85_step30b_api_keys_widget_columns.py
    # for the full rationale on why these live on api_keys rather than
    # a separate embed_keys table. The four columns together gate the
    # public widget surface: key_kind discriminates admin vs embed,
    # allowed_origins enforces the Origin header allowlist, the per-
    # minute cap is the burst guard distinct from the per-day rate
    # limit, and widget_config holds the three branding knobs that
    # are safe to render without inviting XSS on customer sites.

    key_kind: Mapped[str] = mapped_column(
        String(20), nullable=False, default="admin", server_default="admin"
    )
    """Credential class. 'admin' = server-to-server (existing keys; full
    permissions array honored; no origin check; per-day rate_limit only).
    'embed' = public widget key (must have exactly ['chat'] permissions,
    non-empty allowed_origins, and rate_limit_per_minute set)."""

    allowed_origins: Mapped[list[str] | None] = mapped_column(
        ARRAY(String), nullable=True
    )
    """Origin allowlist for embed keys (scheme + host + port match).
    NULL on admin keys; required non-empty on embed keys."""

    rate_limit_per_minute: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    """Per-minute burst cap enforced before SSE stream opens. The existing
    rate_limit column (per-day) still applies. NULL on admin keys;
    required on embed keys."""

    widget_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    """Three-knob branding payload at v1: accent_color (7-char hex),
    greeting_message (length-capped plaintext), display_name (length-
    capped plaintext). No logo, no font, no free-form CSS — those
    surfaces invite XSS we cannot QA on customer sites."""