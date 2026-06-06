"""Arc 12 WU2b — per-instance tool-authorization admin API.

Three admin routes that back the Tool UI's "available tools" panel
and the authorize/revoke toggles. All mount under prefix
``/admin/instances/{instance_id}/tools``:

  * GET    /                       -- list the 8 v1 catalog tools with
                                      per-instance authorization state
                                      and tier/channel availability
                                      flags so the UI can render the
                                      "authorized" band, the "available
                                      to add" band, and grey-out
                                      tier-locked rows.
  * POST   /{tool_id}/authorize    -- create (or no-op if already live)
                                      the instance_tool_authorizations
                                      row for (admin_id, instance_id,
                                      tool_id). 201 on first create;
                                      200 when an already-live row is
                                      returned.
  * POST   /{tool_id}/revoke       -- soft-revoke the live row. 200 on
                                      success; 404 when no live row
                                      existed (idempotent at the
                                      service layer, but the route
                                      surfaces 404 so the operator
                                      notices the no-op).

Layered defences applied to every route
---------------------------------------

  L1   ``ScopePolicy.enforce_admin_owns_instance`` -- cross-Admin
       guard via Instance resolution + admin_id match (Wall-1 + Wall-3).
  L2   ``ScopePolicy.enforce_role_on_instance`` -- Wall-2. Tool
       authorization is an admin-level configuration action: only
       ``admin_owner`` and ``admin_manager`` may toggle (read_only_viewer
       cannot mutate; instance_operator is list/view-scoped per the
       §3.2.2 analogue used by the Knowledge subsystem and is NOT in
       the authoring set). The GET route is read-only and allows the
       full four-role matrix (operator + viewer can see what's
       authorized on the Instance they're scoped to).
  L3   ``TenantScopedDbSession`` -- binds ``app.admin_id`` GUC onto
       the session so Arc 9 RLS fences fire on the
       ``instance_tool_authorizations`` table.
  L4   ``admin_audit_log`` row appended on every authorize / revoke
       in the same transaction as the mutation.

Tier gating
-----------

Each tool declares ``requires_tier`` (subset of ('free','pro',
'enterprise')). Authorization is rejected with 403 when the Admin's
tier is not in the tool's tuple. The GET response surfaces
``tier_available`` per tool so the frontend can grey-out tier-locked
rows with an "Upgrade to {Tier}" hint without speculating about the
matrix.

Channel availability
--------------------

Each tool declares ``requires_channels`` (frozenset of channel ids).
Channel adapters land in Arc 13; until then no Instance has any
channel enabled, so ``channels_available`` is False whenever
``requires_channels`` is non-empty (currently only send_email and
send_sms). The GET response surfaces ``channels_available`` truthfully
so the UI can render the structural reason a tool cannot dispatch yet
even if tier permits it.

Cognition exclusion
-------------------

Per Decision #20 + founder ruling 4, the three cognition behaviours
(escalate / save_memory / session_summary) are NOT tools and are
explicitly absent from the response. The registry holds only the 8
v1 catalog tools (WU7 evicted cognition); this API exposes exactly
that set.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import (
    TenantScopedDbSession,
    get_audit_context,
    get_luciel_instance_service,
)
from app.models.admin import Admin
from app.models.admin_audit_log import (
    ACTION_TOOL_AUTHORIZED,
    ACTION_TOOL_REVOKED,
    RESOURCE_INSTANCE_TOOL_AUTHORIZATION,
)
from app.models.instance import Instance
from app.policy.entitlements import (
    TIER_ENTITLEMENTS,
    TIER_FREE,
)
from app.policy.scope import (
    ROLE_ADMIN_OWNER,
    ScopePolicy,
)
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)
from app.repositories.instance_connection_repository import (
    InstanceConnectionRepository,
)
from app.services.instance_service import InstanceService
from app.services.instance_tool_authorization_service import (
    InstanceToolAuthorizationService,
)
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/admin/instances/{instance_id}/tools",
    tags=["admin-tools"],
)


# =====================================================================
# Role gate sets (Wall-2).
# =====================================================================
#
# Single-login model (Locked Dec #19): the account owner is the only tenant
# identity and may both read and toggle tool authorization. Both gate sets
# collapse to {ROLE_ADMIN_OWNER}; ScopePolicy enforces owner-of-instance (or
# platform_admin) before either action.

_TOGGLE_ROLES = frozenset({ROLE_ADMIN_OWNER})
_READ_ROLES = frozenset({ROLE_ADMIN_OWNER})


# =====================================================================
# Pydantic schemas
# =====================================================================


class ToolView(BaseModel):
    """Per-tool entry in the GET response.

    Carries the §3.3.1 contract fields the Tool UI needs to render a
    catalog row PLUS the per-instance authorization state and the
    tier/channel availability flags driving "Upgrade to {Tier}" and
    "Channel not connected" affordances.
    """

    tool_id: str
    display_name: str
    description: str
    requires_tier: list[str]
    requires_channels: list[str]
    execution_mode: Literal["in_process", "subprocess"]

    # Per-instance state (computed from instance_tool_authorizations).
    authorized: bool
    authorization_id: int | None
    authorized_at: datetime | None
    authorized_by_user_id: str | None

    # Availability gates surfaced for UI affordances. The frontend
    # uses these to grey-out tier-locked rows and to flag tools whose
    # channel adapter is not connected.
    tier_available: bool
    channels_available: bool

    # Arc 15 WU4 — connection-contract status (Arc 17 slice). For a tool
    # that declares ``requires_connection`` the UI renders a chip driven
    # by the live ``instance_connections`` row:
    #   no row / unconfigured → "action_needed"  ("Action needed: connect X")
    #   connected             → "connected"      ("Connected")
    #   error / expired       → "reconnect_needed" ("Reconnect needed")
    # A tool with ``requires_connection == None`` carries both fields
    # ``None`` (no chip).
    connection_type: str | None
    connection_status: (
        Literal["action_needed", "connected", "reconnect_needed"] | None
    )


class ToolListResponse(BaseModel):
    """GET response shape.

    ``admin_tier`` is included so the frontend can render the
    "Upgrade to Pro/Enterprise" hint without a second round-trip.
    """

    instance_id: int
    admin_id: str
    admin_tier: str
    tools: list[ToolView]


class ToolAuthorizationRead(BaseModel):
    """Response shape for authorize / revoke."""

    authorization_id: int
    admin_id: str
    instance_id: int
    tool_id: str
    enabled: bool
    authorized_by_user_id: str
    authorized_at: datetime
    revoked_at: datetime | None


# =====================================================================
# Helpers
# =====================================================================


def _require_admin_id(request: Request) -> str:
    admin_id = getattr(request.state, "admin_id", None)
    if not admin_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No authenticated admin context.",
        )
    return admin_id


def _require_actor_user_id(request: Request) -> uuid.UUID:
    """Resolve the cookied User UUID. Mutating routes require it so
    the audit row's ``authorized_by_user_id`` is populated.
    """
    actor_user_id = getattr(request.state, "actor_user_id", None)
    if actor_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Tool authorization requires a cookied User context; "
                "an API-key-only caller has no User identity to record "
                "as the authorizing actor."
            ),
        )
    if isinstance(actor_user_id, uuid.UUID):
        return actor_user_id
    try:
        return uuid.UUID(str(actor_user_id))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="actor_user_id is not a valid UUID.",
        )


def _load_active_instance(
    *,
    request: Request,
    instance_id: int,
    instance_service: InstanceService,
) -> Instance:
    """Load an Instance, enforce admin-owns-instance, reject if inactive."""
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
    """Look up the Admin's tier. Fail-closed to Free if missing or
    unknown. Mirrors the helper used by the sibling-grant service +
    admin_knowledge route.
    """
    row = db.execute(
        select(Admin.tier).where(Admin.id == admin_id)
    ).scalar_one_or_none()
    return row if row in TIER_ENTITLEMENTS else TIER_FREE


def _instance_channels_enabled(instance: Instance) -> frozenset[str]:
    """Return the set of channel ids structurally enabled on this
    Instance.

    Arc 13 wire-up: reads the real per-instance ``enabled_channels``
    column (migration arc13_b_instance_channel_fields) instead of the
    pre-Arc-13 empty-set stub. The widget is always present (it is the
    entitlement floor for every tier and needs no provisioning); email
    and sms appear once the admin enables + provisions them, which adds
    their id to ``enabled_channels``.

    Single chokepoint by design — every callsite that asks "is channel
    X live on this Instance?" routes through here, so the column read
    lives in exactly one place.
    """
    enabled = set(instance.enabled_channels or ())
    # Widget is structurally always available; defend against a row
    # whose enabled_channels somehow lost it (older backfill, manual
    # edit) so the widget surface never goes dark on a tier that has it.
    enabled.add("widget")
    return frozenset(enabled)


def _registry() -> ToolRegistry:
    """Single-instance registry. The registry's _register_defaults is
    side-effect-free, so constructing per-call is cheap and gives the
    test layer a fresh registry to mock against."""
    return ToolRegistry()


def _connection_status_for(
    *, requires_connection: str | None, live_status: str | None
) -> str | None:
    """Map a tool's connection requirement + the live row status onto the
    three-state UI chip (spec §92-96).

    ``requires_connection is None`` → no chip (return None). Otherwise:
      no row / unconfigured → "action_needed"
      connected             → "connected"
      error / expired       → "reconnect_needed"
    """
    if requires_connection is None:
        return None
    if live_status is None or live_status == "unconfigured":
        return "action_needed"
    if live_status == "connected":
        return "connected"
    # error / expired (and any future non-connected status) → reconnect.
    return "reconnect_needed"


def _serialize_tool_view(
    *,
    tool,
    authorization,
    admin_tier: str,
    instance_channels: frozenset[str],
    live_status_by_type: dict[str, str] | None = None,
) -> ToolView:
    """Build one ToolView from a registry tool + optional live
    authorization row + tier/channel context.

    ``live_status_by_type`` maps ``connection_type -> status`` for the
    instance's live ``instance_connections`` rows; it drives the
    connection chip for any tool that declares ``requires_connection``.
    """
    requires_tier = list(tool.requires_tier)
    requires_channels = sorted(tool.requires_channels)
    requires_connection = getattr(tool, "requires_connection", None)

    status_map = live_status_by_type or {}
    live_status = (
        status_map.get(requires_connection)
        if requires_connection is not None
        else None
    )
    connection_status = _connection_status_for(
        requires_connection=requires_connection, live_status=live_status
    )

    tier_available = admin_tier in requires_tier
    if requires_channels:
        channels_available = all(
            ch in instance_channels for ch in requires_channels
        )
    else:
        channels_available = True

    if authorization is not None:
        authorized = bool(authorization.enabled)
        authorization_id = authorization.id
        authorized_at = authorization.created_at
        authorized_by_user_id = str(authorization.authorized_by_user_id)
    else:
        authorized = False
        authorization_id = None
        authorized_at = None
        authorized_by_user_id = None

    return ToolView(
        tool_id=tool.tool_id,
        display_name=tool.display_name,
        description=tool.description,
        requires_tier=requires_tier,
        requires_channels=requires_channels,
        execution_mode=tool.execution_mode,
        authorized=authorized,
        authorization_id=authorization_id,
        authorized_at=authorized_at,
        authorized_by_user_id=authorized_by_user_id,
        tier_available=tier_available,
        channels_available=channels_available,
        connection_type=requires_connection,
        connection_status=connection_status,
    )


def _serialize_authorization(row) -> ToolAuthorizationRead:
    return ToolAuthorizationRead(
        authorization_id=row.id,
        admin_id=row.admin_id,
        instance_id=row.instance_id,
        tool_id=row.tool_id,
        enabled=bool(row.enabled),
        authorized_by_user_id=str(row.authorized_by_user_id),
        authorized_at=row.created_at,
        revoked_at=row.revoked_at,
    )


# =====================================================================
# Routes
# =====================================================================


@router.get(
    "",
    response_model=ToolListResponse,
)
def list_tools_for_instance(
    request: Request,
    instance_id: int,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
) -> ToolListResponse:
    """List the 8 v1 catalog tools with per-instance authorization
    state + tier/channel availability.

    Read-only. Allowed for the full four-role matrix (owner / manager
    / operator / viewer). ``ScopePolicy.enforce_role_on_instance``
    handles operator-scoping (an operator bound to Instance X gets
    403 trying to read Instance Y's tool state).

    Cognition behaviours (escalate / save_memory / session_summary)
    are NOT in the registry per Decision #20 + founder ruling 4 and
    are therefore absent from the response by construction.
    """
    admin_id = _require_admin_id(request)
    instance = _load_active_instance(
        request=request,
        instance_id=instance_id,
        instance_service=instance_service,
    )
    ScopePolicy.enforce_role_on_instance(
        request, instance, allowed_roles=_READ_ROLES,
    )

    admin_tier = _resolve_admin_tier(db, admin_id=admin_id)
    instance_channels = _instance_channels_enabled(instance)

    auth_service = InstanceToolAuthorizationService(db)
    live_rows = auth_service.list_for_instance(
        admin_id=admin_id, instance_id=instance.id,
    )
    live_by_tool: dict[str, object] = {row.tool_id: row for row in live_rows}

    # Arc 15 WU4 — one query for the instance's live connection statuses,
    # keyed by connection_type, so the per-tool connection chip needs no
    # N+1 lookup.
    conn_repo = InstanceConnectionRepository(db)
    live_status_by_type = conn_repo.live_status_by_type(
        admin_id=admin_id, instance_id=instance.id,
    )

    registry = _registry()
    tools_view = [
        _serialize_tool_view(
            tool=t,
            authorization=live_by_tool.get(t.tool_id),
            admin_tier=admin_tier,
            instance_channels=instance_channels,
            live_status_by_type=live_status_by_type,
        )
        for t in registry.list_tools()
    ]

    return ToolListResponse(
        instance_id=instance.id,
        admin_id=admin_id,
        admin_tier=admin_tier,
        tools=tools_view,
    )


@router.post(
    "/{tool_id}/authorize",
    response_model=ToolAuthorizationRead,
    status_code=status.HTTP_201_CREATED,
)
def authorize_tool_on_instance(
    request: Request,
    instance_id: int,
    tool_id: str,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> ToolAuthorizationRead:
    """Authorize one of the 8 v1 catalog tools on an Instance.

    Wall-2: admin_owner or admin_manager on this Instance.
    Tier gate: Admin's tier must be in ``tool.requires_tier``.
    Idempotent against an already-live row (the service returns the
    existing row); creates the row otherwise. Emits one audit row in
    the same transaction.

    Returns 404 if ``tool_id`` is not a registered catalog tool (the
    cognition behaviours are not registered and therefore yield 404
    here -- they are NOT configurable per Decision #20).
    """
    admin_id = _require_admin_id(request)
    actor_user_id = _require_actor_user_id(request)

    instance = _load_active_instance(
        request=request,
        instance_id=instance_id,
        instance_service=instance_service,
    )
    ScopePolicy.enforce_role_on_instance(
        request, instance, allowed_roles=_TOGGLE_ROLES,
    )

    registry = _registry()
    tool = registry.get(tool_id)
    if tool is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Unknown tool_id {tool_id!r}; not one of the 8 "
                f"configurable v1 catalog tools."
            ),
        )

    admin_tier = _resolve_admin_tier(db, admin_id=admin_id)
    if admin_tier not in tool.requires_tier:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Tool {tool_id!r} is not available on the {admin_tier!r} "
                f"tier; required tiers: {sorted(tool.requires_tier)}."
            ),
        )

    auth_service = InstanceToolAuthorizationService(db)
    # The service is idempotent: returns existing row OR inserts new.
    # We re-check existence so we can decide whether to emit an audit
    # row (an idempotent no-op should not write a duplicate audit row).
    existing = auth_service.repo.get_live(
        admin_id=admin_id,
        instance_id=instance.id,
        tool_id=tool_id,
    )

    if existing is not None:
        # Idempotent no-op: return the live row without writing a new
        # audit entry. The original ACTION_TOOL_AUTHORIZED for this row
        # is already in the chain from the first authorize call.
        return _serialize_authorization(existing)

    row = auth_service.repo.authorize(
        admin_id=admin_id,
        instance_id=instance.id,
        tool_id=tool_id,
        authorized_by_user_id=actor_user_id,
        enabled=True,
        autocommit=False,
    )

    audit_repo = AdminAuditRepository(db)
    audit_repo.record(
        ctx=audit_ctx,
        admin_id=admin_id,
        action=ACTION_TOOL_AUTHORIZED,
        resource_type=RESOURCE_INSTANCE_TOOL_AUTHORIZATION,
        resource_pk=row.id,
        resource_natural_id=f"{instance.id}:{tool_id}",
        luciel_instance_id=instance.id,
        before=None,
        after={
            "tool_id": tool_id,
            "enabled": True,
            "authorized_by_user_id": str(actor_user_id),
            "tier_at_authorize": admin_tier,
        },
        autocommit=False,
    )

    db.commit()
    db.refresh(row)
    logger.info(
        "Tool authorized admin=%s instance=%s tool=%s row_id=%s",
        admin_id, instance.id, tool_id, row.id,
    )
    return _serialize_authorization(row)


@router.post(
    "/{tool_id}/revoke",
    response_model=ToolAuthorizationRead,
)
def revoke_tool_on_instance(
    request: Request,
    instance_id: int,
    tool_id: str,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> ToolAuthorizationRead:
    """Soft-revoke the live authorization for (instance_id, tool_id).

    Wall-2: admin_owner or admin_manager on this Instance.
    Returns 404 if no live row exists -- the service is idempotent
    against missing rows but the route surfaces 404 so the operator
    notices the no-op (e.g. they revoked from a stale UI state).
    Emits one audit row in the same transaction as the soft-delete.

    Cognition behaviours yield 404 here -- not registered.
    """
    admin_id = _require_admin_id(request)
    _require_actor_user_id(request)  # surface 401 early; actor on audit_ctx

    instance = _load_active_instance(
        request=request,
        instance_id=instance_id,
        instance_service=instance_service,
    )
    ScopePolicy.enforce_role_on_instance(
        request, instance, allowed_roles=_TOGGLE_ROLES,
    )

    registry = _registry()
    if registry.get(tool_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Unknown tool_id {tool_id!r}; not one of the 8 "
                f"configurable v1 catalog tools."
            ),
        )

    auth_service = InstanceToolAuthorizationService(db)
    existing = auth_service.repo.get_live(
        admin_id=admin_id,
        instance_id=instance.id,
        tool_id=tool_id,
    )
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No live authorization for tool {tool_id!r} on "
                f"Instance {instance.id}."
            ),
        )

    before_payload = {
        "tool_id": tool_id,
        "enabled": bool(existing.enabled),
    }

    revoked = auth_service.repo.revoke(
        admin_id=admin_id,
        instance_id=instance.id,
        tool_id=tool_id,
        autocommit=False,
    )
    if not revoked:
        # Defensive: another concurrent revoker won the race. Surface
        # the no-op as 404 so the caller retries against fresh state.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Authorization for tool {tool_id!r} on Instance "
                f"{instance.id} was already revoked."
            ),
        )

    db.refresh(existing)

    audit_repo = AdminAuditRepository(db)
    audit_repo.record(
        ctx=audit_ctx,
        admin_id=admin_id,
        action=ACTION_TOOL_REVOKED,
        resource_type=RESOURCE_INSTANCE_TOOL_AUTHORIZATION,
        resource_pk=existing.id,
        resource_natural_id=f"{instance.id}:{tool_id}",
        luciel_instance_id=instance.id,
        before=before_payload,
        after={
            "tool_id": tool_id,
            "revoked_at": (
                existing.revoked_at.isoformat()
                if existing.revoked_at is not None
                else None
            ),
        },
        autocommit=False,
    )

    db.commit()
    db.refresh(existing)
    logger.info(
        "Tool revoked admin=%s instance=%s tool=%s row_id=%s",
        admin_id, instance.id, tool_id, existing.id,
    )
    return _serialize_authorization(existing)
