"""BYO webhook endpoint ORM — Arc 12 WU6.

Mirrors ``byo_webhook_endpoints`` created at
``alembic/versions/arc12_wu6_byo_webhook_and_tool_execution_log.py``.

A row is the admin-registered, per-instance configuration for the
``bring_your_own_webhook`` tool: the outbound URL, the input/output
JSON schemas the subprocess sandbox validates against, and the
egress allowlist of FQDNs the subprocess is permitted to reach.

Wall-1 is enforced by RLS on ``admin_id``; Wall-3 is the
``instance_id`` scope.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ByoWebhookEndpoint(Base):
    __tablename__ = "byo_webhook_endpoints"

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
    endpoint_url: Mapped[str] = mapped_column(
        String(2048), nullable=False
    )
    input_schema: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=False,
    )
    output_schema: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=False,
    )
    allowed_domains: Mapped[list] = mapped_column(
        ARRAY(String(255)).with_variant(JSON(), "sqlite"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        server_onupdate=text("now()"),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ByoWebhookEndpoint id={self.id} admin={self.admin_id} "
            f"instance={self.instance_id} url={self.endpoint_url!r} "
            f"revoked={self.revoked_at is not None}>"
        )
