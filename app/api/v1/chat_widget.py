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
import time
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

# Step 31 sub-branch 1: widget audit log issuing-adapter identifier.
# Hardcoded to 'widget' here so the client cannot spoof which adapter
# asserted a claim -- the value is server-side only and is passed to
# SessionService.create_session_with_identity() when the request
# carries a client_claim.
WIDGET_ISSUING_ADAPTER = "widget"

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

    Step 31 sub-branch 1 -- application-level audit log
    ----------------------------------------------------
    Three structured `logger.info` emissions land on the CloudWatch
    `/ecs/luciel-backend` stream for every widget turn, scoped to a
    single per-request `request_id` so an operator can stitch the
    three lines together. None of the three carry the raw message
    body -- only its length -- so PII never crosses the log boundary.
    The widget-surface 📋 marker on ARCHITECTURE §3.2.7 flips ✅ with
    this change (DRIFTS token
    `D-widget-chat-no-application-level-audit-log-2026-05-10`).

    Step 31 sub-branch 1 -- create_session_with_identity route wiring
    -----------------------------------------------------------------
    When the request carries a `client_claim` field, the lazy session
    creation path swaps from the legacy `session_service.create_session(
    user_id=None, ...)` to `session_service.create_session_with_identity(
    claim_type=..., claim_value=..., issuing_adapter='widget', ...)` so
    subsequent widget turns from the same visitor (or any cross-channel
    follow-up under Step 34a) join the same `conversation_id` per the
    §3.2.11 design. When `client_claim` is absent the legacy anonymous
    path is preserved -- backward-compatible widget bundle bump.
    """
    # Step 31 sub-branch 1: capture entry-time monotonic clock so we
    # can emit `latency_ms` on the completion log line. time.monotonic()
    # is the right clock here -- it never goes backwards across an NTP
    # sync the way time.time() can.
    _turn_start_monotonic = time.monotonic()

    tenant_id = getattr(request.state, "tenant_id", None)
    domain_id = getattr(request.state, "domain_id", None)
    agent_id = getattr(request.state, "agent_id", None)
    luciel_instance_id = getattr(request.state, "luciel_instance_id", None)
    embed_key_prefix = getattr(request.state, "key_prefix", None)

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

    # Step 31 sub-branch 1: emission 1 of 3 -- request entry.
    #
    # Lands AFTER the tenant/domain scope checks so any 403 above
    # never produces a misleading audit row. Carries scope-bearing
    # ids plus message length (NEVER the message body itself -- PII
    # defense). The `has_client_claim` boolean is the only signal that
    # leaves of whether the customer's site asserted identity for
    # this turn; the claim_type / claim_value themselves are NOT
    # logged at this site because they may be PII (an email, a phone
    # number) and the operator does not need them here to triage.
    logger.info(
        "widget_chat_turn_received",
        extra={
            "event": "widget_chat_turn_received",
            "tenant_id": tenant_id,
            "domain_id": domain_id,
            "agent_id": agent_id,
            "luciel_instance_id": luciel_instance_id,
            "embed_key_prefix": embed_key_prefix,
            "message_length": len(payload.message),
            "has_session_id": payload.session_id is not None,
            "has_client_claim": payload.client_claim is not None,
        },
    )

    # Lazy session creation. First widget turn has no session_id;
    # subsequent turns send the one echoed in the first SSE frame.
    #
    # Step 31 sub-branch 1 also routes the FIRST-turn path through
    # SessionService.create_session_with_identity() when the customer's
    # site asserted a client_claim. The follow-up turns of the same
    # conversation come in with a session_id and bypass identity
    # resolution entirely -- the session row already binds the User
    # and the conversation, so there's nothing to resolve.
    is_new_session = payload.session_id is None
    is_new_user = False
    is_new_conversation = False
    user_id_for_audit: str | None = None
    conversation_id_for_audit: str | None = None

    if payload.session_id:
        session_id = payload.session_id
    elif payload.client_claim is not None:
        # Identity-bound lazy session creation (§3.3 step 4 hook).
        # ClaimType is uppercase on the enum (EMAIL/PHONE/SSO_SUBJECT)
        # while the schema accepts lowercase; convert here.
        from app.models.identity_claim import ClaimType
        result = session_service.create_session_with_identity(
            tenant_id=tenant_id,
            domain_id=domain_id,
            agent_id=agent_id,
            channel="widget",
            claim_type=ClaimType(payload.client_claim.claim_type.upper()),
            claim_value=payload.client_claim.claim_value,
            issuing_adapter=WIDGET_ISSUING_ADAPTER,
        )
        # SessionModel's primary key column is `id` (see
        # app/models/session.py:17), not `session_id`. Same read site
        # contract as the legacy branch below.
        session_id = result.session.id
        is_new_user = result.is_new_user
        is_new_conversation = result.is_new_conversation
        user_id_for_audit = str(result.user_id)
        conversation_id_for_audit = str(result.conversation_id)
    else:
        # Legacy anonymous widget path -- preserved verbatim for
        # backward compatibility with the bundles shipped before this
        # commit. is_new_user / is_new_conversation stay False because
        # nothing was resolved; the session is anonymous.
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

    # Step 31 sub-branch 1: emission 2 of 3 -- session resolved.
    #
    # Lands AFTER lazy session creation (or after echoing the
    # follow-up turn's session_id). Carries the resolved session +
    # identity binding so an operator can reconstruct which session
    # rows belong to which visitor without joining the trace table.
    # `user_id` and `conversation_id` are populated only on the
    # identity-bound path (client_claim present); the anonymous and
    # follow-up paths carry them as None.
    logger.info(
        "widget_chat_session_resolved",
        extra={
            "event": "widget_chat_session_resolved",
            "tenant_id": tenant_id,
            "domain_id": domain_id,
            "session_id": session_id,
            "user_id": user_id_for_audit,
            "conversation_id": conversation_id_for_audit,
            "is_new_session": is_new_session,
            "is_new_user": is_new_user,
            "is_new_conversation": is_new_conversation,
        },
    )

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
        # Step 31 sub-branch 1: emission 3 of 3 -- turn completed
        # (moderation-blocked variant). Emitted BEFORE the refusal
        # stream so the audit row lands even if the SSE response is
        # cut short by the client closing the connection. Same field
        # shape as the successful-completion emission below; the
        # `blocked_by_moderation=True` flag distinguishes the two.
        logger.info(
            "widget_chat_turn_completed",
            extra={
                "event": "widget_chat_turn_completed",
                "tenant_id": tenant_id,
                "domain_id": domain_id,
                "session_id": session_id,
                "latency_ms": int(
                    (time.monotonic() - _turn_start_monotonic) * 1000
                ),
                "tokens_emitted": 0,
                "blocked_by_moderation": True,
                "model": None,
                "provider": moderation.provider,
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
        tokens_emitted = 0
        completion_logged = False
        try:
            for token in generator:
                tokens_emitted += 1
                yield f"data: {json.dumps({'token': token})}\n\n"
            yield f"data: {json.dumps({'done': True, 'session_id': session_id})}\n\n"
            # Step 31 sub-branch 1: emission 3 of 3 -- turn completed
            # (successful-stream variant). Lands AFTER the final SSE
            # frame so latency_ms includes the full end-to-end stream
            # cost. The except branch below covers the interrupted
            # variant; both paths emit exactly one completion line.
            logger.info(
                "widget_chat_turn_completed",
                extra={
                    "event": "widget_chat_turn_completed",
                    "tenant_id": tenant_id,
                    "domain_id": domain_id,
                    "session_id": session_id,
                    "latency_ms": int(
                        (time.monotonic() - _turn_start_monotonic) * 1000
                    ),
                    "tokens_emitted": tokens_emitted,
                    "blocked_by_moderation": False,
                    "model": None,
                    "provider": None,
                },
            )
            completion_logged = True
        except Exception:
            # Same sanitized-error contract as /chat/stream
            # (findings_phase1g.md G-1). Server-side log gets the
            # full traceback; client gets a fixed message.
            logger.exception("widget_chat_stream: unhandled exception")
            yield f"data: {json.dumps({'error': 'Stream interrupted. Please retry.'})}\n\n"
            # Step 31 sub-branch 1: emit the completion line on the
            # error path too, so dashboards count interrupted turns
            # alongside successful ones. completion_logged is checked
            # to defensively skip the second emission if the for-loop
            # actually exited cleanly before something else in the
            # try-block raised (currently nothing can, but the guard
            # keeps the contract "exactly one completion line per
            # turn" robust against future edits to this block).
            if not completion_logged:
                logger.info(
                    "widget_chat_turn_completed",
                    extra={
                        "event": "widget_chat_turn_completed",
                        "tenant_id": tenant_id,
                        "domain_id": domain_id,
                        "session_id": session_id,
                        "latency_ms": int(
                            (time.monotonic() - _turn_start_monotonic) * 1000
                        ),
                        "tokens_emitted": tokens_emitted,
                        "blocked_by_moderation": False,
                        "model": None,
                        "provider": None,
                    },
                )

    # sse_headers built above (shared with the moderation refusal
    # path so both responses carry the same CORS/cache contract).
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=sse_headers,
    )
