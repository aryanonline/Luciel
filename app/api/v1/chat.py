"""
Chat API routes.

Handles synchronous and streaming chat endpoints.
"""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.api.deps import get_chat_service
from app.middleware.rate_limit import limiter, get_api_key_or_ip, CHAT_RATE_LIMIT
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat_service import ChatService

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post(
    "",
    response_model=ChatResponse,
    status_code=status.HTTP_200_OK,
)
@limiter.limit(CHAT_RATE_LIMIT, key_func=get_api_key_or_ip)
def chat(
    request: Request,
    payload: ChatRequest,
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> ChatResponse:
    try:
        reply = service.respond(
            session_id=payload.session_id,
            message=payload.message,
            provider=payload.provider,
            caller_tenant_id=getattr(request.state, "tenant_id", None),
        )
    except PermissionError as exc:           # ADD this handler
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    return ChatResponse(session_id=payload.session_id, reply=reply)


@router.post("/stream")
@limiter.limit(CHAT_RATE_LIMIT, key_func=get_api_key_or_ip)
def chat_stream(
    request: Request,
    payload: ChatRequest,
    service: Annotated[ChatService, Depends(get_chat_service)],
):
    try:
        generator = service.respond_stream(
            session_id=payload.session_id,
            message=payload.message,
            provider=payload.provider,
            caller_tenant_id=getattr(request.state, "tenant_id", None),
        )
    except PermissionError as exc:           # ADD this handler
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    def event_stream():
        full_reply = ""
        try:
            for token in generator:
                full_reply += token
                yield f"data: {json.dumps({'token': token})}\n\n"

            yield f"data: {json.dumps({'done': True, 'session_id': payload.session_id, 'full_reply': full_reply})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
