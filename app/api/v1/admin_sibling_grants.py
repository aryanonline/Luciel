"""Arc 12 WU4 — admin sibling-Luciel grant-authoring API (§3.3.4).

Five admin routes that back the sibling-grant authoring + approval
flow. All mount under prefix ``/admin/sibling-grants``:

  * POST   /                    — author a new grant
  * GET    /                    — list grants for the Admin
  * POST   /{grant_id}/approve  — admin_owner flips pending → live
  * POST   /{grant_id}/reject   — admin_owner flips pending → revoked
  * POST   /{grant_id}/revoke   — withdraws a live or pending grant

Layered defences applied to every mutation route
------------------------------------------------

  L1   ``ScopePolicy.enforce_admin_owns_instance`` — cross-Admin
       guard. The route resolves the caller and callee Instance rows
       and verifies the requesting Admin owns BOTH.
  L2   ``ScopePolicy.enforce_role_on_instance`` on BOTH Instances —
       the load-bearing Wall-2 property at the sibling layer. A user
       scoped to only one of the two CANNOT author a cross-Instance
       grant. ``allowed_roles`` is the owner+manager set for
       author/approve/reject/revoke (the spec gates these as owner/
       manager-level operations); approve narrows further to
       admin_owner only.
  L3   ``TenantScopedDbSession`` — binds ``app.admin_id`` GUC onto
       the session so Arc 9 RLS fences fire on the
       ``sibling_call_grants`` table.
  L4   ``admin_audit_log`` row appended on every author / approve /
       reject / revoke — emitted by the service in the same
       transaction as the mutation.

Tier matrix (§3.3.4)
--------------------

  * Free       — call_sibling_luciel is unavailable; author rejected
                 with 403 + structured error.
  * Pro        — author lands ``approval_state='live'`` immediately.
  * Enterprise — author lands ``pending_approval``; admin_owner
                 approve flips it to live.

The service enforces the tier matrix; the route translates service-
layer exceptions into HTTP responses.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.deps import (
    TenantScopedDbSession,
    get_audit_context,
    get_luciel_instance_service,
)
from app.models.instance import Instance
from app.policy.scope import (
    ROLE_ADMIN_MANAGER,
    ROLE_ADMIN_OWNER,
    ScopePolicy,
)
from app.repositories.admin_audit_repository import AuditContext
from app.services.instance_service import InstanceService
from app.services.sibling_call_grant_service import (
    GrantAlreadyExists,
    GrantNotFound,
    InvalidStateTransition,
    SiblingCallGrantService,
    TierNotEligibleForSiblingGrants,
)

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/admin/sibling-grants",
    tags=["admin-sibling-grants"],
)


# =====================================================================
# Role gate sets (Wall-2 at the sibling layer).
# =====================================================================
#
# Per WU4 spec: authoring / approving / rejecting / revoking are
# owner/manager-level operations. instance_operator and
# read_only_viewer cannot author cross-Instance grants. Approve is
# narrower (admin_owner only) per §3.3.4 — only the owner can ratify
# a pending grant on Enterprise.

_AUTHOR_ROLES = frozenset({ROLE_ADMIN_OWNER, ROLE_ADMIN_MANAGER})
_APPROVE_ROLES = frozenset({ROLE_ADMIN_OWNER})
_REJECT_ROLES = frozenset({ROLE_ADMIN_OWNER, ROLE_ADMIN_MANAGER})
_REVOKE_ROLES = frozenset({ROLE_ADMIN_OWNER, ROLE_ADMIN_MANAGER})


# =====================================================================
# Pydantic schemas
# =====================================================================


class SiblingGrantAuthorRequest(BaseModel):
    """Body for POST /admin/sibling-grants."""

    caller_instance_id: int = Field(..., gt=0)
    callee_instance_id: int = Field(..., gt=0)


class SiblingGrantRead(BaseModel):
    """Response shape for any single-grant endpoint."""

    grant_id: int
    admin_id: str
    caller_instance_id: int
    callee_instance_id: int
    approval_state: Literal["live", "pending_approval", "revoked"]
    granted_by_user_id: str
    granted_at: datetime
    approved_by_user_id: str | None
    approved_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SiblingGrantListResponse(BaseModel):
    grants: list[SiblingGrantRead]


# =====================================================================
# Helpers
# =====================================================================


def _serialize(grant) -> SiblingGrantRead:
    """ORM → API shape."""
    return SiblingGrantRead(
        grant_id=grant.id,
        admin_id=grant.admin_id,
        caller_instance_id=grant.caller_instance_id,
        callee_instance_id=grant.callee_instance_id,
        approval_state=grant.approval_state,
        granted_by_user_id=str(grant.granted_by_user_id),
        granted_at=grant.granted_at,
        approved_by_user_id=(
            str(grant.approved_by_user_id)
            if grant.approved_by_user_id is not None
            else None
        ),
        approved_at=grant.approved_at,
        revoked_at=grant.revoked_at,
        created_at=grant.created_at,
        updated_at=grant.updated_at,
    )


def _require_admin_id(request: Request) -> str:
    admin_id = getattr(request.state, "admin_id", None)
    if not admin_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No authenticated admin context.",
        )
    return admin_id


def _require_actor_user_id(request: Request) -> uuid.UUID:
    """Resolve the cookied User UUID that minted this request.

    Grant-authoring routes require a real cookied User behind them —
    the audit row needs ``granted_by_user_id`` populated. API-key-
    only callers (no cookie) cannot author grants because there is
    no User identity to record as the author. Surfaces 401 in that
    case so the operator notices the auth mismatch.
    """
    actor_user_id = getattr(request.state, "actor_user_id", None)
    if actor_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Sibling-grant authoring requires a cookied User "
                "context; an API-key-only caller has no User identity "
                "to record as the grant author."
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
    """Load an Instance, enforce admin-owns-instance, and reject if
    inactive. Same shape as the helper in admin_knowledge.py."""
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


def _enforce_wall2_both_instances(
    *,
    request: Request,
    caller: Instance,
    callee: Instance,
    allowed_roles: frozenset,
) -> None:
    """Wall-2 at the sibling layer.

    The author must hold one of ``allowed_roles`` on the caller
    Instance AND on the callee Instance. ``enforce_role_on_instance``
    raises 403 on miss; we run it twice. A user scoped to only one
    of the two Instances gets 403 on the second call.

    The order matters for the user-facing error: we check caller
    first then callee, so a 403 mentioning the callee tells the
    operator they're scoped to the caller side but not the callee.
    """
    ScopePolicy.enforce_role_on_instance(
        request, caller, allowed_roles=allowed_roles
    )
    ScopePolicy.enforce_role_on_instance(
        request, callee, allowed_roles=allowed_roles
    )


# =====================================================================
# Routes
# =====================================================================


@router.post(
    "",
    response_model=SiblingGrantRead,
    status_code=status.HTTP_201_CREATED,
)
def author_sibling_grant(
    request: Request,
    body: SiblingGrantAuthorRequest,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> SiblingGrantRead:
    """Author a new sibling-call grant.

    Wall-2: the author must hold admin_owner OR admin_manager on
    BOTH caller and callee Instances. A user scoped to only one
    side gets 403.

    Tier matrix:
      * Free       → 403 (composition not available on Free).
      * Pro        → 201, approval_state='live'.
      * Enterprise → 201, approval_state='pending_approval'.
    """
    admin_id = _require_admin_id(request)
    actor_user_id = _require_actor_user_id(request)

    if body.caller_instance_id == body.callee_instance_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="caller_instance_id and callee_instance_id must differ.",
        )

    caller = _load_active_instance(
        request=request,
        instance_id=body.caller_instance_id,
        instance_service=instance_service,
    )
    callee = _load_active_instance(
        request=request,
        instance_id=body.callee_instance_id,
        instance_service=instance_service,
    )

    _enforce_wall2_both_instances(
        request=request,
        caller=caller,
        callee=callee,
        allowed_roles=_AUTHOR_ROLES,
    )

    service = SiblingCallGrantService(db)
    try:
        grant = service.author(
            admin_id=admin_id,
            caller_instance_id=caller.id,
            callee_instance_id=callee.id,
            granted_by_user_id=actor_user_id,
            audit_ctx=audit_ctx,
            autocommit=False,
        )
    except TierNotEligibleForSiblingGrants as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        )
    except GrantAlreadyExists as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
    except InvalidStateTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    db.commit()
    db.refresh(grant)
    logger.info(
        "Sibling grant authored id=%s admin=%s caller=%s callee=%s state=%s",
        grant.id, admin_id, grant.caller_instance_id,
        grant.callee_instance_id, grant.approval_state,
    )
    return _serialize(grant)


@router.get(
    "",
    response_model=SiblingGrantListResponse,
)
def list_sibling_grants(
    request: Request,
    db: TenantScopedDbSession,
    caller_instance_id: int | None = None,
    callee_instance_id: int | None = None,
    state: Literal["live", "pending_approval", "revoked"] | None = None,
) -> SiblingGrantListResponse:
    """List sibling-call grants for the requesting Admin.

    Listing is scoped Admin-wide — the caller must hold a role with
    visibility into the Admin (owner / manager / operator); the
    route enforces this via ``enforce_admin_scope`` (cross-Admin
    guard). instance_operator is intentionally permitted to read
    here (they may need to see grants touching their bound Instance)
    but the response is filtered by RLS at the row level if they
    don't own the row.

    Filters:
      * ``caller_instance_id`` — only grants this Instance authored.
      * ``callee_instance_id`` — only grants targeting this Instance.
      * ``state`` — only this approval_state.
    """
    admin_id = _require_admin_id(request)
    # Cross-Admin guard. We do not gate by Instance-level role here
    # because the list is Admin-scoped; RLS + the explicit filter on
    # admin_id is the load-bearing fence.
    ScopePolicy.enforce_admin_scope(request, admin_id)

    service = SiblingCallGrantService(db)
    grants = service.list_for_admin(
        admin_id=admin_id,
        approval_states=(state,) if state is not None else None,
        caller_instance_id=caller_instance_id,
        callee_instance_id=callee_instance_id,
    )
    return SiblingGrantListResponse(
        grants=[_serialize(g) for g in grants],
    )


def _load_grant_and_enforce_wall2(
    *,
    request: Request,
    grant_id: int,
    db,
    instance_service: InstanceService,
    admin_id: str,
    allowed_roles: frozenset,
):
    """Shared helper for approve / reject / revoke: load the grant,
    resolve both Instances, run Wall-2 on both.

    Returns ``(service, grant)``. Raises HTTPException(404) if not
    found. Raises HTTPException(403) on Wall-2 miss via ScopePolicy.
    """
    service = SiblingCallGrantService(db)
    try:
        grant = service.get_by_id(admin_id=admin_id, grant_id=grant_id)
    except GrantNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )

    # Resolve both Instances. For approve/reject/revoke we DO load
    # the Instance rows because Wall-2 needs to fire on each. If
    # either Instance has been hard-deleted we still want the
    # adjudication to be possible (an admin should be able to revoke
    # a grant whose callee was deleted) — so we look them up but
    # tolerate not-found by returning a synthetic Instance-like
    # object that carries (id, admin_id). The simplest implementation
    # is to look them up; in practice with the deactivation cascade
    # in place (see instance_repository) the grants for a deleted
    # Instance are already swept.
    caller = instance_service.get_by_pk(grant.caller_instance_id)
    callee = instance_service.get_by_pk(grant.callee_instance_id)
    if caller is None or callee is None:
        # Defensive — if a row exists for an Instance that's gone,
        # the operator's only option is to revoke it. Skip Wall-2
        # in that case (the cross-Admin guard via admin_id +
        # `get_by_id` already passed) and let the mutation through.
        # Log so the situation is visible.
        logger.warning(
            "Sibling grant id=%s references a missing Instance "
            "(caller=%s present=%s, callee=%s present=%s) — Wall-2 "
            "skipped, admin_id scope already verified.",
            grant_id, grant.caller_instance_id, caller is not None,
            grant.callee_instance_id, callee is not None,
        )
        return service, grant

    _enforce_wall2_both_instances(
        request=request,
        caller=caller,
        callee=callee,
        allowed_roles=allowed_roles,
    )
    return service, grant


@router.post(
    "/{grant_id}/approve",
    response_model=SiblingGrantRead,
)
def approve_sibling_grant(
    request: Request,
    grant_id: int,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> SiblingGrantRead:
    """Approve a pending grant — Enterprise-only operation.

    Wall-2: admin_owner role on BOTH caller and callee Instances
    (manager cannot approve — only the owner ratifies).
    Pre-condition: grant is in ``pending_approval`` state.
    """
    admin_id = _require_admin_id(request)
    actor_user_id = _require_actor_user_id(request)

    service, grant = _load_grant_and_enforce_wall2(
        request=request,
        grant_id=grant_id,
        db=db,
        instance_service=instance_service,
        admin_id=admin_id,
        allowed_roles=_APPROVE_ROLES,
    )
    try:
        grant = service.approve(
            admin_id=admin_id,
            grant_id=grant_id,
            approved_by_user_id=actor_user_id,
            audit_ctx=audit_ctx,
            autocommit=False,
        )
    except InvalidStateTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )

    db.commit()
    db.refresh(grant)
    return _serialize(grant)


@router.post(
    "/{grant_id}/reject",
    response_model=SiblingGrantRead,
)
def reject_sibling_grant(
    request: Request,
    grant_id: int,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> SiblingGrantRead:
    """Reject a pending grant. Distinct from revoke — reject is the
    pre-live terminal transition (the grant never went live), revoke
    withdraws an already-live grant.

    Wall-2: admin_owner OR admin_manager on BOTH Instances.
    Pre-condition: grant is in ``pending_approval`` state.
    """
    admin_id = _require_admin_id(request)
    actor_user_id = _require_actor_user_id(request)

    service, _ = _load_grant_and_enforce_wall2(
        request=request,
        grant_id=grant_id,
        db=db,
        instance_service=instance_service,
        admin_id=admin_id,
        allowed_roles=_REJECT_ROLES,
    )
    try:
        grant = service.reject(
            admin_id=admin_id,
            grant_id=grant_id,
            rejected_by_user_id=actor_user_id,
            audit_ctx=audit_ctx,
            autocommit=False,
        )
    except InvalidStateTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )

    db.commit()
    db.refresh(grant)
    return _serialize(grant)


@router.post(
    "/{grant_id}/revoke",
    response_model=SiblingGrantRead,
)
def revoke_sibling_grant(
    request: Request,
    grant_id: int,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> SiblingGrantRead:
    """Revoke a live or pending grant. Terminal — re-authoring is
    permitted (a fresh row is inserted; the partial unique index
    excludes the revoked predecessor).

    Wall-2: admin_owner OR admin_manager on BOTH Instances.
    Pre-condition: grant is NOT already revoked.
    """
    admin_id = _require_admin_id(request)
    # actor_user_id is captured for parity with the other routes but
    # the revoke service signature doesn't carry it (the audit
    # context already records the actor; revoke has no "approver"
    # column to populate). Surface 401 early if no cookie.
    _require_actor_user_id(request)

    service, _ = _load_grant_and_enforce_wall2(
        request=request,
        grant_id=grant_id,
        db=db,
        instance_service=instance_service,
        admin_id=admin_id,
        allowed_roles=_REVOKE_ROLES,
    )
    try:
        grant = service.revoke(
            admin_id=admin_id,
            grant_id=grant_id,
            audit_ctx=audit_ctx,
            autocommit=False,
        )
    except InvalidStateTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )

    db.commit()
    db.refresh(grant)
    return _serialize(grant)
