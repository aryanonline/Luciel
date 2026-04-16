"""
Session API routes.

PATCHED: get_session() and list_messages() now verify the
session belongs to the caller's tenant via request.state.tenant_id.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.deps import get_session_service
from app.schemas.session import MessageRead, SessionCreate, SessionRead
from app.services.session_service import SessionService

router = APIRouter(tags=["sessions"])


@router.post(
    "",
    response_model=SessionRead,
    status_code=status.HTTP_201_CREATED,
)
def create_session(
    payload: SessionCreate,
    request: Request,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> SessionRead:
    """
    Create a new session.

    tenant_id, domain_id, and agent_id are resolved from the API key.
    The client can optionally provide them, but they must match
    what the API key allows.
    """
    key_tenant_id = getattr(request.state, "tenant_id", None)
    key_domain_id = getattr(request.state, "domain_id", None)
    key_agent_id = getattr(request.state, "agent_id", None)

    tenant_id = key_tenant_id or payload.tenant_id
    domain_id = key_domain_id or payload.domain_id
    agent_id = key_agent_id or payload.agent_id

    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="tenant_id is required (from API key or request body)",
        )
    if not domain_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="domain_id is required (from API key or request body)",
        )

    if key_domain_id and payload.domain_id and payload.domain_id != key_domain_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"API key is locked to domain '{key_domain_id}'",
        )

    if key_agent_id and payload.agent_id and payload.agent_id != key_agent_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"API key is locked to agent '{key_agent_id}'",
        )

    session = service.create_session(
        tenant_id=tenant_id,
        domain_id=domain_id,
        agent_id=agent_id,
        user_id=payload.user_id,
        channel=payload.channel,
    )
    return SessionRead.model_validate(session)


@router.get("", response_model=list[SessionRead])
def list_sessions(
    service: Annotated[SessionService, Depends(get_session_service)],
    request: Request,
    tenant_id: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[SessionRead]:
    key_tenant_id = getattr(request.state, "tenant_id", None)
    effective_tenant_id = key_tenant_id or tenant_id
    sessions = service.list_sessions(
        tenant_id=effective_tenant_id, user_id=user_id, limit=limit,
    )
    return [SessionRead.model_validate(item) for item in sessions]


@router.get("/{session_id}", response_model=SessionRead)
def get_session(
    session_id: str,
    request: Request,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> SessionRead:
    # Enforce tenant ownership — API key tenant must match session tenant
    key_tenant_id = getattr(request.state, "tenant_id", None)
    session = service.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    if key_tenant_id and session.tenant_id != key_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    return SessionRead.model_validate(session)


@router.get("/{session_id}/messages", response_model=list[MessageRead])
def list_messages(
    session_id: str,
    request: Request,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> list[MessageRead]:
    # Enforce tenant ownership — same check as get_session
    key_tenant_id = getattr(request.state, "tenant_id", None)
    session = service.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    if key_tenant_id and session.tenant_id != key_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    messages = service.list_messages(session_id)
    return [MessageRead.model_validate(item) for item in messages]
