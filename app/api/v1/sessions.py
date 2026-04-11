from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_session_service
from app.schemas.session import MessageRead, SessionCreate, SessionRead
from app.services.session_service import SessionService

router = APIRouter(tags=["sessions"])


@router.post("", response_model=SessionRead, status_code=status.HTTP_201_CREATED)
def create_session(
    payload: SessionCreate,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> SessionRead:
    session = service.create_session(
        tenant_id=payload.tenant_id,
        domain_id=payload.domain_id,
        user_id=payload.user_id,
        channel=payload.channel,
    )
    return SessionRead.model_validate(session)


@router.get("", response_model=list[SessionRead])
def list_sessions(
    service: Annotated[SessionService, Depends(get_session_service)],
    tenant_id: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[SessionRead]:
    sessions = service.list_sessions(
        tenant_id=tenant_id,
        user_id=user_id,
        limit=limit,
    )
    return [SessionRead.model_validate(item) for item in sessions]


@router.get("/{session_id}", response_model=SessionRead)
def get_session(
    session_id: str,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> SessionRead:
    session = service.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    return SessionRead.model_validate(session)


@router.get("/{session_id}/messages", response_model=list[MessageRead])
def list_messages(
    session_id: str,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> list[MessageRead]:
    session = service.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )

    messages = service.list_messages(session_id)
    return [MessageRead.model_validate(item) for item in messages]