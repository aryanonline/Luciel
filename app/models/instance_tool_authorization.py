"""InstanceToolAuthorization ORM — Arc 12 WU2.

Mirrors the ``instance_tool_authorizations`` table created at
``alembic/versions/arc12_wu2_instance_tool_authorizations.py``.

The model is the load-bearing row for the broker's default-deny
gate: a tool dispatch on a given ``(admin_id, instance_id, tool_id)``
proceeds only if a row exists with ``revoked_at IS NULL`` AND
``enabled=True``. Absent row, revoked row, or disabled row ⇒ refuse.

Walls:
* Wall-1 (admin) — ``admin_id`` carries the tenant boundary; RLS
  fences on it.
* Wall-3 (instance) — ``instance_id`` scopes the row to a single
  Instance so a tool authorised on Instance A is not authorised on
  Instance B.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

if TYPE_CHECKING:  # pragma: no cover
    pass


class InstanceToolAuthorization(Base):
    __tablename__ = "instance_tool_authorizations"

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
    tool_id: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    authorized_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
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

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<InstanceToolAuthorization id={self.id} "
            f"admin={self.admin_id} instance={self.instance_id} "
            f"tool={self.tool_id} enabled={self.enabled} "
            f"revoked={self.revoked_at is not None}>"
        )
