"""Arc 13 D5 — per-Instance channel-configuration admin API.

Routes under ``/admin/instances/{instance_id}/channels`` that back the
channel-settings panel: read the current channel state, and enable /
disable the email and SMS surfaces. The widget is the entitlement floor
(always on, no provisioning) and is therefore not toggleable here.

  * GET  ""                  -- current channel state for the Instance:
                                per-channel {enabled, tier_available}
                                plus the SMS provisioned number/mode.
  * PUT  "/email"            -- enable/disable the email surface. Adds /
                                removes 'email' from enabled_channels.
  * PUT  "/sms"              -- enable/disable the SMS surface. Enable on
                                a non-entitled tier (Free) is REJECTED at
                                this API boundary with 403
                                ``channel_not_available_on_tier``. Enable
                                on Pro/Enterprise PROVISIONS a dedicated
                                number (PhoneNumberProvisioningService);
                                disable DEPROVISIONS it.

Layered defences (mirrors admin_tools.py)
-----------------------------------------
  L1 ScopePolicy.enforce_admin_owns_instance — cross-Admin guard.
  L2 caller must hold PERM_CONFIGURE_CHANNELS (permission-based gate).
  L3 TenantScopedDbSession — RLS GUC bound for channel_routes fences.
  L4 admin_audit_log row on every enable/disable, in the same txn.

Free-tier SMS reject shape (DOD contract)
-----------------------------------------
HTTP 403 with body::

    {"detail": {
        "error": "channel_not_available_on_tier",
        "channel": "sms",
        "tier": "free",
        "message": "SMS is not available on the free tier. Upgrade to
                    Pro or Enterprise to enable SMS.",
        "upgrade_required": true
    }}
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import (
    TenantScopedDbSession,
    get_audit_context,
    get_luciel_instance_service,
)
from app.channels.provisioning import (
    SMS_MODE_DEDICATED,
    PhoneNumberProvisioningService,
    TierNotEntitledError,
)
from app.core.config import settings
from app.models.admin import Admin
from app.models.admin_audit_log import (
    ACTION_CHANNEL_DISABLED,
    ACTION_CHANNEL_ENABLED,
    RESOURCE_INSTANCE_CHANNEL,
)
from app.models.channel_route import CHANNEL_EMAIL as ROUTE_CHANNEL_EMAIL
from app.models.channel_route import ChannelRoute
from app.models.instance import Instance
from app.policy.entitlements import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    CHANNEL_WIDGET,
    TIER_ENTITLEMENTS,
    TIER_FREE,
    channels_available,
)
from app.policy.permissions import PERM_CONFIGURE_CHANNELS, PermissionResolver
from app.policy.scope import ScopePolicy
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)
from app.services.instance_service import InstanceService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/instances/{instance_id}/channels",
    tags=["admin-channels"],
)

# Platform-default inbound mail domain used to derive an Instance's
# default email address when ``settings.mail_inbound_domain`` is unset
# (dev / CI boot-safe default is ""). This is the canonical platform
# subdomain root documented in the ChannelRoute model / Architecture
# §3.1.3 — deriving against it keeps email-enable boot-safe (the toggle
# never fails for an empty setting) and the address deterministic.
_DEFAULT_MAIL_INBOUND_DOMAIN = "luciel-mail.com"


# =====================================================================
# Pydantic schemas.
# =====================================================================


class ChannelView(BaseModel):
    """One channel's state in the GET response."""

    channel: str
    enabled: bool
    tier_available: bool


class ChannelStateResponse(BaseModel):
    """GET response: per-channel state + SMS provisioning detail."""

    instance_id: int
    admin_id: str
    admin_tier: str
    channels: list[ChannelView]
    sms_provisioned_number: str | None
    sms_number_mode: str | None


class ChannelToggleRequest(BaseModel):
    """PUT body for enable/disable."""

    enabled: bool


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


def _require_configure_channels(request: Request, *, instance: Instance) -> None:
    """Reject with 403 unless the caller holds PERM_CONFIGURE_CHANNELS."""
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


def _enabled_set(instance: Instance) -> set[str]:
    enabled = set(instance.enabled_channels or ())
    enabled.add(CHANNEL_WIDGET)  # widget is the structural floor
    return enabled


