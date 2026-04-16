from __future__ import annotations

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
    ) -> SessionModel:
        session = SessionModel(
            id=session_id,
            tenant_id=tenant_id,
            domain_id=domain_id,
            agent_id=agent_id,
            user_id=user_id,
            channel=channel,
            status=status,
        )
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        return session

    def get_session(self, session_id: str) -> SessionModel | None:
        return self.db.get(SessionModel, session_id)

    def list_sessions(
        self,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
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