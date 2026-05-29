"""Conversation model -- durable cross-channel grouping of sessions.

Step 24.5c (Cross-channel identity and conversation continuity).

A Conversation is the durable thread that groups multiple Sessions across
channels (widget, programmatic API, voice/SMS/email when Step 34a lands).
One real-world person interacting with one Luciel Instance through
multiple channels shows up as one Conversation row and N Session rows.

Design contract (see ARCHITECTURE.md §3.2.11 "Identity & conversation
continuity" and §3.7.2 "Admin → Instance boundary" for the canonical
specs):

- Sessions remain the atomic auditable unit. Sessions are never merged.
  conversation_id is session-LINKING, not session-merging. Messages still
  hang off sessions.id; the audit chain at the session granularity stays
  walkable end-to-end.

- A Conversation lives in exactly one Admin scope (admin_id). Under
  Architecture §3.7.2 the single authorization boundary is Admin →
  Instance; the Domain layer was eliminated at Arc 5 Path A. A
  Conversation belongs to one Admin and (via its bound sessions) to a
  specific Instance under that Admin. Cross-Admin identity federation
  (the same person across two paying Admins) is Step 38 territory by
  design (ARCHITECTURE §4.9 rejected-alternative).

- admin_id is the sole v2 scope column; it is ``String(100)`` with a
  RESTRICT FK to ``admins.id``. The legacy ``domain_id`` column was
  dropped from this table at Arc 12 EX3 (alembic migration
  ``arc12_ex3_drop_conversation_domain``); the V2 cross-session
  retriever resolves continuity by ``(admin_id, user_id)`` and no
  surface here reads ``domain_id`` anymore.

- PK is UUID (postgresql.UUID, gen_random_uuid()), matching the discipline
  introduced in 24.5b's User and ScopeAssignment tables. The cross-session
  retriever (app/memory/cross_session_retriever.py, landing in sub-branch
  2 of the impl arc) joins on conversation_id; using UUID avoids
  sequential-ID enumeration leaking per-tenant conversation counts.

- last_activity_at is the recency cursor the identity resolver walks when
  resolving "which active conversation should this new session bind to?"
  for a User with multiple recent sessions under the same scope. Update
  semantics live at the service/runtime layer (touched on each new
  message hung off a session bound to this conversation); the column
  itself only commits the storage shape.

- active is the soft-delete flag, mirroring User/ScopeAssignment discipline
  per Invariant 3. A closed conversation is active=False, never deleted.

Relationships:
- Conversation 1..N SessionModel (via sessions.conversation_id nullable FK
  added in the same migration as this table). Back-population from
  SessionModel lands in this sub-branch. Existing sessions stay with
  conversation_id=NULL (a single-session conversation, by definition);
  no backfill is performed because the v1 design contract is to let
  conversation continuity emerge as new sessions arrive bound to a User
  via identity_claims, not to retroactively group historical traffic.

Migration:
- Schema lands alongside identity_claims and sessions.conversation_id in
  the same hand-written migration (alembic revision 3dbbc70d0105). pgcrypto
  extension is already present from 24.5b; no extension management needed.

Cross-refs:
- ARCHITECTURE §3.2.11 (canonical spec for this primitive).
- CANONICAL_RECAP §11 Q8 (the strategic answer this primitive resolves).
- CANONICAL_RECAP §12 Step 24.5c row (the roadmap entry).
- DRIFTS.md D-step-24-5c-impl-backlog-2026-05-11 (the impl token this
  sub-branch starts to drain).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.session import SessionModel


class Conversation(Base):
    """Durable cross-channel grouping of sessions within a single scope.

    See module docstring for the design contract.
    """

    __tablename__ = "conversations"

    # UUID PK, gen_random_uuid() server-default. Matches User /
    # ScopeAssignment discipline -- sequential IDs would leak per-scope
    # conversation counts to anyone who can enumerate.
    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )


    admin_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Recency cursor for the identity resolver. Default to created_at via
    # server_default=now(); the service/runtime layer touches this on each
    # new message hung off a session bound to this conversation.
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    # Invariant 3: soft-delete only. A closed conversation is active=False,
    # never DELETEd. Cross-session retriever filters on active=True at the
    # service layer; the column itself only commits storage shape.
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )

    # Step 30a.2: stamped alongside active=false during tenant cascade
    # (admin_service.deactivate_tenant_with_cascade). Symmetric with
    # tenant_configs.deactivated_at and identity_claims.deactivated_at.
    # NULL on all rows that have never been deactivated (the vast
    # majority). Currently load-bearing only for future per-conversation
    # retention queries; the retention worker scans at tenant_configs
    # granularity. See ARCHITECTURE §3.2.13 (cascade extension).
    deactivated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    # ------ relationships ------
    # lazy="select" matches User / ScopeAssignment discipline -- explicit
    # eager loading happens at the repository / retriever layer where it
    # is needed. Avoid N+1 silently.
    sessions: Mapped[list["SessionModel"]] = relationship(
        "SessionModel",
        back_populates="conversation",
        foreign_keys="SessionModel.conversation_id",
        lazy="select",
    )

    # ------ table-level indexes ------
    # Arc 12 EX3: domain_id was dropped (alembic
    # arc12_ex3_drop_conversation_domain). The legacy composite
    # (tenant_id, domain_id, last_activity_at) was already gone from
    # prod after Arc 9.2 PR #101's auto-detect. v2 scope for this
    # table is just admin_id, served by ix_conversations_admin_id
    # (PR #96). No composite is recreated here pending measured
    # resolver hot-path pressure.
    __table_args__ = (
        {"comment": (
            "Step 24.5c -- durable cross-channel conversation grouping. "
            "Session-linking via sessions.conversation_id, never session-"
            "merging. One Conversation lives in exactly one scope "
            "(admin_id). See ARCHITECTURE §3.2.11."
        )},
    )

    def __repr__(self) -> str:  # pragma: no cover -- debug only
        return (
            f"<Conversation id={self.id} "
            f"admin_id={self.admin_id!r} "
            f"active={self.active}>"
        )