def _default_email_address(*, admin_id: str, instance: Instance) -> str:
    """Derive the platform-default inbound email address for an Instance.

    Shape (Architecture §3.1.3): ``instance-slug@admin-slug.<domain>``.
    ``admins.id`` IS the semantic admin slug; ``instance_slug`` is unique
    within an Admin, so the pair is globally unique. The address is the
    fully-qualified lowercase form stored in ``ChannelRoute.route_value``
    — matching how the EmailChannelAdapter lowercases the inbound
    recipient before lookup. When ``settings.mail_inbound_domain`` is
    empty (dev/CI default), fall back to the canonical platform domain so
    the toggle stays boot-safe and the address deterministic. Custom-domain
    addresses are OUT OF SCOPE here (Connections layer, §3.8 / Arc 17).
    """
    domain = settings.mail_inbound_domain or _DEFAULT_MAIL_INBOUND_DOMAIN
    return f"{instance.instance_slug}@{admin_id}.{domain}".lower()


def _mint_default_email_route(
    *, db, instance: Instance, admin_id: str
) -> ChannelRoute:
    """Mint (or reuse) the live default-address email ChannelRoute.

    Idempotent and re-use-aware, mirroring how SMS handles ChannelRoute:
      * a live route for this Instance at the default address → reuse it;
      * a soft-revoked route at the default address owned by this
        Instance → un-revoke it (clear ``revoked_at``) rather than insert
        a duplicate;
      * otherwise INSERT a fresh live route.

    Uniqueness: the ``uq_channel_routes_live_value`` partial index allows
    one live route per (channel, route_value). If a *different* Instance
    already owns that exact live address, fail loudly (HTTP 409) rather
    than silently steal it.
    """
    address = _default_email_address(admin_id=admin_id, instance=instance)

    existing = (
        db.query(ChannelRoute)
        .filter(
            ChannelRoute.channel == ROUTE_CHANNEL_EMAIL,
            ChannelRoute.route_value == address,
            ChannelRoute.revoked_at.is_(None),
        )
        .first()
    )
    if existing is not None:
        if existing.luciel_instance_id != instance.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "email_address_already_routed",
                    "channel": CHANNEL_EMAIL,
                    "address": address,
                    "message": (
                        f"Email address {address!r} is already routed to a "
                        "different Instance."
                    ),
                },
            )
        return existing  # already live for this Instance — idempotent.

    # Reuse a soft-revoked row for this Instance at the same address.
    revoked = (
        db.query(ChannelRoute)
        .filter(
            ChannelRoute.channel == ROUTE_CHANNEL_EMAIL,
            ChannelRoute.route_value == address,
            ChannelRoute.luciel_instance_id == instance.id,
            ChannelRoute.admin_id == admin_id,
            ChannelRoute.revoked_at.is_not(None),
        )
        .order_by(ChannelRoute.id.desc())
        .first()
    )
    if revoked is not None:
        revoked.revoked_at = None
        db.flush()
        return revoked

    route = ChannelRoute(
        admin_id=admin_id,
        luciel_instance_id=instance.id,
        channel=ROUTE_CHANNEL_EMAIL,
        route_value=address,
    )
    db.add(route)
    db.flush()
    return route


def _revoke_default_email_route(
    *, db, instance: Instance, admin_id: str
) -> ChannelRoute | None:
    """Soft-revoke the live default-address email ChannelRoute, if any.

    Mirrors SMS deprovision: set ``revoked_at`` so the historical row is
    kept (append-only audit) but stops being live/unique. No-op when no
    live route exists.
    """
    address = _default_email_address(admin_id=admin_id, instance=instance)
    route = (
        db.query(ChannelRoute)
        .filter(
            ChannelRoute.channel == ROUTE_CHANNEL_EMAIL,
            ChannelRoute.route_value == address,
            ChannelRoute.luciel_instance_id == instance.id,
            ChannelRoute.admin_id == admin_id,
            ChannelRoute.revoked_at.is_(None),
        )
        .first()
    )
    if route is None:
        return None
    route.revoked_at = datetime.now(timezone.utc)
    db.flush()
    return route


# =====================================================================
# Routes.
# =====================================================================


