from __future__ import annotations

import uuid

from app.repositories.session_repository import SessionRepository


class SessionService:
    def __init__(self, repository: SessionRepository) -> None:
        self.repository = repository

    def create_session(
        self,
        *,
        tenant_id: str,
        domain_id: str,
        user_id: str | None = None,
        channel: str = "web",
    ):
        session_id = str(uuid.uuid4())
        return self.repository.create_session(
            session_id=session_id,
            tenant_id=tenant_id,
            domain_id=domain_id,
            user_id=user_id,
            channel=channel,
        )

    def get_session(self, session_id: str):
        return self.repository.get_session(session_id)

    def list_sessions(
        self,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
        limit: int = 50,
    ):
        return self.repository.list_sessions(
            tenant_id=tenant_id,
            user_id=user_id,
            limit=limit,
        )

    def add_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        trace_id: str | None = None,
    ):
        return self.repository.add_message(
            session_id=session_id,
            role=role,
            content=content,
            trace_id=trace_id,
        )

    def list_messages(self, session_id: str):
        return self.repository.list_messages(session_id)