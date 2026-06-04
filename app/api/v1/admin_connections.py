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
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from app.api.deps import (
    TenantScopedDbSession,
    get_audit_context,
    get_luciel_instance_service,
)
from app.core.config import settings as app_settings
from app.integrations.oauth import (
    OAuthError,
    OAuthNotConfiguredError,
    OAuthStateError,
    get_oauth_provider,
    sign_state,
    verify_state,
)
from app.integrations.secrets import SecretStoreError, get_secret_store
from app.models.admin_audit_log import (
    ACTION_CONNECTION_CONFIGURED,
    ACTION_CONNECTION_DISCONNECTED,
    ACTION_CONNECTION_OAUTH_CONNECTED,
    ACTION_CONNECTION_OAUTH_INITIATED,
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
from app.repositories.secret_cleanup_outbox_repository import (
    SecretCleanupOutboxRepository,
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
    OAuthInitiateResponse,
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


# OAuth connector types that the initiate/callback endpoints serve. A
# request for any other connection_type is a 400 (the consent flow only
# applies to OAuth-backed connectors; CSV/webhook configure via POST
# /connections). ``crm`` is included for forward-compatibility — its
# provider returns None today, so initiate honestly 409s "not configured"
# until a CRM provider lands.
_OAUTH_CONNECTION_TYPES: frozenset[str] = frozenset({"calendar", "crm"})


def _secret_name_for(
    *, admin_id: str, instance_id: int, connection_type: str
) -> str:
    """Logical secret name for a connection's stored credential.

    The AwsSecretsManagerStore prefixes ``luciel/connections/`` (see
    ``AwsSecretsManagerStore._name_for``), so the on-AWS secret resolves
    to ``luciel/connections/{admin_id}/{instance_id}/{connection_type}``.
    The store returns the ARN, which is what we persist into
    ``credential_ref`` — the name here is only the put-time logical key.
    """
    return f"{admin_id}/{instance_id}/{connection_type}"


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
        status_detail=result.detail if result.status == "expired" else None,
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


# =====================================================================
# Arc 17 — OAuth initiate + callback (the "ignition" endpoints).
# =====================================================================


@router.post(
    "/instances/{instance_id}/connections/oauth/{connection_type}/initiate",
    response_model=OAuthInitiateResponse,
)
def initiate_oauth_connection(
    request: Request,
    instance_id: int,
    connection_type: str,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> OAuthInitiateResponse:
    """Begin the OAuth consent flow for an OAuth-backed connector.

    Four-walls authorized (admin context + owns-instance + active +
    PERM_CONFIGURE_CONNECTIONS). Mints a signed, tamper-resistant ``state``
    encoding (admin_id, instance_id, connection_type) so the
    (cookie-less) callback can resolve the tenant WITHOUT trusting the
    client. If the connector's OAuth provider is absent or unconfigured
    (no client creds — the deploy/creds gate) the endpoint returns an
    HONEST 409, never a fake redirect. On success it ensures the
    connection row exists in 'unconfigured' (pending consent) and returns
    the provider consent URL for the admin UI to open.
    """
    admin_id = _require_admin_id(request)

    if connection_type not in _OAUTH_CONNECTION_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"connection_type {connection_type!r} is not an OAuth "
                f"connector; OAuth flow serves {sorted(_OAUTH_CONNECTION_TYPES)}."
            ),
        )

    instance = _load_active_instance(
        request=request,
        instance_id=instance_id,
        instance_service=instance_service,
    )
    _require_configure_connections(request, instance=instance)

    provider = get_oauth_provider(connection_type, app_settings)
    if provider is None or not provider.is_configured():
        # Honest deploy/creds gate — never fake a redirect.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "oauth_not_configured",
                "connection_type": connection_type,
                "message": (
                    f"OAuth is not configured for {connection_type!r} "
                    "(client credentials absent). The connector stays "
                    "unconfigured (arc17_pending); connecting is deploy-gated "
                    "on the OAuth client credentials being populated."
                ),
            },
        )

    repo = InstanceConnectionRepository(db)
    row = repo.get_live_by_type(
        admin_id=admin_id,
        instance_id=instance.id,
        connection_type=connection_type,
    )
    if row is None:
        row = repo.configure(
            admin_id=admin_id,
            instance_id=instance.id,
            connection_type=connection_type,
            provider=provider.connection_type,
            status="unconfigured",
            config_json=None,
            credential_ref=None,
            autocommit=False,
        )

    state = sign_state(
        admin_id=admin_id,
        instance_id=instance.id,
        connection_type=connection_type,
        secret=app_settings.oauth_state_signing_secret,
    )
    auth_url = provider.authorization_url(state=state)

    AdminAuditRepository(db).record(
        ctx=audit_ctx,
        admin_id=admin_id,
        action=ACTION_CONNECTION_OAUTH_INITIATED,
        resource_type=RESOURCE_INSTANCE_CONNECTION,
        resource_pk=row.id,
        resource_natural_id=f"{instance.id}:{connection_type}",
        luciel_instance_id=instance.id,
        after={
            "connection_type": connection_type,
            "provider": row.provider,
            "status": row.status,
        },
        note=f"OAuth consent initiated ({connection_type}).",
        autocommit=False,
    )

    db.commit()
    db.refresh(row)
    return OAuthInitiateResponse(
        authorization_url=auth_url,
        state=state,
        connection_id=row.id,
        connection_type=connection_type,
        provider=row.provider,
        status=row.status,
    )


