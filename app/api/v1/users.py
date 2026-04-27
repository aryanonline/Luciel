"""
Users routes.

Step 24.5b. Platform-admin-only HTTP surface for the durable User
identity layer (Q6 resolution). Tenant admins do NOT bind directly
to the users table -- they create Agents, and the Agent->User
binding is owned by the platform layer.

Routes:
  POST   /api/v1/users           Create a User (real or synthetic).
  GET    /api/v1/users/{id}      Read by UUID.
  PATCH  /api/v1/users/{id}      Partial update (display_name, email).
  DELETE /api/v1/users/{id}      Soft-deactivate (Q6 cascade).

Authorization: every route requires "platform_admin" in
request.state.permissions. Tenant-admin and agent-admin keys get
403 here. The synthetic=True flag on POST has its own service-layer
gate (defense in depth -- even within platform_admin, synthetic
creation is reserved for backend-internal call paths).

Translation contract (domain -> HTTP):
  UserNotFoundError         -> 404
  EmailAlreadyExistsError   -> 409
  PlatformAdminRequiredError-> 403

Audit: every mutation emits an audit row in the same txn as the DB
write (Invariant 4). UserRepository handles emission internally;
the route layer just propagates AuditContext built from the
incoming request.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import DbSession
from app.repositories.admin_audit_repository import AuditContext
from app.schemas.user import (
    UserCreate,
    UserDeactivate,
    UserRead,
    UserUpdate,
)
from app.services.user_service import (
    EmailAlreadyExistsError,
    PlatformAdminRequiredError,
    UserNotFoundError,
    UserService,
)

router = APIRouter(prefix="/users", tags=["users"])


# ---------------------------------------------------------------------
# Authorization helper
# ---------------------------------------------------------------------

def _require_platform_admin(request: Request) -> None:
    """Reject 403 if the calling key isn't platform_admin.

    Mirrors the admin-route convention: ApiKeyAuthMiddleware has
    already populated request.state.permissions; we read it here.
    No fallback -- absence of permissions is treated as no platform_admin.
    """
    perms = getattr(request.state, "permissions", None) or []
    if "platform_admin" not in perms:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This route requires platform_admin permission",
        )


# ---------------------------------------------------------------------
# POST /api/v1/users
# ---------------------------------------------------------------------

@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
)
def create_user(
    payload: UserCreate,
    request: Request,
    db: DbSession,
) -> UserRead:
    """Create a User. Platform-admin only.

    synthetic=True is permitted here only because we've gated the
    route to platform_admin. The service layer re-checks via
    actor_is_platform_admin=True, which mirrors the route gate so
    the service is safe to call from internal code paths too.
    """
    _require_platform_admin(request)
    audit_ctx = AuditContext.from_request(request)
    service = UserService(db)
    try:
        user = service.create_user(
            payload=payload,
            actor_is_platform_admin=True,
            audit_ctx=audit_ctx,
        )
    except EmailAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except PlatformAdminRequiredError as exc:
        # Defense-in-depth -- shouldn't fire because we just passed
        # actor_is_platform_admin=True. But preserve the contract.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    return UserRead.model_validate(user)


# ---------------------------------------------------------------------
# GET /api/v1/users/{user_id}
# ---------------------------------------------------------------------

@router.get(
    "/{user_id}",
    response_model=UserRead,
)
def get_user(
    user_id: uuid.UUID,
    request: Request,
    db: DbSession,
) -> UserRead:
    """Fetch by UUID. Platform-admin only. 404 if missing or inactive."""
    _require_platform_admin(request)
    service = UserService(db)
    try:
        user = service.get_user(user_id)
    except UserNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return UserRead.model_validate(user)


# ---------------------------------------------------------------------
# PATCH /api/v1/users/{user_id}
# ---------------------------------------------------------------------

@router.patch(
    "/{user_id}",
    response_model=UserRead,
)
def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    request: Request,
    db: DbSession,
) -> UserRead:
    """Partial update. Platform-admin only.

    Whitelisted fields: display_name, email. The schema enforces
    normalization (lowercase email, whitespace-collapsed display_name).
    """
    _require_platform_admin(request)
    audit_ctx = AuditContext.from_request(request)
    service = UserService(db)
    try:
        user = service.update_user(
            user_id=user_id,
            payload=payload,
            audit_ctx=audit_ctx,
        )
    except UserNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except EmailAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return UserRead.model_validate(user)


# ---------------------------------------------------------------------
# DELETE /api/v1/users/{user_id}
# ---------------------------------------------------------------------

@router.delete(
    "/{user_id}",
    response_model=UserRead,
)
def deactivate_user(
    user_id: uuid.UUID,
    payload: UserDeactivate,
    request: Request,
    db: DbSession,
) -> UserRead:
    """Soft-deactivate. Platform-admin only.

    Triggers the Q6 cascade (UserService.deactivate_user):
      1. End every active ScopeAssignment for this User across tenants.
      2. Each end_assignment cascades to mandatory key rotation
         via ApiKeyService.rotate_keys_for_agent.
      3. Soft-deactivate every bound Agent.
      4. Soft-deactivate the User row itself.

    All in one transaction. Any failure rolls back the entire cascade.

    The reason field (10-500 chars) is required by the UserDeactivate
    schema and feeds into the audit row's note field.
    """
    _require_platform_admin(request)
    audit_ctx = AuditContext.from_request(request)
    service = UserService(db)
    try:
        user = service.deactivate_user(
            user_id=user_id,
            reason=payload.reason,
            audit_ctx=audit_ctx,
        )
    except UserNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return UserRead.model_validate(user)