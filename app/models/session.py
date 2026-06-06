from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String
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

    # Unit 13e §3.4.8 — the session-key participant id. The §3.4.8 session
    # key is (instance_id, participant_id, channel), where participant_id =
    # the resolved lead identity for lead-facing channels (set when the
    # identity resolver binds a session to a lead/User), or the internal
    # Slack workspace user id for the internal channel (§3.4.9 exception).
    #
    # ADDITIVE: user_id is kept verbatim for back-compat and the anonymous
    # widget path. A widget visitor is anonymous at v1 → resolved_lead_id
    # stays NULL. The §3.4.9 HARD RULE: a NULL resolved_lead_id must NEVER
    # match another session's NULL as "same participant" — anonymous tokens
    # never inherit history. SQL equality already gives this for free
    # (NULL = NULL is NULL, never TRUE), and the session-key lookup helper
    # refuses to match on a NULL participant id.
    #
    # NOT used by the budget meter (which keys on session_id + (admin_id,
    # instance_id, period_start), §3.4.1b) — adding this column does not
    # touch budget counting.
    resolved_lead_id: Mapped[str | None] = mapped_column(
        String(100),
        index=True,
        nullable=True,
        comment=(
            "§3.4.8 session-key participant id: the resolved lead identity "
            "(str of the resolved User.id) for lead-facing channels, or the "
            "internal workspace user id (§3.4.9). NULL = anonymous (never "
            "matches another NULL as same participant)."
        ),
    )
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

    # Rescan Tier-C §3.4.12 — human-controlled session mode.
    #
    # control_mode: 'luciel' (default) = agentic loop runs normally;
    #   'human_controlled' = orchestrator gate short-circuits, zero LLM
    #   calls, inbound messages are persisted + surfaced to dashboard.
    # taken_over_by_user_id: the admin User who initiated a takeover
    #   (NULL for Luciel-initiated path where trigger='luciel_escalated').
    # taken_over_at: UTC timestamp when control_mode became 'human_controlled'.
    # handed_back_at: UTC timestamp when the admin called /handback;
    #   NULL while still human_controlled or if the session ended via
    #   inactivity timeout before handback.
    control_mode: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="luciel",
        server_default="luciel",
        comment="Rescan Tier-C: 'luciel' or 'human_controlled'",
    )
    taken_over_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
        comment="Rescan Tier-C: admin User who initiated takeover (NULL=Luciel-initiated)",
    )
    taken_over_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Rescan Tier-C: UTC timestamp when session became human_controlled",
    )
    handed_back_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Rescan Tier-C: UTC timestamp when admin called /handback",
    )

    __table_args__ = (
        CheckConstraint(
            "control_mode IN ('luciel', 'human_controlled')",
            name="ck_sessions_control_mode",
        ),
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