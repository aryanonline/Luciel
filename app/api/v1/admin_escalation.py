"""Arc 15 WU3 — per-Instance escalation-CONTACT admin API (Vision §3.4).

Routes under ``/admin/instances/{instance_id}/escalation`` that back the
escalation-contact panel. This surface configures **WHO is notified and
HOW** — never **WHEN** to escalate.

  * GET ""  -- the Instance's stored escalation-contact config + tier
               context (available notify channels, the four fixed
               signals for the UI to render per-signal routing).
  * PUT ""  -- set the escalation-contact config.

The four escalation SIGNALS (explicit_human_request,
cannot_confidently_answer, strong_negative_sentiment, high_value_lead)
are fixed runtime cognition. Any payload that tries to configure trigger
conditions, thresholds, or enable/disable a signal is REJECTED with 422
``escalation_triggers_not_configurable``. There are no trigger toggles.

Tier shape (Vision §3.4):
  * Free       — single ``primary_email``.
  * Pro        — primary + optional secondary contact + per-signal
                 ``routing_rules`` (email/sms).
  * Enterprise — + ordered ``chains`` (contacts + SLA minutes) and
                 slack/custom notify channels.

Layered defences mirror admin_channels.py / admin_personality.py:
  L1 ScopePolicy.enforce_admin_owns_instance — cross-Admin guard.
  L2 PERM_CONFIGURE_CHANNELS (the instance-config gate).
  L3 TenantScopedDbSession — RLS GUC bound.
  L4 admin_audit_log row on every change, in the same txn.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select

from app.api.deps import (
    TenantScopedDbSession,
    get_audit_context,
    get_luciel_instance_service,
)
from app.models.admin import Admin
from app.models.admin_audit_log import (
    ACTION_ESCALATION_CONFIG_UPDATED,
    RESOURCE_INSTANCE_ESCALATION,
)
from app.models.instance import Instance
from app.policy.entitlements import (
    TIER_ENTITLEMENTS,
    TIER_FREE,
    escalation_notify_channels,
)
from app.policy.escalation_config import (
    ESCALATION_SIGNALS,
    validate_escalation_config_for_tier,
)
from app.policy.permissions import PERM_CONFIGURE_CHANNELS, PermissionResolver
from app.policy.scope import ScopePolicy
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)
from app.schemas.escalation import (
    EscalationConfigResponse,
    EscalationConfigUpdate,
)
from app.services.instance_service import InstanceService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/instances/{instance_id}/escalation",
    tags=["admin-escalation"],
)


# =====================================================================
# Helpers (mirror admin_channels.py / admin_personality.py).
# =====================================================================


def _require_admin_id(request: Request) -> str:
    admin_id = getattr(request.state, "admin_id", None)
    if not admin_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No authenticated admin context.",
        )
    return admin_id


def _require_configure_channels(request: Request, *, instance: Instance) -> None:
    if ScopePolicy.is_platform_admin(request):
        return
    resolved = PermissionResolver.resolve(request, instance=instance)
    if PERM_CONFIGURE_CHANNELS not in resolved:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Caller does not hold required permission "
                f"{PERM_CONFIGURE_CHANNELS!r}."
            ),
        )


def _load_active_instance(
    *,
    request: Request,
    instance_id: int,
    instance_service: InstanceService,
) -> Instance:
    instance = instance_service.get_by_pk(instance_id)
    if instance is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Instance {instance_id} not found",
        )
    ScopePolicy.enforce_admin_owns_instance(request, instance)
    if not instance.active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Instance {instance_id} is inactive",
        )
    return instance


def _resolve_admin_tier(db, *, admin_id: str) -> str:
    row = db.execute(
        select(Admin.tier).where(Admin.id == admin_id)
    ).scalar_one_or_none()
    return row if row in TIER_ENTITLEMENTS else TIER_FREE


def _response(
    *, admin_id: str, admin_tier: str, instance: Instance
) -> EscalationConfigResponse:
    return EscalationConfigResponse(
        instance_id=instance.id,
        admin_id=admin_id,
        admin_tier=admin_tier,
        available_notify_channels=sorted(escalation_notify_channels(admin_tier)),
        escalation_signals=sorted(ESCALATION_SIGNALS),
        escalation_config=instance.escalation_config,
        updated_at=instance.updated_at,
    )


# =====================================================================
# Routes.
# =====================================================================


@router.get("", response_model=EscalationConfigResponse)
def get_escalation_config(
    request: Request,
    instance_id: int,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
) -> EscalationConfigResponse:
    """Return the Instance's escalation-contact config + tier context."""
    admin_id = _require_admin_id(request)
    instance = _load_active_instance(
        request=request,
        instance_id=instance_id,
        instance_service=instance_service,
    )
    _require_configure_channels(request, instance=instance)
    admin_tier = _resolve_admin_tier(db, admin_id=admin_id)
    return _response(admin_id=admin_id, admin_tier=admin_tier, instance=instance)


@router.put("", response_model=EscalationConfigResponse)
def put_escalation_config(
    request: Request,
    instance_id: int,
    body: EscalationConfigUpdate,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> EscalationConfigResponse:
    """Set the Instance's escalation-CONTACT config.

    Rejects (422) any attempt to configure escalation TRIGGERS — the
    four signals are fixed runtime cognition. Tier-gates secondary
    contact / routing_rules (Pro+) and chains (Enterprise).
    """
    admin_id = _require_admin_id(request)
    instance = _load_active_instance(
        request=request,
        instance_id=instance_id,
        instance_service=instance_service,
    )
    _require_configure_channels(request, instance=instance)
    admin_tier = _resolve_admin_tier(db, admin_id=admin_id)

    problems = validate_escalation_config_for_tier(
        tier=admin_tier, config=body.config
    )
    if problems:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "escalation_config_invalid",
                "tier": admin_tier,
                "problems": problems,
            },
        )

    before = instance.escalation_config
    instance.escalation_config = body.config or None

    AdminAuditRepository(db).record(
        ctx=audit_ctx,
        admin_id=admin_id,
        action=ACTION_ESCALATION_CONFIG_UPDATED,
        resource_type=RESOURCE_INSTANCE_ESCALATION,
        resource_pk=instance.id,
        resource_natural_id=instance.instance_slug,
        luciel_instance_id=instance.id,
        before=before,
        after=instance.escalation_config,
        note="Escalation-contact config updated (contact + routing only).",
    )

    db.commit()
    db.refresh(instance)
    return _response(admin_id=admin_id, admin_tier=admin_tier, instance=instance)
