"""Instance ORM model — V2 unit replacing LucielInstance + Agent (Arc 5 B1).

Mirrors the ``instances`` table created at Revision A
(``alembic/versions/arc5_a_admin_instance_additive.py``). Replaces the
legacy ``LucielInstance`` (``app/models/luciel_instance.py``) and the
legacy ``Agent`` (``app/models/agent.py``) — both deleted in the same B1
commit per the aggressive-cleanup amendment
(``docs/DRIFTS.md`` ``D-arc5-aggressive-cleanup-doctrine-amendment-2026-05-23``).

Schema anchors
--------------
* ``instances.id`` is INTEGER autoincrement mirroring legacy
  ``luciel_instances.id``.
* ``instances.admin_id`` is FK to ``admins.id`` (RESTRICT — soft-delete
  Admins, never hard-delete).
* ``instance_slug`` is unique within an Admin.
* Back-pointers ``legacy_luciel_instance_id`` and ``legacy_agent_id``
  were dropped at Revision C alongside the legacy tables (Arc 9 C15:
  ORM declarations removed to match production schema).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Instance(Base):
    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    admin_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    instance_slug: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Arc 6 Commit 8.5b — Deferred-downgrade overflow archive stamp.
    # Set when this Instance is one of the LRU losers at a downgrade
    # boundary (Pro→Free or Ent→Pro/Free). Pairs with active=false at
    # the same moment. Re-upgrade within the audit_retention window
    # (Free=30d) rehydrates rows that still carry this stamp.
    # NULL = not archived for downgrade reasons.
    pending_downgrade_archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "admin_id", "instance_slug", name="uq_instances_admin_id_slug"
        ),
        Index("ix_instances_active", "active"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Instance id={self.id} admin_id={self.admin_id} "
            f"slug={self.instance_slug} active={self.active}>"
        )
