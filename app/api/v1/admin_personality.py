"""Arc 15 WU3 — per-Instance personality-config admin API (Vision §3.5).

Routes under ``/admin/instances/{instance_id}/personality`` that back
the personality-settings panel:

  * GET ""  -- the Instance's current structured personality config
               (preset / axes / business_context) plus tier context
               (whether ``custom`` is available, the business_context cap).
  * PUT ""  -- set the personality config. Tier gates:
               ``custom`` preset → 403 on Free; ``business_context``
               length → 422 over the tier cap.

Layered defences (mirrors admin_channels.py)
--------------------------------------------
  L1 ScopePolicy.enforce_admin_owns_instance — cross-Admin guard.
  L2 caller must hold PERM_CONFIGURE_CHANNELS (the instance-config gate
     reused from the channel API per the WU3 spec).
  L3 TenantScopedDbSession — RLS GUC bound.
  L4 admin_audit_log row on every change, in the same txn.

This surface exposes NO raw-prompt-authoring hook (Architecture §3.5.1:
"never raw prompt authoring"). The only inputs are a curated preset, the
four bounded custom axes, and framed background ``business_context``.
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
    ACTION_PERSONALITY_UPDATED,
    RESOURCE_INSTANCE_PERSONALITY,
)
from app.models.instance import Instance
from app.policy.entitlements import (
    TIER_ENTITLEMENTS,
    TIER_FREE,
    business_context_max_chars,
    custom_personality_enabled,
)
from app.policy.instance_config import validate_pillars_for_tier
from app.policy.permissions import PERM_CONFIGURE_CHANNELS, PermissionResolver
from app.policy.scope import ScopePolicy
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)
from app.schemas.personality import (
    PersonalityConfigResponse,
    PersonalityConfigUpdate,
)
from app.services.instance_service import InstanceService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/instances/{instance_id}/personality",
    tags=["admin-personality"],
)


# =====================================================================
# Helpers (mirror admin_channels.py).
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
) -> PersonalityConfigResponse:
    return PersonalityConfigResponse(
        instance_id=instance.id,
        admin_id=admin_id,
        admin_tier=admin_tier,
        custom_preset_available=custom_personality_enabled(admin_tier),
        business_context_max_chars=business_context_max_chars(admin_tier),
        personality_preset=instance.personality_preset,
        personality_axes=instance.personality_axes,
        business_context=instance.business_context,
        updated_at=instance.updated_at,
    )


# =====================================================================
# Routes.
# =====================================================================


@router.get("", response_model=PersonalityConfigResponse)
def get_personality_config(
    request: Request,
    instance_id: int,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
) -> PersonalityConfigResponse:
    """Return the Instance's structured personality config + tier context."""
    admin_id = _require_admin_id(request)
    instance = _load_active_instance(
        request=request,
        instance_id=instance_id,
        instance_service=instance_service,
    )
    _require_configure_channels(request, instance=instance)
    admin_tier = _resolve_admin_tier(db, admin_id=admin_id)
    return _response(admin_id=admin_id, admin_tier=admin_tier, instance=instance)


@router.put("", response_model=PersonalityConfigResponse)
def put_personality_config(
    request: Request,
    instance_id: int,
    body: PersonalityConfigUpdate,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> PersonalityConfigResponse:
    """Set the Instance's personality config (preset / axes / context).

    Tier gates: ``custom`` preset → 403 on Free; ``business_context``
    length over the tier cap → 422. Structural validation (axes shape,
    axes-only-when-custom) already ran in the Pydantic layer.
    """
    admin_id = _require_admin_id(request)
    instance = _load_active_instance(
        request=request,
        instance_id=instance_id,
        instance_service=instance_service,
    )
    _require_configure_channels(request, instance=instance)
    admin_tier = _resolve_admin_tier(db, admin_id=admin_id)

    # --- Tier gate: custom preset is a CAPABILITY refusal → 403. ---
    if body.personality_preset == "custom" and not custom_personality_enabled(
        admin_tier
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "custom_preset_not_available_on_tier",
                "tier": admin_tier,
                "message": (
                    "The 'custom' personality preset is available on Pro and "
                    "Enterprise only. Choose one of the named presets."
                ),
                "upgrade_required": True,
            },
        )

    # --- Tier-conditional pillar validation (business_context length). ---
    # custom-preset check is handled above as a 403; business_context length
    # is a 422 (malformed-for-tier payload).
    problems = validate_pillars_for_tier(
        tier=admin_tier,
        personality_preset=body.personality_preset,
        business_context=body.business_context,
    )
    # Drop the custom-preset problem (already enforced as 403) so we don't
    # double-report it as a 422.
    problems = [
        p
        for p in problems
        if p.get("reason") != "custom_preset_not_available_on_tier"
    ]
    if problems:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "personality_config_invalid_for_tier",
                "tier": admin_tier,
                "problems": problems,
            },
        )

    # Re-bind the Instance to the RLS-scoped request session (``db``).
    # ``_load_active_instance`` loads via ``instance_service`` which is
    # constructed on a SEPARATE ``get_db`` session; mutating that copy and
    # then committing/refreshing on ``db`` (TenantScopedDbSession) writes
    # to the wrong unit-of-work (the mutation is silently lost) and the
    # subsequent ``db.refresh`` raises "not persistent within this
    # Session". Re-fetching on ``db`` makes load + mutate + commit + refresh
    # share one session. Scope/tier/active checks already passed above.
    instance = db.execute(
        select(Instance).where(Instance.id == instance_id)
    ).scalar_one()

    before = {
        "personality_preset": instance.personality_preset,
        "personality_axes": instance.personality_axes,
        "business_context_len": len(instance.business_context or ""),
    }

    # Apply. Axes are persisted ONLY for custom (named presets resolve
    # their axis tuple from code, never the DB).
    instance.personality_preset = body.personality_preset
    instance.personality_axes = (
        body.personality_axes if body.personality_preset == "custom" else None
    )
    instance.business_context = body.business_context

    AdminAuditRepository(db).record(
        ctx=audit_ctx,
        admin_id=admin_id,
        action=ACTION_PERSONALITY_UPDATED,
        resource_type=RESOURCE_INSTANCE_PERSONALITY,
        resource_pk=instance.id,
        resource_natural_id=instance.instance_slug,
        luciel_instance_id=instance.id,
        before=before,
        after={
            "personality_preset": instance.personality_preset,
            "personality_axes": instance.personality_axes,
            # Never copy the free-text body into the audit chain; record
            # only its length so the row stays bounded and PII-light.
            "business_context_len": len(instance.business_context or ""),
        },
        note="Personality config updated.",
    )

    db.commit()
    db.refresh(instance)
    return _response(admin_id=admin_id, admin_tier=admin_tier, instance=instance)