@router.get("", response_model=ChannelStateResponse)
def get_channel_state(
    request: Request,
    instance_id: int,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
) -> ChannelStateResponse:
    """Return the Instance's per-channel state + SMS provisioning detail."""
    admin_id = _require_admin_id(request)
    instance = _load_active_instance(
        request=request,
        instance_id=instance_id,
        instance_service=instance_service,
    )
    _require_configure_channels(request, instance=instance)

    admin_tier = _resolve_admin_tier(db, admin_id=admin_id)
    tier_channels = channels_available(admin_tier)
    enabled = _enabled_set(instance)

    channels = [
        ChannelView(
            channel=ch,
            enabled=ch in enabled,
            tier_available=ch in tier_channels,
        )
        for ch in (CHANNEL_WIDGET, CHANNEL_EMAIL, CHANNEL_SMS)
    ]
    return ChannelStateResponse(
        instance_id=instance.id,
        admin_id=admin_id,
        admin_tier=admin_tier,
        channels=channels,
        sms_provisioned_number=instance.sms_provisioned_number,
        sms_number_mode=instance.sms_number_mode,
    )


@router.put("/email", response_model=ChannelStateResponse)
def set_email_channel(
    request: Request,
    instance_id: int,
    body: ChannelToggleRequest,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> ChannelStateResponse:
    """Enable or disable the email surface for an Instance."""
    admin_id = _require_admin_id(request)
    instance = _load_active_instance(
        request=request,
        instance_id=instance_id,
        instance_service=instance_service,
    )
    _require_configure_channels(request, instance=instance)

    admin_tier = _resolve_admin_tier(db, admin_id=admin_id)
    _toggle_simple_channel(
        db=db,
        instance=instance,
        admin_id=admin_id,
        admin_tier=admin_tier,
        channel=CHANNEL_EMAIL,
        enabled=body.enabled,
        audit_ctx=audit_ctx,
    )
    db.commit()
    db.refresh(instance)
    return _state_response(
        admin_id=admin_id, admin_tier=admin_tier, instance=instance
    )


@router.put("/sms", response_model=ChannelStateResponse)
def set_sms_channel(
    request: Request,
    instance_id: int,
    body: ChannelToggleRequest,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> ChannelStateResponse:
    """Enable or disable the SMS surface for an Instance.

    Enable on a non-entitled tier (Free) is REJECTED here with 403
    ``channel_not_available_on_tier`` (the API boundary reject; the
    provisioning service refuses too as defence in depth). Enable on
    Pro/Enterprise PROVISIONS a dedicated number; disable DEPROVISIONS.
    """
    admin_id = _require_admin_id(request)
    instance = _load_active_instance(
        request=request,
        instance_id=instance_id,
        instance_service=instance_service,
    )
    _require_configure_channels(request, instance=instance)

    admin_tier = _resolve_admin_tier(db, admin_id=admin_id)

    if body.enabled:
        # --- API-boundary tier reject for Free (and any non-entitled). ---
        if CHANNEL_SMS not in channels_available(admin_tier):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "channel_not_available_on_tier",
                    "channel": CHANNEL_SMS,
                    "tier": admin_tier,
                    "message": (
                        f"SMS is not available on the {admin_tier} tier. "
                        "Upgrade to Pro or Enterprise to enable SMS."
                    ),
                    "upgrade_required": True,
                },
            )

        prov = PhoneNumberProvisioningService(db)
        try:
            result = prov.provision(
                admin_id=admin_id,
                instance_id=instance.id,
                tier=admin_tier,
                mode=SMS_MODE_DEDICATED,
                audit_ctx=audit_ctx,
            )
        except TierNotEntitledError as e:
            # Defence in depth — should be unreachable past the gate above.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "channel_not_available_on_tier",
                    "channel": CHANNEL_SMS,
                    "tier": admin_tier,
                    "message": str(e),
                    "upgrade_required": True,
                },
            ) from e

        enabled = set(instance.enabled_channels or ())
        enabled.add(CHANNEL_SMS)
        instance.enabled_channels = sorted(enabled)

        audit = AdminAuditRepository(db)
        audit.record(
            ctx=audit_ctx,
            admin_id=admin_id,
            action=ACTION_CHANNEL_ENABLED,
            resource_type=RESOURCE_INSTANCE_CHANNEL,
            resource_pk=result.route_id,
            resource_natural_id=f"{instance.id}:{CHANNEL_SMS}",
            luciel_instance_id=instance.id,
            after={
                "channel": CHANNEL_SMS,
                "number": result.e164,
                "mode": result.mode,
                "provider": result.provider,
            },
            note="SMS channel enabled (number provisioned).",
        )
    else:
        # Disable → deprovision (idempotent no-op if no number).
        prov = PhoneNumberProvisioningService(db)
        prov.deprovision(
            admin_id=admin_id,
            instance_id=instance.id,
            audit_ctx=audit_ctx,
        )
        enabled = set(instance.enabled_channels or ())
        enabled.discard(CHANNEL_SMS)
        instance.enabled_channels = sorted(enabled)

        audit = AdminAuditRepository(db)
        audit.record(
            ctx=audit_ctx,
            admin_id=admin_id,
            action=ACTION_CHANNEL_DISABLED,
            resource_type=RESOURCE_INSTANCE_CHANNEL,
            resource_natural_id=f"{instance.id}:{CHANNEL_SMS}",
            luciel_instance_id=instance.id,
            before={"channel": CHANNEL_SMS},
            note="SMS channel disabled (number deprovisioned).",
        )

    db.commit()
    db.refresh(instance)
    return _state_response(
        admin_id=admin_id, admin_tier=admin_tier, instance=instance
    )


