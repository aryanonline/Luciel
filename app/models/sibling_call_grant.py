"""SiblingCallGrant ORM — Arc 12 WU4.

Mirrors the ``sibling_call_grants`` table created at
``alembic/versions/arc12_wu4_sibling_call_grants.py``.

Architecture v1 §3.3.4 — sibling-Luciel composition. The grant row
is the durable record that authorises ``call_sibling_luciel`` to
dispatch a call from one Instance to another under the same Admin.
Three approval states:

* ``live``              — grant is in force; WU5 runtime dispatch
                          finds it and lets the call through.
* ``pending_approval``  — Enterprise-only intermediate state. The
                          grant has been authored but is awaiting
                          ``admin_owner`` approval before it goes
                          live. WU5 dispatch refuses pending grants
                          (they're not live yet).
* ``revoked``           — terminal state. Either the grant was
                          live/pending and was revoked, or the
                          instance-deactivation cascade swept it.
                          WU5 dispatch refuses revoked grants.

Walls
-----
* Wall-1 (admin) — ``admin_id`` carries the tenant boundary; RLS
  fences on it.
* Wall-2 (role+scope) — enforced at the grant-authoring API layer
  via ``ScopePolicy.enforce_role_on_instance`` on BOTH the caller
  and the callee Instance. A user scoped to only one of the two
  cannot author a cross-Instance grant.
* Wall-3 (instance) — the (caller, callee) pair both reference
  ``instances.id``; FK + the CHECK on caller != callee enforces
  the structural constraint at the DB layer.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


# Approval-state literals. Kept as module-level constants so callers
# (service, repo, tests, route layer) reference a single source of
# truth. The CHECK constraint in the migration pins the column to
# exactly these values.
APPROVAL_STATE_LIVE = "live"
APPROVAL_STATE_PENDING = "pending_approval"
APPROVAL_STATE_REVOKED = "revoked"

ALLOWED_APPROVAL_STATES: frozenset[str] = frozenset({
    APPROVAL_STATE_LIVE,
    APPROVAL_STATE_PENDING,
    APPROVAL_STATE_REVOKED,
})


class SiblingCallGrant(Base):
    __tablename__ = "sibling_call_grants"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    admin_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    caller_instance_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("instances.id", ondelete="RESTRICT"),
        nullable=False,
    )
    callee_instance_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("instances.id", ondelete="RESTRICT"),
        nullable=False,
    )
    granted_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    approval_state: Mapped[str] = mapped_column(
        String(20), nullable=False
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
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

    __table_args__ = (
        CheckConstraint(
            "caller_instance_id <> callee_instance_id",
            name="ck_sibling_call_grants_no_self_edge",
        ),
        CheckConstraint(
            "approval_state IN ('live', 'pending_approval', 'revoked')",
            name="ck_sibling_call_grants_approval_state",
        ),
        Index(
            "ix_sibling_call_grants_dispatch",
            "admin_id",
            "caller_instance_id",
            postgresql_where=text("approval_state = 'live'"),
        ),
        Index(
            "uq_sibling_call_grants_active",
            "admin_id",
            "caller_instance_id",
            "callee_instance_id",
            unique=True,
            postgresql_where=text("approval_state <> 'revoked'"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<SiblingCallGrant id={self.id} admin={self.admin_id} "
            f"caller={self.caller_instance_id} callee={self.callee_instance_id} "
            f"state={self.approval_state}>"
        )
