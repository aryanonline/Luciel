"""InstanceConnection ORM — Arc 15 WU4 (Arc 17 connection-contract slice).

Mirrors the ``instance_connections`` table created at
``alembic/versions/arc15_b_instance_connections.py`` (Architecture
§3.8.2).

A row records WHICH external system an instance is wired to and the
live status of that wiring. WU5's dispatch gate reads it: a tool with
``requires_connection`` set may only fire when a live row exists with
``status == 'connected'``.

Walls:
* Wall-1 (admin) — ``admin_id`` carries the tenant boundary; RLS fences
  on it.
* Wall-3 (instance) — ``instance_id`` scopes the row to a single
  Instance.

Honesty invariants (Architecture §3.8.2):
* ``config_json`` holds NON-SECRET config ONLY (CSV column map, webhook
  URL). Secrets NEVER land here — they ride behind ``credential_ref``.
* ``status == 'connected'`` is only ever written for a connection with
  a real backing (CSV / webhook in this slice). Deferred connectors
  (calendar / crm) land as ``unconfigured`` — never a fake ``connected``.

Domain-agnostic naming (Locked Decision #5): the connector category is
``record_source`` (admin CSV upload / generic record provider), NOT a
vertical-specific ``property_source``. ``last_health_check_at`` is the
timestamp of the last successful health check / verification.
"""
from __future__ import annotations

from datetime import datetime

import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Closed vocabularies — mirror the PG enum types created in the
# migration. ``create_type=False`` because the migration owns DDL.
CONNECTION_TYPES = (
    "calendar",
    "email_sender",
    "sms_sender",
    "crm",
    "record_source",
    "outbound_webhook",
)
CONNECTION_STATUSES = (
    "unconfigured",
    "connected",
    "error",
    "expired",
    # rescand_connections_schema additions (§3.8.4):
    "revoked",   # explicit revoke; broker skips; revoked_at IS NOT NULL
    "dormant",   # Pro→Free downgrade preserve; restore on re-upgrade
)

_conn_type_enum = ENUM(
    *CONNECTION_TYPES, name="connection_type", create_type=False
)
_conn_status_enum = ENUM(
    *CONNECTION_STATUSES, name="connection_status", create_type=False
)


class InstanceConnection(Base):
    __tablename__ = "instance_connections"

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
    connection_type: Mapped[str] = mapped_column(
        _conn_type_enum, nullable=False
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    config_json: Mapped[dict | None] = mapped_column(JSONB(), nullable=True)
    credential_ref: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    status: Mapped[str] = mapped_column(
        _conn_status_enum,
        nullable=False,
        server_default="unconfigured",
    )
    last_health_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
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
    # rescand_connections_schema additions (§3.8.2):
    status_detail: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment=(
            "Human-readable detail for the current status. Written by the "
            "health-check worker on expired path (CJ §7 Reconnect chip) "
            "and by the dormant path on downgrade. NULL for connected/ "
            "unconfigured rows."
        ),
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "The team member (User) who configured this connection. NULL "
            "for connections created before this column was added or "
            "created by system processes."
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<InstanceConnection id={self.id} admin={self.admin_id} "
            f"instance={self.instance_id} type={self.connection_type} "
            f"provider={self.provider} status={self.status} "
            f"revoked={self.revoked_at is not None}>"
        )
