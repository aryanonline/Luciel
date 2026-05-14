"""Conversation model -- durable cross-channel grouping of sessions.

Step 24.5c (Cross-channel identity and conversation continuity).

A Conversation is the durable thread that groups multiple Sessions across
channels (widget, programmatic API, voice/SMS/email when Step 34a lands).
One real-world person interacting with one Luciel through multiple channels
shows up as one Conversation row and N Session rows.

Design contract (see ARCHITECTURE.md §3.2.11 "Identity & conversation
continuity" for the canonical spec):

- Sessions remain the atomic auditable unit. Sessions are never merged.
  conversation_id is session-LINKING, not session-merging. Messages still
  hang off sessions.id; the audit chain at the session granularity stays
  walkable end-to-end.

- A Conversation lives in exactly one scope (tenant_id, domain_id). It is
  the grouping concept WITHIN a scope, never above it. Cross-tenant
  identity federation (the same person across two paying tenants) is
  Step 38 territory by design (ARCHITECTURE §4.9 rejected-alternative).

- tenant_id and domain_id mirror the existing scope-arithmetic shape used
  by SessionModel, ScopeAssignment, Agent, ApiKey, LucielInstance: both
  are String(100). tenant_id carries a FK to tenant_configs.tenant_id
  (RESTRICT). domain_id intentionally has no FK because domain_configs
  uses (tenant_id, domain_id) as a composite natural key -- a single-
  column FK from here would be a half-truth. Service layer validates
  the (tenant_id, domain_id) pair against domain_configs at write time.
  Same convention scope_assignments uses.

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
    Index,
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

    # Scope: (tenant_id, domain_id). String(100), same shape as everywhere
    # else in this codebase. tenant_id has FK to tenant_configs.tenant_id
    # with RESTRICT to protect identity history from cascade-delete on
    # tenant removal. domain_id deliberately has no FK; service layer
    # validates the composite (tenant_id, domain_id) against domain_configs
    # at write time. Same convention scope_assignments uses (File 1.2 of
    # Step 24.5b).
    tenant_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey(
            "tenant_configs.tenant_id",
            ondelete="RESTRICT",
            name="fk_conversations_tenant_id_tenant_configs",
        ),
        nullable=False,
        index=True,
    )

    domain_id: Mapped[str] = mapped_column(
        String(100),
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
    __table_args__ = (
        # Composite index on (tenant_id, domain_id, last_activity_at DESC)
        # serves the identity resolver's "most-recent active conversation
        # under this scope for this user" lookup once joined to sessions.
        # Recency-ordered partial index would be marginally tighter but
        # the simple form is easier to reason about; we can revisit if
        # the cross-session retriever shows hot-path pressure once it
        # lands in sub-branch 2.
        Index(
            "ix_conversations_tenant_domain_last_activity",
            "tenant_id",
            "domain_id",
            "last_activity_at",
        ),
        {"comment": (
            "Step 24.5c -- durable cross-channel conversation grouping. "
            "Session-linking via sessions.conversation_id, never session-"
            "merging. One Conversation lives in exactly one scope "
            "(tenant_id, domain_id). See ARCHITECTURE §3.2.11."
        )},
    )

    def __repr__(self) -> str:  # pragma: no cover -- debug only
        return (
            f"<Conversation id={self.id} "
            f"tenant_id={self.tenant_id!r} domain_id={self.domain_id!r} "
            f"active={self.active}>"
        )
