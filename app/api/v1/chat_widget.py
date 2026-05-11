"""Step 30b commit (c): chat widget SSE endpoint.

POST /api/v1/chat/widget
------------------------

Public-facing chat surface for the embeddable Preact widget. The
endpoint mirrors the existing /api/v1/chat/stream SSE shape almost
exactly -- same event-stream format, same per-token framing -- but
sits behind extra constraints scoped to embed keys:

  * key_kind == 'embed'
  * permissions == ['chat']  (Step 30c lockstep -- no tools at v1)
  * Origin in the embed key's allowed_origins
  * Per-key minutely cap from api_keys.rate_limit_per_minute

See app/api/widget_deps.py for the dependency that enforces the
first three; the slowapi limit decorator below reads the per-key
cap statically via the EMBED_WIDGET_RATE_LIMIT constant on the
widget_deps module. The previous dynamic per-key cap shipped broken
(see widget_deps docstring); v1 uses a conservative global cap.

Why this is a SEPARATE endpoint from /chat/stream
--------------------------------------------------

Same SSE shape, different security envelope. /chat/stream is
called by trusted server-to-server clients with admin keys; the
widget runs on customer browsers with public embed keys. Forking
the endpoint keeps the public surface auditable -- one path, one
gate, one rate-limit policy -- and means future widget-only
features (CORS preflight, origin echo, branding payload echo)
don't have to coexist with admin-key semantics on /chat/stream.

OPTIONS preflight
-----------------

Browsers send a CORS preflight OPTIONS before the actual POST.
The auth middleware lets OPTIONS through unauthenticated for this
exact path (see app/middleware/auth.py); the OPTIONS handler
below answers with permissive CORS headers echoing the request
Origin. The Origin allowlist check happens on the POST itself,
not on preflight, because preflight has no key.

Lazy session creation
---------------------

The first widget message has no session_id. The endpoint creates
one, and the first SSE frame echoes it back as
``{\"session_id\": \"<uuid>\"}`` so the widget can persist it for
follow-up turns. This matches how the existing session-create
admin endpoint works but folds it into the chat path so the
widget never needs a separate session-create network round trip.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from app.api.deps import get_chat_service, get_session_service
from app.api.widget_deps import (
    EMBED_WIDGET_RATE_LIMIT,
    cors_response_headers,
    require_embed_key,
)
from app.core.config import settings
from app.middleware.rate_limit import limiter, get_api_key_or_ip
from app.policy.moderation import ModerationGate
from app.schemas.chat import ChatWidgetRequest
from app.services.chat_service import ChatService
from app.services.session_service import SessionService

router = APIRouter(prefix="/chat", tags=["chat-widget"])
logger = logging.getLogger(__name__)

# Step 30d Deliverable B: content-safety moderation gate.
#
# Built once at module import, same pattern as the module-level
# `logger` above. The factory reads settings.moderation_provider
# and raises ConfigurationError immediately if 'openai' is selected
# but openai_api_key is empty -- so a misconfigured production
# deploy crash-loops on rollout rather than silently running with a
# disabled gate. See app/policy/moderation.py.
_moderation_gate = ModerationGate.from_settings(settings)

# Neutral refusal returned when the moderation gate blocks a turn.
# Deliberately category-free: the operator sees the categories in
# the server-side WARNING line, but the client never does (same
# sanitization discipline as findings_phase1g.md G-1).
REFUSAL_MESSAGE = (
    "I can't help with that. Please rephrase or try a different question."
)


@router.options("/widget")
def widget_preflight(request: Request) -> Response:
    """CORS preflight handler.

    Permissive on purpose: we cannot scope to a specific embed key's
    allowlist on preflight because the browser does not attach the
    Authorization header to the OPTIONS request. The actual POST is
    what enforces the per-key origin check via require_embed_key.

    The response echoes the request Origin (rather than '*') so the
    widget bundle's fetch() succeeds for any caller, but the POST
    that follows is still gated. Caches Vary on Origin so a CDN
    cannot bleed responses across customer sites.
    """
    origin = request.headers.get("Origin", "")
    headers = {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
    }
    return Response(status_code=204, headers=headers)


@router.post("/widget")
@limiter.limit(EMBED_WIDGET_RATE_LIMIT, key_func=get_api_key_or_ip)
def widget_chat_stream(
    request: Request,
    payload: ChatWidgetRequest,
    chat_service: Annotated[ChatService, Depends(get_chat_service)],
    session_service: Annotated[SessionService, Depends(get_session_service)],
    widget_config: Annotated[dict, Depends(require_embed_key)],
):
    """Public widget SSE endpoint.

    Frame contract:
      * frame 1: {"session_id": "<uuid>", "widget_config": {...}}
        - sent before the first token so the widget can persist the
          session id and render branding (display_name, accent_color,
          greeting_message) on first turn
      * frames 2..N-1: {"token": "<chunk>"}
      * final frame: {"done": true, "session_id": "<uuid>"}
      * on error: {"error": "Stream interrupted. Please retry."}
        (sanitized; see findings_phase1g.md G-1)
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    domain_id = getattr(request.state, "domain_id", None)
    agent_id = getattr(request.state, "agent_id", None)

    # Embed keys MUST be tenant-scoped. NULL tenant_id means
    # platform-admin in our model -- it has no place on a public
    # widget surface. Defense-in-depth alongside the issuance-time
    # check (future commit).
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "embed_key_not_tenant_scoped",
                "message": "Embed keys must be bound to a tenant.",
            },
        )

    # Embed keys MUST be domain-scoped so create_session has a
    # non-NULL domain_id. If a key was issued without one we fail
    # closed here rather than silently use a placeholder.
    if domain_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "embed_key_not_domain_scoped",
                "message": (
                    "Embed keys must be bound to a domain. Re-issue "
                    "the key with a domain_id."
                ),
            },
        )

    # Lazy session creation. First widget turn has no session_id;
    # subsequent turns send the one echoed in the first SSE frame.
    if payload.session_id:
        session_id = payload.session_id
    else:
        session = session_service.create_session(
            tenant_id=tenant_id,
            domain_id=domain_id,
            agent_id=agent_id,
            user_id=None,  # widget visitors are anonymous at v1
            channel="widget",
        )
        # SessionModel's primary key column is `id` (see
        # app/models/session.py:17), not `session_id`. The session_id
        # name lives on payload (ChatWidgetRequest) and on MessageModel
        # as an FK, which is why this read site is the only one in the
        # codebase that touches the SessionModel attribute directly.
        session_id = session.id

    # --- Content-safety moderation gate (Step 30d Deliverable B) ----
    # Runs BEFORE the LLM call. If the gate blocks, we return a
    # 200 + sanitized SSE refusal frame in the existing widget frame
    # shape (session_id frame, single token frame, done frame). 200
    # not 4xx so the widget UI renders the refusal inline rather
    # than as a network-error banner, AND so the existence of the
    # gate is not trivially fingerprintable by a hostile prober
    # (4xx is a different signal than 200). The block is logged
    # server-side at WARNING with structured fields so the operator
    # has a triage signal; the moderation categories never reach the
    # client.
    sse_headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    sse_headers.update(cors_response_headers(request, widget_config))

    moderation = _moderation_gate.moderate(payload.message)
    if moderation.blocked:
        logger.warning(
            "widget_chat_stream: turn blocked by moderation gate",
            extra={
                "tenant_id": tenant_id,
                "domain_id": domain_id,
                "session_id": session_id,
                "categories": moderation.categories,
                "provider": moderation.provider,
                "provider_request_id": moderation.provider_request_id,
            },
        )

        def refusal_stream():
            # Same three-frame shape as a successful turn so the
            # widget renders the refusal as if it were a one-token
            # reply. session_id is echoed so follow-up turns can
            # carry it (an attacker who keeps probing will keep
            # getting refusals -- the block does NOT terminate the
            # session).
            yield (
                "data: "
                + json.dumps(
                    {
                        "session_id": session_id,
                        "widget_config": widget_config,
                    }
                )
                + "\n\n"
            )
            yield (
                "data: "
                + json.dumps({"token": REFUSAL_MESSAGE})
                + "\n\n"
            )
            yield (
                "data: "
                + json.dumps(
                    {"done": True, "session_id": session_id}
                )
                + "\n\n"
            )

        return StreamingResponse(
            refusal_stream(),
            media_type="text/event-stream",
            headers=sse_headers,
        )

    try:
        generator = chat_service.respond_stream(
            session_id=session_id,
            message=payload.message,
            provider=None,  # widget cannot override provider
            caller_tenant_id=tenant_id,
            luciel_instance_id=getattr(request.state, "luciel_instance_id", None),
            actor_key_prefix=getattr(request.state, "key_prefix", None),
            actor_user_id=getattr(request.state, "actor_user_id", None),
        )
    except PermissionError as exc:
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
        # First frame: hand the widget the session id and branding so
        # it can render the chat panel chrome before any token lands.
        yield f"data: {json.dumps({'session_id': session_id, 'widget_config': widget_config})}\n\n"
        try:
            for token in generator:
                yield f"data: {json.dumps({'token': token})}\n\n"
            yield f"data: {json.dumps({'done': True, 'session_id': session_id})}\n\n"
        except Exception:
            # Same sanitized-error contract as /chat/stream
            # (findings_phase1g.md G-1). Server-side log gets the
            # full traceback; client gets a fixed message.
            logger.exception("widget_chat_stream: unhandled exception")
            yield f"data: {json.dumps({'error': 'Stream interrupted. Please retry.'})}\n\n"

    # sse_headers built above (shared with the moderation refusal
    # path so both responses carry the same CORS/cache contract).
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=sse_headers,
    )
