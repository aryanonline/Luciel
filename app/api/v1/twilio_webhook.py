"""Arc 13 D4 — Twilio inbound-SMS webhook (store-and-forward ingress).

Twilio POSTs an inbound text (form-encoded) to ``/api/v1/twilio/sms``.
This route is the HTTP binding for :class:`SmsChannelAdapter`:

  1. Build the verification envelope (full URL + form params + the
     ``X-Twilio-Signature`` header) and run ``verify_inbound`` — which
     checks the HMAC signature FIRST, then resolves the destination
     number to a live Instance. A bad signature is a 403; an authentic
     payload that routes nowhere is audit-logged as a DROP (never
     silent) and answered with an empty 204.
  3. ``receive`` canonicalises the turn; the sender E.164 is resolved to
     a session via the identity resolver (``issuing_adapter=
     'sms_gateway'``) so repeat texters continue one conversation.
  4. ChatService.respond produces the reply; ``send`` dispatches it
     (live-switch gated — no real Twilio call in dev/CI) and the
     delivery is audit-logged.

Public surface: the api-key middleware skips ``/api/v1/twilio`` (Twilio
carries no API key); the X-Twilio-Signature HMAC is the auth gate.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps import get_chat_service, get_session_service
from app.channels.base import (
    SignatureVerificationError,
    UnresolvableInboundError,
    check_instance_lifecycle,
)
from app.channels.sms_adapter import SmsChannelAdapter
from app.db.session import get_db
from app.middleware.rate_limit import limiter
from app.models.admin_audit_log import (
    ACTION_CHANNEL_INBOUND_DROPPED,
    ACTION_CHANNEL_INBOUND_RECEIVED,
    ACTION_CHANNEL_OUTBOUND_DELIVERED,
    RESOURCE_INSTANCE_CHANNEL,
)
from app.models.identity_claim import ClaimType
from app.repositories.admin_audit_repository import AdminAuditRepository, AuditContext
from app.services.chat_service import ChatService
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)

router = APIRouter()

_SMS_ISSUING_ADAPTER = "sms_gateway"


@router.post("/twilio/sms")
@limiter.limit("600/minute")
async def receive_twilio_sms(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    chat_service: Annotated[ChatService, Depends(get_chat_service)],
    session_service: Annotated[SessionService, Depends(get_session_service)],
) -> Response:
    """Receive an inbound Twilio SMS, answer it, and reply over SMS."""
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    signature = request.headers.get("X-Twilio-Signature", "")
    envelope = {
        "url": str(request.url),
        "params": params,
        "signature": signature,
    }

    adapter = SmsChannelAdapter(db)
    audit = AdminAuditRepository(db)

    # --- verify_inbound: signature FIRST, then routing. ---
    try:
        ctx = adapter.verify_inbound(envelope)
    except SignatureVerificationError as e:
        logger.warning("twilio_webhook: signature verification failed: %s", e)
        return Response(status_code=403)
    except UnresolvableInboundError as e:
        # Authentic but unroutable → audit-log the drop (never silent).
        logger.info("twilio_webhook: unresolvable inbound dropped: %s", e)
        audit.record(
            ctx=AuditContext.system("twilio_webhook"),
            admin_id="platform",
            action=ACTION_CHANNEL_INBOUND_DROPPED,
            resource_type=RESOURCE_INSTANCE_CHANNEL,
            resource_natural_id=(params.get("To") or "")[:320] or None,
            note=f"Inbound SMS dropped (unresolvable): {e}",
            autocommit=True,
        )
        return Response(status_code=204)

    # --- lifecycle gate: only an ACTIVE instance is served. ---
    # Architecture §3.6.2: a paused/deactivating/grace_window (or missing)
    # instance acknowledges the inbound with a 2xx no-op and is NOT routed
    # to the runtime — no reply, no budget accrual. Shared across all
    # channels via check_instance_lifecycle; audited, never silent.
    drop = check_instance_lifecycle(db, ctx)
    if drop is not None:
        logger.info(
            "twilio_webhook: inbound dropped, instance not active "
            "(instance_id=%s status=%s)",
            drop.instance_id,
            drop.status,
        )
        audit.record(
            ctx=AuditContext.system("twilio_webhook"),
            admin_id=ctx.admin_id,
            action=ACTION_CHANNEL_INBOUND_DROPPED,
            resource_type=RESOURCE_INSTANCE_CHANNEL,
            resource_natural_id=(params.get("To") or "")[:320] or None,
            luciel_instance_id=drop.instance_id,
            note=(
                f"Inbound dropped: instance not active (status={drop.status})"
            ),
            autocommit=True,
        )
        return Response(status_code=204)

    inbound = adapter.receive(envelope)

    # --- resolve the sender E.164 to a session (find-or-continue). ---
    resolution = session_service.create_session_with_identity(
        admin_id=ctx.admin_id,
        channel="sms",
        claim_type=ClaimType.PHONE,
        claim_value=inbound.customer_identifier,
        issuing_adapter=_SMS_ISSUING_ADAPTER,
        luciel_instance_id=ctx.instance_id,
    )
    session_id = resolution.session.id

    audit.record(
        ctx=AuditContext.system("twilio_webhook"),
        admin_id=ctx.admin_id,
        action=ACTION_CHANNEL_INBOUND_RECEIVED,
        resource_type=RESOURCE_INSTANCE_CHANNEL,
        resource_natural_id=inbound.channel_metadata.get("to"),
        luciel_instance_id=ctx.instance_id,
        after={
            "channel": "sms",
            "from": inbound.customer_identifier,
            "session_id": session_id,
            "message_sid": inbound.channel_metadata.get("message_sid"),
        },
        note="Inbound SMS received + routed.",
    )

    reply_text = chat_service.respond(
        session_id=session_id,
        message=inbound.body,
        caller_tenant_id=ctx.admin_id,
        luciel_instance_id=ctx.instance_id,
    )

    from app.channels.base import OutboundMessage

    receipt = adapter.send(
        OutboundMessage(
            to=inbound.customer_identifier,
            body=reply_text,
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            session_id=session_id,
            channel_metadata={"from": inbound.channel_metadata.get("to")},
        )
    )

    audit.record(
        ctx=AuditContext.system("twilio_webhook"),
        admin_id=ctx.admin_id,
        action=ACTION_CHANNEL_OUTBOUND_DELIVERED,
        resource_type=RESOURCE_INSTANCE_CHANNEL,
        resource_natural_id=inbound.customer_identifier,
        luciel_instance_id=ctx.instance_id,
        after={
            "channel": "sms",
            "provider_message_id": receipt.provider_message_id,
            "status": receipt.status,
            "session_id": session_id,
        },
        note="Outbound SMS reply delivered.",
        autocommit=True,
    )

    return Response(status_code=204)
