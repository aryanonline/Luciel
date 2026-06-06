"""Arc 15 WU4 — per-Instance connection-config admin API (Arc 17 slice).

Routes mounted at ``/admin/connections/...`` (Architecture §1.1) that
back the connection-settings panel:

  * GET    "/instances/{instance_id}/connections"  -- list live rows.
  * POST   "/instances/{instance_id}/connections"  -- configure a row.
  * DELETE "/connections/{connection_id}"           -- soft-delete a row.

This is the REAL ``instance_connections`` contract. The OAuth
initiate/callback connect paths, the secrets-store integration, the
§3.8.5 auth_class fork, and the §3.8.5 health/refresh worker are BUILT
(Unit 13c); what remains environment-dependent is the live provider
credential / consent, which is deploy-gated, NOT unbuilt.

Honesty invariant (non-negotiable, spec §115)
----------------------------------------------
No endpoint returns ``connected`` for a connection with no real backing.
A connection is ``connected`` when its credential SHAPE is present —
``api_key`` config present (record_source / outbound_webhook),
``provisioned_resource`` platform sender identity present (email_sender /
sms_sender), ``oauth_token`` consent completed (calendar / crm). When the
live credential/consent is absent it is an honest ``unconfigured`` row
plus a structured ``arc17_pending`` payload that marks the connection as
DEPLOY-GATED pending (the connect path is built; it needs live provider
credentials to succeed in this environment). The ``arc17_pending`` field
NAME is retained for API stability (renaming is a frontend-visible
contract change, out of scope); it means "deploy-gated pending", not
"feature not built". ``non_secret_config`` carries NON-SECRET config
ONLY; ``secret_ref`` is NULL for shapes that have no per-tenant secret.

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
    ACTION_CONNECTION_STATUS_CHANGED,
    RESOURCE_INSTANCE_CONNECTION,
)
from app.models.instance import Instance
from app.connections.instance_connection import (
    CONNECTION_TYPES,
    InstanceConnection,
    auth_class_for,
)
from app.policy.permissions import (
    PERM_CONFIGURE_CONNECTIONS,
    PermissionResolver,
)
from app.policy.scope import ScopePolicy
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)
from app.connections.repository import (
    InstanceConnectionRepository,
)
from app.connections.secret_cleanup_outbox_repository import (
    SecretCleanupOutboxRepository,
)
from app.schemas.connection import (
    Arc17Pending,
    ConnectionCreate,
    ConnectionCreateResponse,
    ConnectionDeleteResponse,
    ConnectionListResponse,
    ConnectionRefreshResponse,
    ConnectionView,
    ConnectorCatalogEntry,
    ConnectorCatalogResponse,
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
# whose non_secret_config is missing a required key (422). Keeps the contract
# honest: a LIVE connector needs its real backing reference present.
_REQUIRED_CONFIG_KEYS: dict[str, tuple[str, ...]] = {
    "record_source": ("store_ref",),
    "outbound_webhook": ("url",),
}

# Keys we explicitly REJECT in non_secret_config — anything that looks like a
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
    ``secret_ref`` — the name here is only the put-time logical key.
    """
    return f"{admin_id}/{instance_id}/{connection_type}"


def _provisioned_resource_identity(
    settings, connection_type: str
) -> dict | None:
    """Return the NON-SECRET sender identity for a provisioned_resource
    connector when the platform resource is present in config, else None.

    ``provisioned_resource`` connectors (email_sender / sms_sender)
    authenticate via the platform's own transport (SES / Twilio), not a
    per-tenant credential. The "connect" verification is therefore a
    config-presence check of the platform-owned sender identity — mirrors
    the ``is_configured()`` gate the send_email / send_sms tools already
    apply (see send_email_tool / send_sms_tool). The returned dict carries
    ONLY non-secret identifiers (the From address/name, the Twilio account
    SID + messaging-service SID) — NEVER the Twilio auth token or any
    secret, which stay in settings/SSM and never land in non_secret_config.
    """
    if connection_type == "email_sender":
        from_address = settings.email_sender_from_address
        if not from_address:
            return None
        identity = {"from_address": from_address}
        if settings.email_sender_from_name:
            identity["from_name"] = settings.email_sender_from_name
        return identity
    if connection_type == "sms_sender":
        # Mirror send_sms_tool's gate: account_sid + auth_token must be
        # present for a live send. auth_token is a SECRET — it is the gate
        # but NEVER part of the non-secret identity we persist.
        if not (settings.twilio_account_sid and settings.twilio_auth_token):
            return None
        identity = {"account_sid": settings.twilio_account_sid}
        if settings.twilio_messaging_service_sid:
            identity["messaging_service_sid"] = (
                settings.twilio_messaging_service_sid
            )
        return identity
    return None


