from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_chat_service
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat_service import ChatService

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse, status_code=status.HTTP_200_OK)
def chat(
    payload: ChatRequest,
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> ChatResponse:
    """
    Handle one chat turn for an existing session.

    The route stays thin:
    - validate HTTP input
    - delegate to ChatService
    - translate errors into HTTP responses
    """
    try:
        reply = service.respond(
            session_id=payload.session_id,
            message=payload.message,
            provider=payload.provider,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    return ChatResponse(
        session_id=payload.session_id,
        reply=reply,
    )