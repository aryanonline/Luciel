from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.conversation import Conversation
    from app.models.message import MessageModel


class SessionModel(Base, TimestampMixin):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    admin_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Arc 9.1 Phase A (2026-05-25): NOT NULL. See arc9_1_a_tenant_isolation_seal.
    # Every session is now bound to its Instance at creation time.
    # Arc 5 Revision C / Arc 9.2 PR #99 — FK target is `instances.id`
    # (the `luciel_instances` table was dropped in Arc 5 Revision C
    # and the column kept its legacy name only). The earlier model
    # still pointed the SQLAlchemy FK at the dropped table, which
    # raised NoReferencedTableError on every widget chat. The DB-side
    # FK constraint is named `fk_sessions_luciel_instance_id` and is
    # the migration of record; this string is metadata-only.
    luciel_instance_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "instances.id",
            ondelete="SET NULL",
            name="fk_sessions_luciel_instance_id",
        ),
        nullable=False,
        index=True,
    )

    user_id: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    channel: Mapped[str] = mapped_column(String(50), default="web", nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="active", nullable=False)

    # Step 24.5c — nullable FK to the Conversation this session is part of.
    # NULL = a single-session conversation (the session has no continuity
    # claim yet, e.g. a brand-new visitor on a fresh device with no prior
    # identity_claims match). When a later session is bound to the same
    # User via identity_claims under the same scope, the resolver resolves
    # this column to the User's most recent active conversation under that
    # scope. Session-linking, never session-merging — message rows still
    # hang off sessions.id, the audit chain at session granularity stays
    # walkable. See ARCHITECTURE §3.2.11 + §4.9 rejected-alternative.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "conversations.id",
            ondelete="SET NULL",
            name="fk_sessions_conversation_id",
        ),
        nullable=True,
        index=True,
    )

    messages: Mapped[list["MessageModel"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="MessageModel.created_at",
    )

    # Step 24.5c — back-populated from Conversation.sessions.
    conversation: Mapped["Conversation | None"] = relationship(
        "Conversation",
        back_populates="sessions",
        foreign_keys=[conversation_id],
        lazy="select",
    )