def _connector_is_ready(settings, connection_type: str) -> bool:
    """Whether ``connection_type`` CAN connect live in this environment.

    Read-only, no per-tenant data, no secrets, no network — purely a
    config-presence check keyed on the §3.8.5 auth_class:
      * ``api_key``              → always ready (per-tenant config supplies
                                    the backing at configure time).
      * ``provisioned_resource`` → platform sender identity present (same
                                    gate as the configure path /
                                    ``_provisioned_resource_identity``).
      * ``oauth_token``          → OAuth client creds configured (same
                                    ``provider.is_configured()`` gate the
                                    connect path uses).
    """
    klass = auth_class_for(connection_type)
    if klass == "api_key":
        return True
    if klass == "provisioned_resource":
        return _provisioned_resource_identity(settings, connection_type) is not None
    if klass == "oauth_token":
        provider = get_oauth_provider(connection_type, settings)
        return provider is not None and provider.is_configured()
    return False


def _view(row: InstanceConnection) -> ConnectionView:
    return ConnectionView(
        id=row.id,
        instance_id=row.instance_id,
        admin_id=row.admin_id,
        connection_type=row.connection_type,
        provider=row.provider,
        status=row.status,
        auth_class=row.auth_class,
        non_secret_config=row.non_secret_config,
        last_health_check_at=row.last_health_check_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _validate_non_secret_config(
    *, connection_type: str, non_secret_config: dict | None
) -> dict:
    """Enforce the non-secret + required-key invariants on non_secret_config.

    * Reject any forbidden (secret-looking) key → 422.
    * For a LIVE connector, require the per-type backing reference → 422.
    Returns the (possibly empty) config dict to persist.
    """
    cfg = dict(non_secret_config or {})

    offending = sorted(k for k in cfg if k.lower() in _FORBIDDEN_CONFIG_KEYS)
    if offending:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "secret_in_non_secret_config",
                "message": (
                    "non_secret_config carries non-secret config only; secrets "
                    "ride behind secret_ref (NULL in this slice)."
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
                "error": "non_secret_config_missing_required_keys",
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


@router.get(
    "/connections/catalog",
    response_model=ConnectorCatalogResponse,
)
def connector_catalog(request: Request) -> ConnectorCatalogResponse:
    """Read-only connector-readiness catalog for the deploy environment.

    Returns one entry per supported ``connection_type`` with its §3.8.5
    ``auth_class`` and whether the connector CAN connect live here
    (``is_ready``). Static + tenant-agnostic: no per-tenant data, no
    secrets, no network — just a config-presence check (see
    ``_connector_is_ready``). Requires an authenticated admin context like
    the other connections routes. The UI uses it to render which
    connectors are available to configure in this environment.
    """
    _require_admin_id(request)
    entries = [
        ConnectorCatalogEntry(
            connection_type=ct,
            auth_class=auth_class_for(ct),
            is_ready=_connector_is_ready(app_settings, ct),
        )
        for ct in CONNECTION_TYPES
    ]
    return ConnectorCatalogResponse(connectors=entries)


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

    The status is decided HERE (the policy boundary, driven by the §3.8.5
    auth_class) — the repository never fabricates a status:

      * ``api_key`` (record_source / outbound_webhook): config present →
        ``connected``.
      * ``provisioned_resource`` (email_sender / sms_sender): platform
        sender identity present in settings → ``connected``; absent →
        honest ``unconfigured`` + a deploy-gated ``arc17_pending`` marker.
      * ``oauth_token`` (calendar / crm): POST configure is NOT the connect
        path — OAuth connects via the initiate/callback consent flow, so
        this records an honest ``unconfigured`` row pointing the caller at
        the initiate endpoint (live consent is deploy-gated on the OAuth
        client credentials).

    ``arc17_pending`` means "deploy-gated pending" (the connect path is
    built; it needs the live credential/consent), NOT "feature not built".
    """
    admin_id = _require_admin_id(request)
    instance = _load_active_instance(
        request=request,
        instance_id=instance_id,
        instance_service=instance_service,
    )
    _require_configure_connections(request, instance=instance)

    conn_type = body.connection_type
    cfg = _validate_non_secret_config(
        connection_type=conn_type, non_secret_config=body.non_secret_config
    )

    # --- The honesty fork, driven by the §3.8.5 auth_class (NOT a
    # hardcoded LIVE/DEFERRED list). Each credential SHAPE has its own
    # honest connect path; NONE ever fabricates 'connected' without a
    # real backing.
    #
    #   api_key            (record_source / outbound_webhook):
    #       config-presence already enforced by _validate_non_secret_config
    #       (the required store_ref / url is present) → connected.
    #   provisioned_resource (email_sender / sms_sender):
    #       verify the platform-owned sender identity is present in
    #       settings → connected + the NON-SECRET identity recorded in
    #       non_secret_config; absent → unconfigured (honest, no fake).
    #   oauth_token        (calendar / crm):
    #       POST configure is NOT the connect path — OAuth connects via the
    #       initiate/callback consent flow. Record an unconfigured row and
    #       point the caller at the initiate endpoint. NEVER connected here.
    klass = auth_class_for(conn_type)
    pending: Arc17Pending | None = None

    if klass == "api_key":
        new_status = "connected"
    elif klass == "provisioned_resource":
        identity = _provisioned_resource_identity(app_settings, conn_type)
        if identity is not None:
            new_status = "connected"
            # Fold the verified non-secret sender identity into the config
            # we persist (request body may add allowlist hints; the
            # platform identity is authoritative for the live send).
            cfg = {**cfg, **identity}
        else:
            new_status = "unconfigured"
            pending = Arc17Pending(
                connection_type=conn_type,
                message=(
                    f"{conn_type} is not yet provisioned on the platform "
                    "(sender identity absent in config); the connection is "
                    "recorded as 'unconfigured'. Provisioning the sender "
                    "identity is deploy-gated."
                ),
            )
    elif klass == "oauth_token":
        new_status = "unconfigured"
        pending = Arc17Pending(
            connection_type=conn_type,
            message=(
                f"{conn_type} connects via the OAuth consent flow: POST "
                f"/admin/instances/{instance.id}/connections/oauth/"
                f"{conn_type}/initiate, then complete consent. The "
                "connection is recorded as 'unconfigured' until consent "
                "completes; live consent is deploy-gated on the OAuth "
                "client credentials."
            ),
        )
    else:  # pragma: no cover — auth_class_for pins the four-value vocab.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unhandled auth_class {klass!r} for {conn_type!r}.",
        )

    repo = InstanceConnectionRepository(db)
    row = repo.configure(
        admin_id=admin_id,
        instance_id=instance.id,
        connection_type=conn_type,
        provider=body.provider,
        status=new_status,
        non_secret_config=cfg or None,
        secret_ref=None,  # provisioned_resource has no per-tenant secret.
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
            "auth_class": klass,
            # non_secret_config is non-secret by contract; record only its keys
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

      * api_key connector (record_source / outbound_webhook) →
        reachability probe → ``connected`` / ``error`` + a fresh
        ``last_health_check_at``.
      * OAuth connector → silent token refresh. The live flow is
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
        secret_ref=result.new_secret_ref,
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
                "(live credential flow deploy-gated on provider creds)."
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
            non_secret_config=None,
            secret_ref=None,
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
    persisted into ``secret_ref`` — the token VALUE never touches
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

    # Capture the prior status BEFORE any mutation so the status-changed
    # audit can record the honest old→new transition (§3.8.5 timeline).
    prior_status = row.status

    def _emit_status_changed(new_status: str) -> None:
        """Emit the §3.8.5 status-transition audit (old→new) when the
        callback moved the connection's status. No-op if unchanged."""
        if new_status == prior_status:
            return
        audit_repo.record(
            ctx=ctx,
            admin_id=admin_id,
            action=ACTION_CONNECTION_STATUS_CHANGED,
            resource_type=RESOURCE_INSTANCE_CONNECTION,
            resource_pk=row.id,
            resource_natural_id=f"{instance_id}:{connection_type}",
            luciel_instance_id=instance_id,
            before={"status": prior_status},
            after={
                "connection_type": connection_type,
                "auth_class": row.auth_class,
                "status": new_status,
                "notify_admin": False,
            },
            note=(
                f"Connection status changed ({connection_type}: "
                f"{prior_status}→{new_status})."
            ),
            autocommit=False,
        )

    def _fail(detail: str) -> RedirectResponse:
        repo.apply_health_check(
            row=row,
            status="error",
            last_health_check_at=datetime.now(timezone.utc),
            secret_ref=None,
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
                "secret_ref_present": False,
            },
            note=f"OAuth callback failed ({connection_type}): {detail}"[:256],
            autocommit=False,
        )
        _emit_status_changed("error")
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
        secret_ref = store.put(secret_name, refresh_token)
    except SecretStoreError as exc:
        return _fail(f"secret store write failed: {exc}")

    repo.apply_health_check(
        row=row,
        status="connected",
        last_health_check_at=datetime.now(timezone.utc),
        secret_ref=secret_ref,
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
            "secret_ref_present": True,
        },
        note=f"OAuth callback connected ({connection_type}).",
        autocommit=False,
    )
    _emit_status_changed("connected")
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
    ``secret_ref`` (a real stored secret — e.g. an OAuth refresh
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
    secret_ref = row.secret_ref

    disconnected = repo.disconnect(
        admin_id=admin_id,
        connection_id=connection_id,
        autocommit=False,
    )

    secret_cleanup_enqueued = False
    if disconnected and secret_ref:
        SecretCleanupOutboxRepository(db).enqueue(
            admin_id=admin_id,
            secret_ref=secret_ref,
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