# =====================================================================
# Internal.
# =====================================================================


def _toggle_simple_channel(
    *,
    db,
    instance: Instance,
    admin_id: str,
    admin_tier: str,
    channel: str,
    enabled: bool,
    audit_ctx: AuditContext,
) -> None:
    """Enable/disable a non-provisioned channel (email).

    Enable on a non-entitled tier is rejected with the same 403 shape as
    SMS. No number provisioning, but email DOES need its inbound
    ChannelRoute to be end-to-end: on enable we mint (or reuse) the
    platform-default address route ``instance-slug@admin-slug.<domain>``
    so an inbound email addressed there resolves to this Instance; on
    disable we soft-revoke it. Custom-domain addresses stay out of scope
    (Connections layer §3.8 / Arc 17).
    """
    if enabled and channel not in channels_available(admin_tier):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "channel_not_available_on_tier",
                "channel": channel,
                "tier": admin_tier,
                "message": (
                    f"{channel} is not available on the {admin_tier} tier. "
                    "Upgrade to Pro or Enterprise to enable it."
                ),
                "upgrade_required": True,
            },
        )

    current = set(instance.enabled_channels or ())
    if enabled:
        current.add(channel)
    else:
        current.discard(channel)
    instance.enabled_channels = sorted(current)

    route_address: str | None = None
    route_pk: int | None = None
    if channel == CHANNEL_EMAIL:
        if enabled:
            route = _mint_default_email_route(
                db=db, instance=instance, admin_id=admin_id
            )
            route_address = route.route_value
            route_pk = route.id
        else:
            revoked = _revoke_default_email_route(
                db=db, instance=instance, admin_id=admin_id
            )
            if revoked is not None:
                route_address = revoked.route_value
                route_pk = revoked.id

    payload = {"channel": channel, "enabled": enabled}
    if route_address is not None:
        payload["address"] = route_address

    audit = AdminAuditRepository(db)
    audit.record(
        ctx=audit_ctx,
        admin_id=admin_id,
        action=ACTION_CHANNEL_ENABLED if enabled else ACTION_CHANNEL_DISABLED,
        resource_type=RESOURCE_INSTANCE_CHANNEL,
        resource_pk=route_pk,
        resource_natural_id=f"{instance.id}:{channel}",
        luciel_instance_id=instance.id,
        after=payload if enabled else None,
        before=None if enabled else payload,
        note=f"{channel} channel {'enabled' if enabled else 'disabled'}.",
    )


def _state_response(
    *, admin_id: str, admin_tier: str, instance: Instance
) -> ChannelStateResponse:
    tier_channels = channels_available(admin_tier)
    enabled = _enabled_set(instance)
    channels = [
        ChannelView(
            channel=ch,
            enabled=ch in enabled,
            tier_available=ch in tier_channels,
        )
        for ch in (CHANNEL_WIDGET, CHANNEL_EMAIL, CHANNEL_SMS)
    ]
    return ChannelStateResponse(
        instance_id=instance.id,
        admin_id=admin_id,
        admin_tier=admin_tier,
        channels=channels,
        sms_provisioned_number=instance.sms_provisioned_number,
        sms_number_mode=instance.sms_number_mode,
    )
