"""Arc 15 WU4 — per-Instance connection-config admin API (Arc 17 slice).

Routes mounted at ``/admin/connections/...`` (Architecture §1.1) that
back the connection-settings panel:

  * GET    "/instances/{instance_id}/connections"  -- list live rows.
  * POST   "/instances/{instance_id}/connections"  -- configure a row.
  * DELETE "/connections/{connection_id}"           -- soft-delete a row.

This is the REAL Arc 17 ``instance_connections`` contract narrowed in
surface — not a throwaway stub. Full OAuth / secrets-manager / health-
worker flows stay deferred to full Arc 17.

Honesty invariant (non-negotiable, spec §115)
----------------------------------------------
No endpoint returns ``connected`` for a connection with no real backing.
Only CSV (``record_source``) and ``outbound_webhook`` connect LIVE in
this slice → a real ``connected`` row. calendar / crm / email_sender /
sms_sender are DEFERRED → an honest ``unconfigured`` row plus a
structured ``arc17_pending`` payload. ``config_json`` carries NON-SECRET
config ONLY; ``credential_ref`` stays NULL in this slice.

Layered defences (mirrors admin_channels.py / admin_personality.py)
-------------------------------------------------------------------
  L1 ScopePolicy.enforce_admin_owns_instance — cross-Admin guard.
  L2 caller must hold PERM_CONFIGURE_CONNECTIONS (Arc 12b catalog).
  L3 TenantScopedDbSession — RLS GUC bound for the instance_connections
     tenant-isolation fence.
  L4 admin_audit_log row on every configure/disconnect, in the same txn.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import (
    TenantScopedDbSession,
    get_audit_context,
    get_luciel_instance_service,
)
from app.models.admin_audit_log import (
    ACTION_CONNECTION_CONFIGURED,
    ACTION_CONNECTION_DISCONNECTED,
    ACTION_CONNECTION_REFRESHED,
    RESOURCE_INSTANCE_CONNECTION,
)
from app.models.instance import Instance
from app.models.instance_connection import InstanceConnection
from app.policy.permissions import (
    PERM_CONFIGURE_CONNECTIONS,
    PermissionResolver,
)
from app.policy.scope import ScopePolicy
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)
from app.repositories.instance_connection_repository import (
    InstanceConnectionRepository,
)
from app.schemas.connection import (
    DEFERRED_CONNECTION_TYPES,
    LIVE_CONNECTION_TYPES,
    Arc17Pending,
    ConnectionCreate,
    ConnectionCreateResponse,
    ConnectionDeleteResponse,
    ConnectionListResponse,
    ConnectionRefreshResponse,
    ConnectionView,
)
from app.services.connection_health_service import ConnectionHealthService
from app.services.instance_service import InstanceService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin",
    tags=["admin-connections"],
)

# Per-type required NON-SECRET config keys. The route refuses a configure
# whose config_json is missing a required key (422). Keeps the contract
# honest: a LIVE connector needs its real backing reference present.
_REQUIRED_CONFIG_KEYS: dict[str, tuple[str, ...]] = {
    "record_source": ("store_ref",),
    "outbound_webhook": ("url",),
}

# Keys we explicitly REJECT in config_json — anything that looks like a
# secret must not land in the non-secret column (spec §76 / §116).
_FORBIDDEN_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "secret",
        "api_key",
        "apikey",
        "token",
        "access_token",
        "refresh_token",
        "password",
        "client_secret",
        "private_key",
        "auth_header_value",
        "header_value",
        "credential",
        "credentials",
    }
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


def _require_configure_connections(
    request: Request, *, instance: Instance
) -> None:
    """Reject with 403 unless the caller holds PERM_CONFIGURE_CONNECTIONS."""
    if ScopePolicy.is_platform_admin(request):
        return
    resolved = PermissionResolver.resolve(request, instance=instance)
    if PERM_CONFIGURE_CONNECTIONS not in resolved:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Caller does not hold required permission "
                f"{PERM_CONFIGURE_CONNECTIONS!r}."
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


def _view(row: InstanceConnection) -> ConnectionView:
    return ConnectionView(
        id=row.id,
        instance_id=row.instance_id,
        admin_id=row.admin_id,
        connection_type=row.connection_type,
        provider=row.provider,
        status=row.status,
        config_json=row.config_json,
        last_health_check_at=row.last_health_check_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _validate_config_json(
    *, connection_type: str, config_json: dict | None
) -> dict:
    """Enforce the non-secret + required-key invariants on config_json.

    * Reject any forbidden (secret-looking) key → 422.
    * For a LIVE connector, require the per-type backing reference → 422.
    Returns the (possibly empty) config dict to persist.
    """
    cfg = dict(config_json or {})

    offending = sorted(k for k in cfg if k.lower() in _FORBIDDEN_CONFIG_KEYS)
    if offending:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "secret_in_config_json",
                "message": (
                    "config_json carries non-secret config only; secrets "
                    "ride behind credential_ref (NULL in this slice)."
                ),
                "forbidden_keys": offending,
            },
        )

    required = _REQUIRED_CONFIG_KEYS.get(connection_type, ())
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "config_json_missing_required_keys",
                "connection_type": connection_type,
                "missing_keys": missing,
                "message": (
                    f"Connector {connection_type!r} requires config keys "
                    f"{list(required)} to connect."
                ),
            },
        )
    return cfg


# =====================================================================
# Routes.
# =====================================================================


@router.get(
    "/instances/{instance_id}/connections",
    response_model=ConnectionListResponse,
)
def list_connections(
    request: Request,
    instance_id: int,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
) -> ConnectionListResponse:
    """List the live connection rows for an instance."""
    admin_id = _require_admin_id(request)
    instance = _load_active_instance(
        request=request,
        instance_id=instance_id,
        instance_service=instance_service,
    )
    _require_configure_connections(request, instance=instance)

    repo = InstanceConnectionRepository(db)
    rows = repo.list_for_instance(admin_id=admin_id, instance_id=instance.id)
    return ConnectionListResponse(
        instance_id=instance.id,
        admin_id=admin_id,
        connections=[_view(r) for r in rows],
    )


@router.post(
    "/instances/{instance_id}/connections",
    response_model=ConnectionCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def configure_connection(
    request: Request,
    instance_id: int,
    body: ConnectionCreate,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> ConnectionCreateResponse:
    """Configure a connection for an instance.

    CSV (``record_source``) and ``outbound_webhook`` connect LIVE → a
    real ``connected`` row. calendar / crm / email_sender / sms_sender
    are DEFERRED → an honest ``unconfigured`` row plus an
    ``arc17_pending`` marker. The status is decided HERE (the policy
    boundary); the repository never fabricates a status.
    """
    admin_id = _require_admin_id(request)
    instance = _load_active_instance(
        request=request,
        instance_id=instance_id,
        instance_service=instance_service,
    )
    _require_configure_connections(request, instance=instance)

    conn_type = body.connection_type
    cfg = _validate_config_json(
        connection_type=conn_type, config_json=body.config_json
    )

    # --- The honesty fork: LIVE → connected; DEFERRED → unconfigured. ---
    if conn_type in LIVE_CONNECTION_TYPES:
        new_status = "connected"
        pending = None
    elif conn_type in DEFERRED_CONNECTION_TYPES:
        new_status = "unconfigured"
        pending = Arc17Pending(
            connection_type=conn_type,
            message=(
                f"Connecting {conn_type!r} via a live credential flow is "
                "available in Arc 17. The connection has been recorded as "
                "'unconfigured'; dependent tools stay disabled until then."
            ),
        )
    else:  # pragma: no cover — Literal makes this unreachable.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown connection_type {conn_type!r}.",
        )

    repo = InstanceConnectionRepository(db)
    row = repo.configure(
        admin_id=admin_id,
        instance_id=instance.id,
        connection_type=conn_type,
        provider=body.provider,
        status=new_status,
        config_json=cfg or None,
        credential_ref=None,  # secrets flow deferred to full Arc 17.
        autocommit=False,
    )

    AdminAuditRepository(db).record(
        ctx=audit_ctx,
        admin_id=admin_id,
        action=ACTION_CONNECTION_CONFIGURED,
        resource_type=RESOURCE_INSTANCE_CONNECTION,
        resource_pk=row.id,
        resource_natural_id=f"{instance.id}:{conn_type}",
        luciel_instance_id=instance.id,
        after={
            "connection_type": conn_type,
            "provider": body.provider,
            "status": new_status,
            # config_json is non-secret by contract; record only its keys
            # so the audit row stays bounded and free of payload bulk.
            "config_keys": sorted(cfg.keys()),
        },
        note=f"Connection configured ({conn_type}={new_status}).",
        autocommit=False,
    )

    db.commit()
    db.refresh(row)
    return ConnectionCreateResponse(connection=_view(row), arc17_pending=pending)


@router.post(
    "/connections/{connection_id}/refresh",
    response_model=ConnectionRefreshResponse,
)
def refresh_connection(
    request: Request,
    connection_id: int,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> ConnectionRefreshResponse:
    """Manually re-verify a connection (Arc 17 brief deliverable #2).

    Loads the live row fenced to the admin (404 if absent / not theirs),
    re-enforces PERM_CONFIGURE_CONNECTIONS on the owning instance, then
    runs the shared health-check / token-refresh path:

      * LIVE connector (record_source / outbound_webhook) → reachability
        probe → ``connected`` / ``error`` + a fresh ``last_health_check_at``.
      * DEFERRED OAuth connector → silent token refresh. The live flow is
        DEPLOY-GATED on OAuth client creds; absent them the row stays an
        HONEST ``unconfigured`` + ``arc17_pending`` — NEVER a fake
        ``connected``.

    The honest status is decided by ConnectionHealthService; this route
    persists it + writes one ACTION_CONNECTION_REFRESHED audit row in the
    same transaction.
    """
    from app.core.config import settings

    admin_id = _require_admin_id(request)
    repo = InstanceConnectionRepository(db)

    row = repo.get_live_for_admin(
        admin_id=admin_id, connection_id=connection_id
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Connection {connection_id} not found",
        )

    instance = _load_active_instance(
        request=request,
        instance_id=row.instance_id,
        instance_service=instance_service,
    )
    _require_configure_connections(request, instance=instance)

    result = ConnectionHealthService(settings).check_health(row)

    repo.apply_health_check(
        row=row,
        status=result.status,
        last_health_check_at=result.checked_at,
        credential_ref=result.new_credential_ref,
        autocommit=False,
    )

    pending = None
    if result.arc17_pending:
        pending = Arc17Pending(
            connection_type=row.connection_type,
            message=result.detail
            or (
                f"{row.connection_type} stays unconfigured "
                "(live credential flow deferred to Arc 17 deploy)."
            ),
        )

    AdminAuditRepository(db).record(
        ctx=audit_ctx,
        admin_id=admin_id,
        action=ACTION_CONNECTION_REFRESHED,
        resource_type=RESOURCE_INSTANCE_CONNECTION,
        resource_pk=row.id,
        resource_natural_id=f"{row.instance_id}:{row.connection_type}",
        luciel_instance_id=row.instance_id,
        after={
            "connection_type": row.connection_type,
            "status": result.status,
            "arc17_pending": result.arc17_pending,
        },
        note=f"Connection refreshed ({row.connection_type}={result.status}).",
        autocommit=False,
    )

    db.commit()
    db.refresh(row)
    return ConnectionRefreshResponse(
        connection=_view(row),
        arc17_pending=pending,
        detail=result.detail,
    )


@router.delete(
    "/connections/{connection_id}",
    response_model=ConnectionDeleteResponse,
)
def disconnect_connection(
    request: Request,
    connection_id: int,
    db: TenantScopedDbSession,
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> ConnectionDeleteResponse:
    """Soft-delete (revoke) a connection row, fenced to the admin.

    Idempotent: re-disconnecting an already-revoked row returns
    ``disconnected=False``. Full Arc 17 will enqueue secret cleanup here
    (TODO) once live credential flows land; this slice stores no secrets,
    so there is nothing to scrub.
    """
    admin_id = _require_admin_id(request)
    repo = InstanceConnectionRepository(db)

    # Load first so we can scope the audit row to the right instance and
    # 404 cleanly when the row is not the caller's (Wall-1 via repo).
    row = repo.get_live_for_admin(admin_id=admin_id, connection_id=connection_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Connection {connection_id} not found",
        )

    instance_id = row.instance_id
    conn_type = row.connection_type

    disconnected = repo.disconnect(
        admin_id=admin_id,
        connection_id=connection_id,
        autocommit=False,
    )

    AdminAuditRepository(db).record(
        ctx=audit_ctx,
        admin_id=admin_id,
        action=ACTION_CONNECTION_DISCONNECTED,
        resource_type=RESOURCE_INSTANCE_CONNECTION,
        resource_pk=connection_id,
        resource_natural_id=f"{instance_id}:{conn_type}",
        luciel_instance_id=instance_id,
        before={"connection_type": conn_type, "status": row.status},
        note="Connection disconnected (soft-delete).",
        autocommit=False,
    )

    db.commit()
    return ConnectionDeleteResponse(
        instance_id=instance_id,
        connection_id=connection_id,
        disconnected=disconnected,
    )
