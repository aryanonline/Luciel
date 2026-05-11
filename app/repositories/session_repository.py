"""
Session repository.

PATCHED:
  S1 — get_session() now accepts optional tenant_id for ownership check.
  S2 — list_sessions() now accepts agent_id filter.
  S3 — Step 24.5c sub-branch 4: create_session() now accepts an optional
       conversation_id (UUID) that the identity resolver supplies. NULL
       is the legacy / single-session conversation path (existing
       callers see no behavioural change).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.message import MessageModel
from app.models.session import SessionModel


class SessionRepository:

    def __init__(self, db: Session) -> None:
        self.db = db

    def create_session(
        self,
        *,
        session_id: str,
        tenant_id: str,
        domain_id: str,
        agent_id: str | None = None,
        user_id: str | None = None,
        channel: str = "web",
        status: str = "active",
        conversation_id: uuid.UUID | None = None,
    ) -> SessionModel:
        # Step 24.5c sub-branch 4: conversation_id is the FK to
        # conversations.id that groups sibling sessions across
        # channels. NULL = no continuity claim yet (a single-session
        # conversation, the existing semantics). The identity
        # resolver (app.identity.resolver) supplies a UUID when
        # the request asserted an identity_claim. Existing callers
        # that don't pass conversation_id continue to mint NULL
        # sessions -- behavioural compatibility per the §3.2.11
        # nullable-by-design contract.
        session = SessionModel(
            id=session_id,
            tenant_id=tenant_id,
            domain_id=domain_id,
            agent_id=agent_id,
            user_id=user_id,
            channel=channel,
            status=status,
            conversation_id=conversation_id,
        )
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        return session

    def get_session(
        self,
        session_id: str,
        *,
        tenant_id: str | None = None,
    ) -> SessionModel | None:
        """
        Get a session by ID.

        If tenant_id is provided, also verifies the session belongs
        to that tenant. Returns None if the session exists but belongs
        to a different tenant — preventing cross-tenant reads.
        """
        session = self.db.get(SessionModel, session_id)
        if session is None:
            return None
        if tenant_id and session.tenant_id != tenant_id:
            return None
        return session

    def list_sessions(
        self,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> list[SessionModel]:
        stmt = (
            select(SessionModel)
            .order_by(SessionModel.created_at.desc())
            .limit(limit)
        )
        if tenant_id:
            stmt = stmt.where(SessionModel.tenant_id == tenant_id)
        if user_id:
            stmt = stmt.where(SessionModel.user_id == user_id)
        if agent_id:
            stmt = stmt.where(SessionModel.agent_id == agent_id)
        return list(self.db.scalars(stmt).all())

    def add_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        trace_id: str | None = None,
    ) -> MessageModel:
        message = MessageModel(
            session_id=session_id,
            role=role,
            content=content,
            trace_id=trace_id,
        )
        self.db.add(message)
        self.db.commit()
        self.db.refresh(message)
        return message

    def list_messages(self, session_id: str) -> list[MessageModel]:
        stmt = (
            select(MessageModel)
            .where(MessageModel.session_id == session_id)
            .order_by(MessageModel.created_at.asc(), MessageModel.id.asc())
        )
        return list(self.db.scalars(stmt).all())