def _callback_redirect(connection_type: str, outcome: str) -> RedirectResponse:
    """Build the post-callback browser redirect to the frontend.

    ``outcome`` is ``connected`` or ``error``. The SPA refetches the
    connections list and toasts the result. 302 so the browser issues a
    fresh GET on the success route.
    """
    base = app_settings.oauth_callback_success_url
    sep = "&" if "?" in base else "?"
    return RedirectResponse(
        url=f"{base}{sep}connection_type={connection_type}&oauth={outcome}",
        status_code=status.HTTP_302_FOUND,
    )


@router.get("/connections/oauth/{connection_type}/callback")
def oauth_callback(
    connection_type: str,
    db: TenantScopedDbSession,
    state: str = "",
    code: str = "",
    error: str = "",
):
    """Google's redirect target — authorizes ENTIRELY off the verified state.

    This endpoint is UNAUTHENTICATED in the session-cookie sense: Google
    redirects the browser here with no cookie. So it MUST NOT trust the
    request for tenant identity — it verifies the signed ``state`` (HMAC +
    TTL), extracts (admin_id, instance_id, connection_type), and only then
    proceeds. A tampered / forged / expired state is a 400 and NEVER
    reaches a token exchange.

    On a verified state it runs the REAL ``provider.exchange_code`` against
    the provider, stores the refresh token via the SecretStore (the ref is
    persisted into ``credential_ref`` — the token VALUE never touches
    Postgres), flips the row to 'connected', and audits
    ACTION_CONNECTION_OAUTH_CONNECTED. On any failure the row goes to
    'error' with an honest audit — never a fake 'connected'.
    """
    # --- Wall: verify the state BEFORE trusting anything. ---
    try:
        verified = verify_state(
            state,
            secret=app_settings.oauth_state_signing_secret,
            max_age_seconds=app_settings.oauth_state_ttl_seconds,
        )
    except OAuthStateError as exc:
        # Cannot resolve a tenant from an unverifiable state → refuse.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_oauth_state",
                "message": f"OAuth state did not verify: {exc}",
            },
        ) from exc

    # The connection_type in the URL must match the signed one — a
    # mismatch means a crafted request reusing a state for another type.
    if verified.connection_type != connection_type:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "oauth_state_type_mismatch",
                "message": (
                    "URL connection_type does not match the signed state."
                ),
            },
        )

    admin_id = verified.admin_id
    instance_id = verified.instance_id
    repo = InstanceConnectionRepository(db)
    audit_repo = AdminAuditRepository(db)
    ctx = AuditContext.system(label="oauth_callback")

    row = repo.get_live_by_type(
        admin_id=admin_id,
        instance_id=instance_id,
        connection_type=connection_type,
    )
    if row is None:
        # The initiate step ensures the row; its absence means a stale or
        # out-of-band state. Honest 404 — do not silently mint a row off
        # a request whose tenant context came only from the state.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "connection_not_found",
                "message": (
                    "No pending connection row for this state; re-initiate "
                    "the OAuth flow."
                ),
            },
        )

    def _fail(detail: str) -> RedirectResponse:
        repo.apply_health_check(
            row=row,
            status="error",
            last_health_check_at=datetime.now(timezone.utc),
            credential_ref=None,
            autocommit=False,
        )
        audit_repo.record(
            ctx=ctx,
            admin_id=admin_id,
            action=ACTION_CONNECTION_OAUTH_CONNECTED,
            resource_type=RESOURCE_INSTANCE_CONNECTION,
            resource_pk=row.id,
            resource_natural_id=f"{instance_id}:{connection_type}",
            luciel_instance_id=instance_id,
            after={
                "connection_type": connection_type,
                "provider": row.provider,
                "status": "error",
                "credential_ref_present": False,
            },
            note=f"OAuth callback failed ({connection_type}): {detail}"[:256],
            autocommit=False,
        )
        db.commit()
        return _callback_redirect(connection_type, "error")

    # Google can redirect with ?error=access_denied (user declined).
    if error:
        return _fail(f"provider returned error={error}")
    if not code:
        return _fail("no authorization code in callback")

    provider = get_oauth_provider(connection_type, app_settings)
    if provider is None or not provider.is_configured():
        return _fail("OAuth provider not configured")

    # --- REAL token exchange against the provider. ---
    try:
        tokens = provider.exchange_code(code=code)
    except OAuthNotConfiguredError:
        return _fail("OAuth provider became unconfigured")
    except OAuthError as exc:
        return _fail(f"token exchange rejected: {exc}")

    refresh_token = tokens.refresh_token
    if not refresh_token:
        # No refresh token → cannot persist a durable credential. Google
        # only re-issues it with access_type=offline + prompt=consent,
        # which authorization_url sets, so this is an honest provider edge.
        return _fail("provider returned no refresh token")

    # --- Store the refresh token; persist ONLY the pointer. ---
    store = get_secret_store(app_settings)
    secret_name = _secret_name_for(
        admin_id=admin_id,
        instance_id=instance_id,
        connection_type=connection_type,
    )
    try:
        credential_ref = store.put(secret_name, refresh_token)
    except SecretStoreError as exc:
        return _fail(f"secret store write failed: {exc}")

    repo.apply_health_check(
        row=row,
        status="connected",
        last_health_check_at=datetime.now(timezone.utc),
        credential_ref=credential_ref,
        autocommit=False,
    )
    audit_repo.record(
        ctx=ctx,
        admin_id=admin_id,
        action=ACTION_CONNECTION_OAUTH_CONNECTED,
        resource_type=RESOURCE_INSTANCE_CONNECTION,
        resource_pk=row.id,
        resource_natural_id=f"{instance_id}:{connection_type}",
        luciel_instance_id=instance_id,
        after={
            "connection_type": connection_type,
            "provider": row.provider,
            "status": "connected",
            "credential_ref_present": True,
        },
        note=f"OAuth callback connected ({connection_type}).",
        autocommit=False,
    )
    db.commit()
    return _callback_redirect(connection_type, "connected")


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
    ``disconnected=False``. When the revoked row carried a non-null
    ``credential_ref`` (a real stored secret — e.g. an OAuth refresh
    token), the same transaction enqueues a secret-cleanup outbox row
    (pointer only). The Celery drain worker performs the actual
    ``SecretStore.delete`` out of band; Postgres never holds the secret
    value, so the enqueued pointer is inert.
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
    # Capture the pointer BEFORE the soft-delete so we can enqueue cleanup.
    credential_ref = row.credential_ref

    disconnected = repo.disconnect(
        admin_id=admin_id,
        connection_id=connection_id,
        autocommit=False,
    )

    secret_cleanup_enqueued = False
    if disconnected and credential_ref:
        SecretCleanupOutboxRepository(db).enqueue(
            admin_id=admin_id,
            credential_ref=credential_ref,
            instance_id=instance_id,
            connection_id=connection_id,
            autocommit=False,
        )
        secret_cleanup_enqueued = True

    AdminAuditRepository(db).record(
        ctx=audit_ctx,
        admin_id=admin_id,
        action=ACTION_CONNECTION_DISCONNECTED,
        resource_type=RESOURCE_INSTANCE_CONNECTION,
        resource_pk=connection_id,
        resource_natural_id=f"{instance_id}:{conn_type}",
        luciel_instance_id=instance_id,
        before={"connection_type": conn_type, "status": row.status},
        after={"secret_cleanup_enqueued": secret_cleanup_enqueued},
        note="Connection disconnected (soft-delete).",
        autocommit=False,
    )

    db.commit()
    return ConnectionDeleteResponse(
        instance_id=instance_id,
        connection_id=connection_id,
        disconnected=disconnected,
        secret_cleanup_enqueued=secret_cleanup_enqueued,
    )
