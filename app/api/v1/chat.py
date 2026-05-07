"""
Chat API routes.

Handles synchronous and streaming chat endpoints.
"""

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.api.deps import get_chat_service
from app.middleware.rate_limit import limiter, get_api_key_or_ip, CHAT_RATE_LIMIT
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat_service import ChatService

router = APIRouter(prefix="/chat", tags=["chat"])

# Step 29.y Cluster 6 (G-1 fix): module logger for the
# chat_stream sanitized-error path. The exception is logged
# server-side at ERROR with full traceback via logger.exception()
# while the SSE client receives a hard-coded user-facing message.
logger = logging.getLogger(__name__)


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
            luciel_instance_id=getattr(request.state, "luciel_instance_id", None),  # Step 24.5 File 15
            actor_key_prefix=getattr(request.state, "key_prefix", None),  # Step 27b
            actor_user_id=getattr(request.state, "actor_user_id", None),  # Step 24.5b File 2.5
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
            luciel_instance_id=getattr(request.state, "luciel_instance_id", None),  # Step 24.5 File 15
            actor_key_prefix=getattr(request.state, "key_prefix", None),  # Step 27b
            actor_user_id=getattr(request.state, "actor_user_id", None),  # Step 24.5b File 2.5
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
        except Exception:
            # Step 29.y Cluster 6 (G-1 fix): do NOT leak str(exc) to
            # the client. Pre-29.y any in-stream exception (DB
            # connection error, LLM provider 401/429, JSONB
            # serialization failure, cross-tenant memory rejection,
            # etc.) was serialized to data: {"error": "<internal
            # message>"} and streamed verbatim. An attacker probing
            # /chat/stream could fingerprint internal state by
            # observing different error strings (tenant existence,
            # provider keys, scope-policy verbiage). See
            # findings_phase1g.md G-1 for the documented attack.
            #
            # We log the full exception server-side at ERROR (with
            # traceback via logger.exception) and stream a fixed,
            # generic message to the client.
            logger.exception("chat_stream: unhandled exception")
            yield f"data: {json.dumps({'error': 'Stream interrupted. Please retry.'})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
