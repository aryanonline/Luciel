"""Unit 9 §3.5.4/§3.5.5 — escalation acknowledgement admin API.

  POST /api/v1/admin/escalations/{escalation_id}/ack
      Transitions an escalation event's delivery_status → 'acked' and
      emits the §3.5.5 escalation_acked audit event. Idempotent: a
      second ack on an already-acked event returns 200 without
      duplicating the audit row. Returns 404 if the event is not the
      caller's tenant (existence is not leaked).

The FRONTEND "I'm on it" button is OUT OF SCOPE (Unit 11); this module
is the backend endpoint + status transition only.

Layered defences (mirror admin_handoff.py / admin_escalation.py):
  L1   Tenant fence on admin_id in EscalationDeliveryService.mark_acked.
  L2   Role gate: PERM_CONFIGURE_CHANNELS (the same permission the
       escalation-config routes use), or platform_admin.
  L3   TenantScopedDbSession — RLS GUC bound.
  L4   admin_audit_log row in the same txn as the transition.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app.api.deps import TenantScopedDbSession
from app.policy.permissions import PERM_CONFIGURE_CHANNELS, PermissionResolver
from app.policy.scope import ScopePolicy
from app.services.escalation_delivery_service import EscalationDeliveryService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/escalations",
    tags=["admin-escalation-ack"],
)


class AckResponse(BaseModel):
    escalation_id: int
    delivery_status: str
    message: str


def _require_admin_id(request: Request) -> str:
    admin_id = getattr(request.state, "admin_id", None)
    if not admin_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No authenticated admin context.",
        )
    return admin_id


def _require_actor_user_id(request: Request) -> uuid.UUID | None:
    actor_user_id = getattr(request.state, "actor_user_id", None)
    if actor_user_id is None:
        return None
    if isinstance(actor_user_id, uuid.UUID):
        return actor_user_id
    try:
        return uuid.UUID(str(actor_user_id))
    except (ValueError, AttributeError):
        return None


def _require_ack_permission(request: Request) -> None:
    """Enforce the same permission gate the escalation-config routes use.

    platform_admin always passes. Otherwise the caller must hold
    PERM_CONFIGURE_CHANNELS. The ack target is identified by escalation
    id (not instance id) so the permission is resolved against the
    request context; the tenant fence on admin_id in mark_acked is the
    cross-tenant guard.
    """
    if ScopePolicy.is_platform_admin(request):
        return
    resolved = PermissionResolver.resolve(request)
    if PERM_CONFIGURE_CHANNELS not in resolved:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Caller does not hold required permission "
                f"{PERM_CONFIGURE_CHANNELS!r}."
            ),
        )


@router.post(
    "/{escalation_id}/ack",
    response_model=AckResponse,
    status_code=status.HTTP_200_OK,
)
def admin_ack_escalation(
    request: Request,
    escalation_id: int,
    db: TenantScopedDbSession,
) -> AckResponse:
    """Acknowledge an escalation: delivery_status → 'acked' + audit row.

    Idempotent. Returns 404 if the event is not the caller's tenant.
    """
    admin_id = _require_admin_id(request)
    actor_user_id = _require_actor_user_id(request)
    _require_ack_permission(request)

    svc = EscalationDeliveryService()
    new_status = svc.mark_acked(
        event_id=escalation_id,
        admin_id=admin_id,
        actor_user_id=actor_user_id,
        db=db,
    )
    if new_status is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Escalation {escalation_id} not found.",
        )
    db.commit()

    logger.info(
        "escalation_acked",
        extra={
            "event": "escalation_acked",
            "escalation_id": escalation_id,
            "actor_user_id": str(actor_user_id) if actor_user_id else None,
        },
    )
    return AckResponse(
        escalation_id=escalation_id,
        delivery_status=new_status,
        message="Escalation acknowledged.",
    )
