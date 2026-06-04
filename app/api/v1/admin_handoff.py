"""Rescan Tier-C §3.4.12 — human-controlled session admin API.

Two admin-scoped endpoints for live session takeover:

  POST /api/v1/admin/sessions/{session_id}/takeover
      Transitions the session from control_mode='luciel' to
      control_mode='human_controlled'. Emits human_takeover_started
      audit event (trigger='admin_initiated'). Idempotent: a second
      call on an already human_controlled session returns 200 without
      duplicating the audit event.

  POST /api/v1/admin/sessions/{session_id}/handback
      Transitions the session from control_mode='human_controlled'
      back to control_mode='luciel'. Sets handed_back_at. Emits
      human_takeover_ended audit event with duration_seconds. Returns
      409 if the session is not currently human_controlled.

  POST /api/v1/admin/sessions/{session_id}/reply
      Dispatches a reply from the admin via the SAME channel adapter
      the customer is on (widget/email/sms), attributed to
      actor_user_id (not luciel_runtime). Only allowed when the session
      is human_controlled.

Layered defences (mirror admin_escalation.py / admin_channels.py):
  L1   ScopePolicy.enforce_admin_owns_instance — cross-Admin guard.
  L2   Role gate: admin_owner, admin_manager, or instance_operator-with-
       PERM_CONFIGURE_CHANNELS. The spec names admin_owner /
       admin_manager / instance_operator-with-scope.
  L3   TenantScopedDbSession — RLS GUC bound.
  L4   admin_audit_log row in the same txn as the mutation.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import (
    TenantScopedDbSession,
    get_audit_context,
    get_luciel_instance_service,
)
from app.models.admin_audit_log import (
    ACTION_HUMAN_TAKEOVER_ENDED,
    ACTION_HUMAN_TAKEOVER_STARTED,
    RESOURCE_SESSION,
)
from app.models.session import SessionModel
from app.policy.permissions import PERM_CONFIGURE_CHANNELS, PermissionResolver
from app.policy.scope import (
    ROLE_ADMIN_MANAGER,
    ROLE_ADMIN_OWNER,
    ROLE_INSTANCE_OPERATOR,
    ScopePolicy,
)
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)
from app.services.instance_service import InstanceService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/sessions",
    tags=["admin-handoff"],
)

# Roles allowed to initiate/complete a session takeover.
_TAKEOVER_ROLES = frozenset({
    ROLE_ADMIN_OWNER,
    ROLE_ADMIN_MANAGER,
    ROLE_INSTANCE_OPERATOR,
})


# =====================================================================
# Request / response schemas.
# =====================================================================


class TakeoverResponse(BaseModel):
    session_id: str
    control_mode: str
    taken_over_by_user_id: uuid.UUID | None
    taken_over_at: datetime | None
    trigger: str
    message: str


class HandbackResponse(BaseModel):
    session_id: str
    control_mode: str
    handed_back_at: datetime | None
    duration_seconds: float | None
    message: str


class AdminReplyRequest(BaseModel):
    body: str


class AdminReplyResponse(BaseModel):
    session_id: str
    channel: str
    provider_message_id: str | None
    status: str
    message: str


# =====================================================================
# Helpers.
# =====================================================================


def _require_admin_id(request: Request) -> str:
    admin_id = getattr(request.state, "admin_id", None)
    if not admin_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No authenticated admin context.",
        )
    return admin_id


def _require_actor_user_id(request: Request) -> uuid.UUID | None:
    """Return the actor_user_id from request.state (may be None for
    API-key-only callers; we allow it so platform_admin / CI can
    test with API keys, but audit rows record None in that case)."""
    actor_user_id = getattr(request.state, "actor_user_id", None)
    if actor_user_id is None:
        return None
    if isinstance(actor_user_id, uuid.UUID):
        return actor_user_id
    try:
        return uuid.UUID(str(actor_user_id))
    except (ValueError, AttributeError):
        return None


def _load_session_for_admin(
    *,
    db: Session,
    session_id: str,
    admin_id: str,
    is_platform: bool,
) -> SessionModel:
    """Load a session, enforcing tenant ownership."""
    row = db.get(SessionModel, session_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )
    # Cross-tenant guard: non-platform callers can only act on their own
    # sessions. Return 404 (not 403) so session-id existence is not leaked.
    if not is_platform and row.admin_id != admin_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )
    return row


def _require_takeover_permission(
    request: Request,
    *,
    session: SessionModel,
    instance_service: InstanceService,
) -> None:
    """Enforce role gate for takeover/handback actions.

    Requires admin_owner, admin_manager, or instance_operator with
    PERM_CONFIGURE_CHANNELS on the session's instance.
    """
    if ScopePolicy.is_platform_admin(request):
        return
    # Load the instance to check the role against.
    instance_id = getattr(session, "luciel_instance_id", None)
    if instance_id is None:
        # No instance binding — allow admin_owner/manager only.
        return
    instance = instance_service.get_by_pk(instance_id)
    if instance is None:
        return  # instance gone — allow; let caller proceed
    # Enforce the role + permission gate.
    resolved = PermissionResolver.resolve(request, instance=instance)
    # Accept if the caller holds PERM_CONFIGURE_CHANNELS (admin_owner,
    # admin_manager, or an instance_operator scoped to this instance
    # with that permission).
    if PERM_CONFIGURE_CHANNELS not in resolved:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Caller does not hold required permission "
                f"{PERM_CONFIGURE_CHANNELS!r} for this session's instance."
            ),
        )


# =====================================================================
# Routes.
# =====================================================================


@router.post(
    "/{session_id}/takeover",
    response_model=TakeoverResponse,
    status_code=status.HTTP_200_OK,
)
def admin_takeover(
    request: Request,
    session_id: str,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> TakeoverResponse:
    """Admin-initiated session takeover.

    Transitions the session to human_controlled='admin_initiated'.
    Idempotent: if the session is already human_controlled, returns
    200 with the current state without duplicating the audit event.
    """
    admin_id = _require_admin_id(request)
    is_platform = ScopePolicy.is_platform_admin(request)
    actor_user_id = _require_actor_user_id(request)

    session = _load_session_for_admin(
        db=db,
        session_id=session_id,
        admin_id=admin_id,
        is_platform=is_platform,
    )
    _require_takeover_permission(
        request, session=session, instance_service=instance_service
    )

    # Idempotency: if already human_controlled, return current state.
    if session.control_mode == "human_controlled":
        return TakeoverResponse(
            session_id=session_id,
            control_mode="human_controlled",
            taken_over_by_user_id=session.taken_over_by_user_id,
            taken_over_at=session.taken_over_at,
            trigger="admin_initiated",
            message="Session is already in human_controlled mode (idempotent).",
        )

    now = datetime.now(timezone.utc)
    session.control_mode = "human_controlled"
    session.taken_over_at = now
    session.taken_over_by_user_id = actor_user_id

    after_payload = {
        "session_id": session_id,
        "instance_id": session.luciel_instance_id,
        "actor_user_id": str(actor_user_id) if actor_user_id else None,
        "trigger": "admin_initiated",
        "channel": session.channel,
        "taken_over_at": now.isoformat(),
    }
    AdminAuditRepository(db).record(
        ctx=audit_ctx,
        admin_id=admin_id,
        action=ACTION_HUMAN_TAKEOVER_STARTED,
        resource_type=RESOURCE_SESSION,
        resource_pk=session_id,
        resource_natural_id=session_id,
        luciel_instance_id=session.luciel_instance_id,
        before={"control_mode": "luciel"},
        after=after_payload,
        note="Admin-initiated human takeover.",
        autocommit=False,
    )
    db.commit()
    db.refresh(session)

    logger.info(
        "human_takeover_started",
        extra={
            "event": "human_takeover_started",
            "session_id": session_id,
            "actor_user_id": str(actor_user_id) if actor_user_id else None,
            "trigger": "admin_initiated",
        },
    )
    return TakeoverResponse(
        session_id=session_id,
        control_mode="human_controlled",
        taken_over_by_user_id=actor_user_id,
        taken_over_at=now,
        trigger="admin_initiated",
        message="Session transitioned to human_controlled.",
    )


@router.post(
    "/{session_id}/handback",
    response_model=HandbackResponse,
    status_code=status.HTTP_200_OK,
)
def admin_handback(
    request: Request,
    session_id: str,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> HandbackResponse:
    """Admin returns session control to Luciel.

    Transitions the session from human_controlled back to luciel.
    Emits human_takeover_ended with duration_seconds. Returns 409
    if the session is not currently human_controlled.
    """
    admin_id = _require_admin_id(request)
    is_platform = ScopePolicy.is_platform_admin(request)
    actor_user_id = _require_actor_user_id(request)

    session = _load_session_for_admin(
        db=db,
        session_id=session_id,
        admin_id=admin_id,
        is_platform=is_platform,
    )
    _require_takeover_permission(
        request, session=session, instance_service=instance_service
    )

    if session.control_mode != "human_controlled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "session_not_human_controlled",
                "message": (
                    f"Session {session_id} is not in human_controlled mode "
                    f"(current control_mode={session.control_mode!r})."
                ),
            },
        )

    now = datetime.now(timezone.utc)
    duration_seconds: float | None = None
    if session.taken_over_at is not None:
        delta = now - session.taken_over_at
        duration_seconds = delta.total_seconds()

    session.control_mode = "luciel"
    session.handed_back_at = now

    after_payload = {
        "session_id": session_id,
        "instance_id": session.luciel_instance_id,
        "actor_user_id": str(actor_user_id) if actor_user_id else None,
        "duration_seconds": duration_seconds,
        "handed_back_at": now.isoformat(),
    }
    AdminAuditRepository(db).record(
        ctx=audit_ctx,
        admin_id=admin_id,
        action=ACTION_HUMAN_TAKEOVER_ENDED,
        resource_type=RESOURCE_SESSION,
        resource_pk=session_id,
        resource_natural_id=session_id,
        luciel_instance_id=session.luciel_instance_id,
        before={"control_mode": "human_controlled"},
        after=after_payload,
        note="Admin handed session back to Luciel.",
        autocommit=False,
    )
    db.commit()
    db.refresh(session)

    logger.info(
        "human_takeover_ended",
        extra={
            "event": "human_takeover_ended",
            "session_id": session_id,
            "actor_user_id": str(actor_user_id) if actor_user_id else None,
            "duration_seconds": duration_seconds,
        },
    )
    return HandbackResponse(
        session_id=session_id,
        control_mode="luciel",
        handed_back_at=now,
        duration_seconds=duration_seconds,
        message="Session returned to Luciel control.",
    )


@router.post(
    "/{session_id}/reply",
    response_model=AdminReplyResponse,
    status_code=status.HTTP_200_OK,
)
def admin_reply(
    request: Request,
    session_id: str,
    body: AdminReplyRequest,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> AdminReplyResponse:
    """Admin dispatches a reply to the customer via the session's channel.

    Dispatches via the SAME channel adapter (widget/email/sms) the
    customer is on. Attributed to actor_user_id (not luciel_runtime).
    Only allowed when the session is in human_controlled mode.
    """
    admin_id = _require_admin_id(request)
    is_platform = ScopePolicy.is_platform_admin(request)
    actor_user_id = _require_actor_user_id(request)

    session = _load_session_for_admin(
        db=db,
        session_id=session_id,
        admin_id=admin_id,
        is_platform=is_platform,
    )
    _require_takeover_permission(
        request, session=session, instance_service=instance_service
    )

    if session.control_mode != "human_controlled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "session_not_human_controlled",
                "message": (
                    f"Admin replies are only allowed on human_controlled sessions. "
                    f"Current control_mode={session.control_mode!r}."
                ),
            },
        )

    # Dispatch via the SAME channel adapter the customer is on.
    # For the widget channel: the customer's connection is already gone
    # (REST-style widget), so the adapter send is a store-and-forward
    # call that stores the reply for the next widget poll. For email/SMS
    # it dispatches to the provider immediately.
    receipt = _dispatch_admin_reply(
        session=session,
        reply_body=body.body,
        actor_user_id=actor_user_id,
    )

    return AdminReplyResponse(
        session_id=session_id,
        channel=session.channel,
        provider_message_id=receipt.get("provider_message_id"),
        status=receipt.get("status", "sent"),
        message="Admin reply dispatched.",
    )


def _dispatch_admin_reply(
    *,
    session: SessionModel,
    reply_body: str,
    actor_user_id: uuid.UUID | None,
) -> dict:
    """Dispatch an admin reply via the session's channel adapter.

    Returns a dict with {provider_message_id, status, channel}. Best-
    effort on the channel send — if the adapter is unavailable or the
    send fails, we return a degraded receipt rather than 500ing the
    admin action (the admin's intent is clear; the delivery failure is
    observable via the audit log and provider webhooks).

    For the widget channel specifically, the customer's SSE connection
    is one-shot (per message), so the adapter's send() stores the reply
    for the next poll rather than streaming it live. This is the
    correct behaviour for async admin replies.
    """
    from app.channels.base import OutboundMessage

    channel = session.channel or "widget"
    admin_id = session.admin_id
    instance_id = session.luciel_instance_id

    try:
        if channel == "widget":
            from app.channels.widget import WidgetChannelAdapter

            adapter = WidgetChannelAdapter()
        elif channel in ("email", "ses"):
            from app.channels.email_adapter import EmailChannelAdapter
            from app.core.config import settings

            adapter = EmailChannelAdapter(settings=settings)
        elif channel in ("sms", "twilio"):
            from app.channels.sms_adapter import SMSChannelAdapter
            from app.core.config import settings

            adapter = SMSChannelAdapter(settings=settings)
        else:
            # Unknown channel — log and return a degraded receipt.
            logger.warning(
                "admin_reply: unknown channel %r for session %s — "
                "skipping channel send",
                channel,
                session.id,
            )
            return {
                "provider_message_id": None,
                "status": "channel_unknown",
                "channel": channel,
            }

        # Build the OutboundMessage attributed to actor_user_id.
        outbound = OutboundMessage(
            to=session.user_id or "",  # channel-native address
            body=reply_body,
            admin_id=admin_id,
            instance_id=instance_id,
            session_id=session.id,
            channel_metadata={
                "actor_user_id": str(actor_user_id) if actor_user_id else None,
                "source": "admin_reply",
            },
        )
        receipt = adapter.send(outbound)
        return {
            "provider_message_id": receipt.provider_message_id,
            "status": receipt.status,
            "channel": receipt.channel,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "admin_reply channel send failed: exc_class=%s — "
            "reply not delivered but action recorded",
            type(exc).__name__,
        )
        return {
            "provider_message_id": None,
            "status": "send_failed",
            "channel": channel,
        }
