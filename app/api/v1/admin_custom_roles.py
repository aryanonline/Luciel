"""Arc 12b — admin custom-role authoring API (Enterprise-only).

Architecture §3.7.2 ("Permission-based custom roles (Enterprise, Arc 12b)") +
Vision §9 Decision #8 (Path B locked).

Routes mounted under prefix ``/admin/custom-roles``:

  * GET    /permissions          — list the permission catalog.
  * GET    /                     — list custom roles for the Admin.
  * POST   /                     — author a new custom role.
  * GET    /{role_id}            — read one custom role + its permissions.
  * PATCH  /{role_id}            — update display name / description /
                                   permission set on a custom role.
  * POST   /{role_id}/revoke     — revoke (soft-delete) a custom role.

Plus assignment routes under ``/admin/role-assignments``:

  * GET    /                     — list active user role assignments for
                                   the Admin (with filters).
  * POST   /                     — assign a role (locked OR custom) to a
                                   user at a scope.
  * POST   /{assignment_id}/revoke — revoke an assignment.

Layered defences applied to every mutation route:

  L1   Cross-Admin guard via ``ScopePolicy.enforce_admin_scope`` —
       the caller's admin_id must match the bound admin_id.
  L2   Tier gate — the Admin's tier must enable
       ``custom_role_authoring_enabled`` (Enterprise only). 403 on
       Free / Pro.
  L3   Permission gate — the caller must hold
       ``can_author_custom_roles`` for role-authoring writes, and
       ``can_assign_roles`` for assignment writes.
  L4   No-privilege-escalation — when authoring/updating a custom role,
       the author cannot grant a permission they themselves do not hold.
  L5   ``TenantScopedDbSession`` — binds ``app.admin_id`` GUC onto
       the session so the RLS policies on custom_roles +
       user_role_assignments fire.
  L6   ``admin_audit_log`` row appended on every author / update /
       revoke / assign — emitted in the same transaction as the
       mutation.

Reads (GET) require ``can_author_custom_roles`` OR ``can_assign_roles``
to see the surface — a manager who can't author/assign should not see
the authoring UI surface. This matches the Customer-Journey "Dana
(Enterprise)" admin surface convention.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import TenantScopedDbSession, get_audit_context
from app.models.admin import Admin
from app.models.admin_audit_log import (
    ACTION_CUSTOM_ROLE_AUTHORED,
    ACTION_CUSTOM_ROLE_REVOKED,
    ACTION_CUSTOM_ROLE_UPDATED,
    ACTION_USER_ROLE_ASSIGNED,
    ACTION_USER_ROLE_REVOKED,
    RESOURCE_CUSTOM_ROLE,
    RESOURCE_USER_ROLE_ASSIGNMENT,
)
from app.models.permission_model import (
    ALL_LOCKED_ROLES,
    ALL_SCOPE_TYPES,
    CustomRole,
    Permission,
    RolePermission,
    SCOPE_TYPE_ALL_INSTANCES,
    SCOPE_TYPE_INSTANCE_SPECIFIC,
    UserRoleAssignment,
)
from app.policy.entitlements import (
    TIER_FREE,
    TIER_ENTERPRISE,
    resolve_entitlement,
)
from app.policy.permissions import (
    PERM_ASSIGN_ROLES,
    PERM_AUTHOR_CUSTOM_ROLES,
    PermissionResolver,
)
from app.policy.scope import ScopePolicy
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)

logger = logging.getLogger(__name__)


roles_router = APIRouter(
    prefix="/admin/custom-roles",
    tags=["admin-custom-roles"],
)

assignments_router = APIRouter(
    prefix="/admin/role-assignments",
    tags=["admin-role-assignments"],
)


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
    """Custom-role + assignment writes require a cookied User behind them.
    API-key-only callers have no User identity to record as author.
    """
    actor_user_id = getattr(request.state, "actor_user_id", None)
    if actor_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Custom-role authoring requires a cookied User context; "
                "an API-key-only caller has no User identity to record."
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


def _resolve_admin_tier(db: Session, admin_id: str) -> str:
    """Return the Admin's tier string. Fail-closed to Free on miss."""
    from app.policy.entitlements import TIER_ENTITLEMENTS

    row = db.execute(
        select(Admin.tier).where(Admin.id == admin_id)
    ).scalar_one_or_none()
    return row if row in TIER_ENTITLEMENTS else TIER_FREE


def _require_enterprise_tier(db: Session, admin_id: str) -> None:
    """Reject the call when the Admin's tier does not enable Arc 12b
    custom-role authoring (Free/Pro). Tier resolution honours
    ``admin_tier_overrides``.
    """
    tier = _resolve_admin_tier(db, admin_id)
    enabled = resolve_entitlement(
        tier=tier,
        axis="custom_role_authoring_enabled",
    )
    if not enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Custom-role authoring is only available on the "
                "Enterprise tier."
            ),
        )


def _require_caller_permission(
    request: Request,
    *,
    permission_key: str,
) -> None:
    """Reject with 403 if the caller does not hold ``permission_key``."""
    if ScopePolicy.is_platform_admin(request):
        return
    resolved = PermissionResolver.resolve(request)
    if permission_key not in resolved:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Caller does not hold required permission "
                f"{permission_key!r}."
            ),
        )


def _require_caller_holds_all(
    request: Request,
    *,
    permission_keys: list[str],
) -> None:
    """No-privilege-escalation enforcement.

    Reject with 403 if the caller does not hold EVERY permission in
    ``permission_keys``. The author cannot grant a permission they do
    not themselves hold (Architecture §3.7.2 — "no privilege escalation
    via role authoring").
    """
    if ScopePolicy.is_platform_admin(request):
        return
    resolved = PermissionResolver.resolve(request)
    missing = [k for k in permission_keys if k not in resolved]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Privilege-escalation blocked: caller does not hold "
                f"the following permission(s) it is trying to grant: "
                f"{missing}."
            ),
        )


def _load_permissions_by_keys(
    db: Session, keys: list[str]
) -> list[Permission]:
    """Return the Permission rows for the given keys.

    Raises 400 if any key does not exist in the catalog.
    """
    if not keys:
        return []
    rows = db.execute(
        select(Permission).where(Permission.key.in_(keys))
    ).scalars().all()
    found_keys = {r.key for r in rows}
    missing = [k for k in keys if k not in found_keys]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown permission key(s): {missing}",
        )
    return list(rows)


def _serialize_role(
    role: CustomRole,
    perm_keys: list[str],
) -> "CustomRoleRead":
    return CustomRoleRead(
        role_id=role.id,
        admin_id=role.admin_id,
        role_key=role.role_key,
        display_name=role.display_name,
        description=role.description,
        permission_keys=sorted(perm_keys),
        authored_by_user_id=str(role.authored_by_user_id),
        authored_at=role.authored_at,
        revoked_at=role.revoked_at,
        created_at=role.created_at,
        updated_at=role.updated_at,
    )


def _role_permission_keys(db: Session, role: CustomRole) -> list[str]:
    """Return the list of permission keys bound to this custom role."""
    rows = db.execute(
        select(Permission.key)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.custom_role_id == role.id)
    ).scalars().all()
    return list(rows)


# =====================================================================
# Pydantic schemas
# =====================================================================


_ROLE_KEY_FIELD = Field(
    ...,
    min_length=1,
    max_length=64,
    pattern=r"^[a-z][a-z0-9_]{0,63}$",
    description=(
        "Stable identifier; lowercase letters / digits / underscore; "
        "starts with a letter."
    ),
)


class PermissionRead(BaseModel):
    permission_id: int
    key: str
    display_name: str
    description: str
    category: str


class PermissionsListResponse(BaseModel):
    permissions: list[PermissionRead]


class CustomRoleAuthorRequest(BaseModel):
    role_key: str = _ROLE_KEY_FIELD
    display_name: str = Field(..., min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    permission_keys: list[str] = Field(default_factory=list)

    @field_validator("permission_keys")
    @classmethod
    def _dedup(cls, v: list[str]) -> list[str]:
        return sorted(set(v))


class CustomRoleUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    # ``None`` = leave existing permissions untouched. Empty list = clear.
    permission_keys: list[str] | None = None

    @field_validator("permission_keys")
    @classmethod
    def _dedup(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return sorted(set(v))


class CustomRoleRead(BaseModel):
    role_id: int
    admin_id: str
    role_key: str
    display_name: str
    description: str | None
    permission_keys: list[str]
    authored_by_user_id: str
    authored_at: datetime
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CustomRoleListResponse(BaseModel):
    roles: list[CustomRoleRead]


class UserRoleAssignmentRequest(BaseModel):
    user_id: str = Field(..., description="UUID of the target User.")
    locked_role: Literal[
        "admin_owner",
        "admin_manager",
        "instance_operator",
        "read_only_viewer",
    ] | None = None
    custom_role_id: int | None = None
    scope_type: Literal["all_instances", "instance_specific"]
    instance_id: int | None = None

    @field_validator("user_id")
    @classmethod
    def _valid_uuid(cls, v: str) -> str:
        try:
            uuid.UUID(v)
        except ValueError as exc:
            raise ValueError(f"user_id must be a valid UUID: {exc}")
        return v


class UserRoleAssignmentRead(BaseModel):
    assignment_id: int
    admin_id: str
    user_id: str
    locked_role: str | None
    custom_role_id: int | None
    scope_type: str
    instance_id: int | None
    assigned_by_user_id: str
    assigned_at: datetime
    revoked_at: datetime | None


class UserRoleAssignmentListResponse(BaseModel):
    assignments: list[UserRoleAssignmentRead]


def _serialize_assignment(row: UserRoleAssignment) -> UserRoleAssignmentRead:
    return UserRoleAssignmentRead(
        assignment_id=row.id,
        admin_id=row.admin_id,
        user_id=str(row.user_id),
        locked_role=row.locked_role,
        custom_role_id=row.custom_role_id,
        scope_type=row.scope_type,
        instance_id=row.instance_id,
        assigned_by_user_id=str(row.assigned_by_user_id),
        assigned_at=row.assigned_at,
        revoked_at=row.revoked_at,
    )


# =====================================================================
# Routes — custom-roles
# =====================================================================


@roles_router.get(
    "/permissions",
    response_model=PermissionsListResponse,
)
def list_permission_catalog(
    request: Request,
    db: TenantScopedDbSession,
) -> PermissionsListResponse:
    """List the platform-managed permission catalog.

    Enterprise-only — the catalog is meaningless to Free/Pro UIs
    (they have no role-authoring surface).
    """
    admin_id = _require_admin_id(request)
    ScopePolicy.enforce_admin_scope(request, admin_id)
    _require_enterprise_tier(db, admin_id)
    _require_caller_permission(
        request, permission_key=PERM_AUTHOR_CUSTOM_ROLES
    )

    rows = (
        db.execute(
            select(Permission).order_by(Permission.category, Permission.key)
        )
        .scalars()
        .all()
    )
    return PermissionsListResponse(
        permissions=[
            PermissionRead(
                permission_id=p.id,
                key=p.key,
                display_name=p.display_name,
                description=p.description,
                category=p.category,
            )
            for p in rows
        ]
    )


@roles_router.get(
    "",
    response_model=CustomRoleListResponse,
)
def list_custom_roles(
    request: Request,
    db: TenantScopedDbSession,
    include_revoked: bool = False,
) -> CustomRoleListResponse:
    """List custom roles for the bound Admin.

    ``include_revoked`` defaults False (the UI's main view).
    """
    admin_id = _require_admin_id(request)
    ScopePolicy.enforce_admin_scope(request, admin_id)
    _require_enterprise_tier(db, admin_id)
    _require_caller_permission(
        request, permission_key=PERM_AUTHOR_CUSTOM_ROLES
    )

    stmt = select(CustomRole).where(CustomRole.admin_id == admin_id)
    if not include_revoked:
        stmt = stmt.where(CustomRole.revoked_at.is_(None))
    stmt = stmt.order_by(CustomRole.created_at.desc())

    rows = db.execute(stmt).scalars().all()
    out = []
    for role in rows:
        out.append(_serialize_role(role, _role_permission_keys(db, role)))
    return CustomRoleListResponse(roles=out)


@roles_router.post(
    "",
    response_model=CustomRoleRead,
    status_code=status.HTTP_201_CREATED,
)
def author_custom_role(
    request: Request,
    body: CustomRoleAuthorRequest,
    db: TenantScopedDbSession,
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> CustomRoleRead:
    """Author a new custom role.

    Defences:
      * Cross-Admin guard via ``enforce_admin_scope``.
      * Tier gate — Enterprise only (403 on Free / Pro).
      * Permission gate — author must hold ``can_author_custom_roles``.
      * No-priv-escalation — author must hold every permission they
        are granting in the new role.
      * Uniqueness — at-most-one live custom role per
        (admin_id, role_key); duplicate returns 409.
    """
    admin_id = _require_admin_id(request)
    actor_user_id = _require_actor_user_id(request)
    ScopePolicy.enforce_admin_scope(request, admin_id)
    _require_enterprise_tier(db, admin_id)
    _require_caller_permission(
        request, permission_key=PERM_AUTHOR_CUSTOM_ROLES
    )
    _require_caller_holds_all(
        request, permission_keys=body.permission_keys
    )

    # Reject duplicate live role_key (matches the partial unique index).
    existing = db.execute(
        select(CustomRole)
        .where(CustomRole.admin_id == admin_id)
        .where(CustomRole.role_key == body.role_key)
        .where(CustomRole.revoked_at.is_(None))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A live custom role with role_key={body.role_key!r} "
                f"already exists for this Admin."
            ),
        )

    # Resolve every requested permission.
    perms = _load_permissions_by_keys(db, body.permission_keys)

    role = CustomRole(
        admin_id=admin_id,
        role_key=body.role_key,
        display_name=body.display_name,
        description=body.description,
        authored_by_user_id=actor_user_id,
    )
    db.add(role)
    db.flush()  # populate role.id for the bindings.

    for p in perms:
        db.add(
            RolePermission(
                admin_id=admin_id,
                custom_role_id=role.id,
                permission_id=p.id,
            )
        )

    # Audit row — same transaction as the mutation.
    AdminAuditRepository(db).record(
        ctx=audit_ctx,
        admin_id=admin_id,
        action=ACTION_CUSTOM_ROLE_AUTHORED,
        resource_type=RESOURCE_CUSTOM_ROLE,
        resource_pk=role.id,
        resource_natural_id=role.role_key,
        after={
            "role_key": role.role_key,
            "display_name": role.display_name,
            "description": role.description,
            "permission_keys": sorted(p.key for p in perms),
            "authored_by_user_id": str(actor_user_id),
        },
        note=f"Authored custom role {role.role_key!r} with {len(perms)} permission(s).",
    )

    db.commit()
    db.refresh(role)
    return _serialize_role(role, [p.key for p in perms])


@roles_router.get(
    "/{role_id}",
    response_model=CustomRoleRead,
)
def get_custom_role(
    request: Request,
    role_id: int,
    db: TenantScopedDbSession,
) -> CustomRoleRead:
    admin_id = _require_admin_id(request)
    ScopePolicy.enforce_admin_scope(request, admin_id)
    _require_enterprise_tier(db, admin_id)
    _require_caller_permission(
        request, permission_key=PERM_AUTHOR_CUSTOM_ROLES
    )

    role = db.execute(
        select(CustomRole)
        .where(CustomRole.id == role_id)
        .where(CustomRole.admin_id == admin_id)
    ).scalar_one_or_none()
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Custom role {role_id} not found",
        )
    return _serialize_role(role, _role_permission_keys(db, role))


@roles_router.patch(
    "/{role_id}",
    response_model=CustomRoleRead,
)
def update_custom_role(
    request: Request,
    role_id: int,
    body: CustomRoleUpdateRequest,
    db: TenantScopedDbSession,
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> CustomRoleRead:
    """Update display_name / description / permission set on a custom role.

    Permission edits are subject to the same no-priv-escalation rule
    as authoring — the editor must hold every permission they are
    adding. Permissions already on the role that they don't hold can
    be left in place or removed but not added if not previously present.
    """
    admin_id = _require_admin_id(request)
    _require_actor_user_id(request)
    ScopePolicy.enforce_admin_scope(request, admin_id)
    _require_enterprise_tier(db, admin_id)
    _require_caller_permission(
        request, permission_key=PERM_AUTHOR_CUSTOM_ROLES
    )

    role = db.execute(
        select(CustomRole)
        .where(CustomRole.id == role_id)
        .where(CustomRole.admin_id == admin_id)
    ).scalar_one_or_none()
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Custom role {role_id} not found",
        )
    if role.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Custom role {role_id} is revoked and cannot be updated.",
        )

    before = {
        "display_name": role.display_name,
        "description": role.description,
        "permission_keys": sorted(_role_permission_keys(db, role)),
    }

    if body.display_name is not None:
        role.display_name = body.display_name
    if body.description is not None:
        role.description = body.description

    after_permission_keys = list(before["permission_keys"])
    if body.permission_keys is not None:
        existing_keys = set(before["permission_keys"])
        new_keys = set(body.permission_keys)
        added = new_keys - existing_keys

        # No-priv-escalation: the editor must hold every permission
        # they are ADDING. Removing or leaving in place permissions
        # they don't hold is permitted.
        _require_caller_holds_all(request, permission_keys=sorted(added))

        # Resolve every requested permission (also catches unknown keys).
        if new_keys:
            perms = _load_permissions_by_keys(db, sorted(new_keys))
        else:
            perms = []

        # Replace the binding rows for this custom role.
        db.execute(
            RolePermission.__table__.delete().where(
                RolePermission.custom_role_id == role.id
            )
        )
        for p in perms:
            db.add(
                RolePermission(
                    admin_id=admin_id,
                    custom_role_id=role.id,
                    permission_id=p.id,
                )
            )
        after_permission_keys = sorted(new_keys)

    role.updated_at = datetime.now(tz=timezone.utc)

    AdminAuditRepository(db).record(
        ctx=audit_ctx,
        admin_id=admin_id,
        action=ACTION_CUSTOM_ROLE_UPDATED,
        resource_type=RESOURCE_CUSTOM_ROLE,
        resource_pk=role.id,
        resource_natural_id=role.role_key,
        before=before,
        after={
            "display_name": role.display_name,
            "description": role.description,
            "permission_keys": after_permission_keys,
        },
        note=f"Updated custom role {role.role_key!r}.",
    )

    db.commit()
    db.refresh(role)
    return _serialize_role(role, _role_permission_keys(db, role))


@roles_router.post(
    "/{role_id}/revoke",
    response_model=CustomRoleRead,
)
def revoke_custom_role(
    request: Request,
    role_id: int,
    db: TenantScopedDbSession,
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> CustomRoleRead:
    """Revoke (soft-delete) a custom role.

    Active user_role_assignments referencing this role are NOT
    cascade-revoked — the assignments survive but resolve to an
    empty permission set (because the role row's revoked_at != NULL
    is filtered out). Operators clean up assignments separately so an
    accidental revoke is recoverable.
    """
    admin_id = _require_admin_id(request)
    _require_actor_user_id(request)
    ScopePolicy.enforce_admin_scope(request, admin_id)
    _require_enterprise_tier(db, admin_id)
    _require_caller_permission(
        request, permission_key=PERM_AUTHOR_CUSTOM_ROLES
    )

    role = db.execute(
        select(CustomRole)
        .where(CustomRole.id == role_id)
        .where(CustomRole.admin_id == admin_id)
    ).scalar_one_or_none()
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Custom role {role_id} not found",
        )
    if role.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Custom role {role_id} is already revoked.",
        )

    role.revoked_at = datetime.now(tz=timezone.utc)
    role.updated_at = role.revoked_at

    AdminAuditRepository(db).record(
        ctx=audit_ctx,
        admin_id=admin_id,
        action=ACTION_CUSTOM_ROLE_REVOKED,
        resource_type=RESOURCE_CUSTOM_ROLE,
        resource_pk=role.id,
        resource_natural_id=role.role_key,
        before={"revoked_at": None},
        after={"revoked_at": role.revoked_at.isoformat()},
        note=f"Revoked custom role {role.role_key!r}.",
    )

    db.commit()
    db.refresh(role)
    return _serialize_role(role, _role_permission_keys(db, role))


# =====================================================================
# Routes — role-assignments
# =====================================================================


@assignments_router.get(
    "",
    response_model=UserRoleAssignmentListResponse,
)
def list_role_assignments(
    request: Request,
    db: TenantScopedDbSession,
    user_id: str | None = None,
    include_revoked: bool = False,
) -> UserRoleAssignmentListResponse:
    """List active user role assignments for the bound Admin."""
    admin_id = _require_admin_id(request)
    ScopePolicy.enforce_admin_scope(request, admin_id)
    _require_enterprise_tier(db, admin_id)
    _require_caller_permission(request, permission_key=PERM_ASSIGN_ROLES)

    stmt = select(UserRoleAssignment).where(
        UserRoleAssignment.admin_id == admin_id
    )
    if user_id:
        try:
            uid = uuid.UUID(user_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="user_id must be a valid UUID",
            )
        stmt = stmt.where(UserRoleAssignment.user_id == uid)
    if not include_revoked:
        stmt = stmt.where(UserRoleAssignment.revoked_at.is_(None))
    stmt = stmt.order_by(UserRoleAssignment.created_at.desc())

    rows = db.execute(stmt).scalars().all()
    return UserRoleAssignmentListResponse(
        assignments=[_serialize_assignment(r) for r in rows]
    )


@assignments_router.post(
    "",
    response_model=UserRoleAssignmentRead,
    status_code=status.HTTP_201_CREATED,
)
def create_role_assignment(
    request: Request,
    body: UserRoleAssignmentRequest,
    db: TenantScopedDbSession,
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> UserRoleAssignmentRead:
    """Assign a role (locked OR custom) to a user at a scope.

    Defences:
      * Cross-Admin guard.
      * Tier gate — Enterprise only.
      * Permission gate — caller must hold ``can_assign_roles``.
      * No-priv-escalation — caller cannot assign a role whose
        permission set contains a permission they themselves do
        not hold.
      * XOR — exactly one of locked_role / custom_role_id is set
        (DB CHECK + this validator).
      * Scope sanity — instance_specific requires instance_id;
        all_instances requires instance_id IS NULL.
    """
    admin_id = _require_admin_id(request)
    actor_user_id = _require_actor_user_id(request)
    ScopePolicy.enforce_admin_scope(request, admin_id)
    _require_enterprise_tier(db, admin_id)
    _require_caller_permission(request, permission_key=PERM_ASSIGN_ROLES)

    # XOR validation.
    if (body.locked_role is None) == (body.custom_role_id is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Exactly one of locked_role or custom_role_id must be set."
            ),
        )

    # Scope sanity.
    if body.scope_type == SCOPE_TYPE_INSTANCE_SPECIFIC and body.instance_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="scope_type='instance_specific' requires instance_id.",
        )
    if body.scope_type == SCOPE_TYPE_ALL_INSTANCES and body.instance_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="scope_type='all_instances' must not carry an instance_id.",
        )

    # Resolve the permission set the assignment would grant — for the
    # no-priv-escalation check.
    granted_perm_keys: list[str] = []
    if body.locked_role is not None:
        if body.locked_role not in ALL_LOCKED_ROLES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown locked_role {body.locked_role!r}.",
            )
        rows = db.execute(
            select(Permission.key)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.locked_role == body.locked_role)
        ).scalars().all()
        granted_perm_keys = list(rows)
    else:
        # Custom role must belong to this Admin and be live.
        custom_role = db.execute(
            select(CustomRole)
            .where(CustomRole.id == body.custom_role_id)
            .where(CustomRole.admin_id == admin_id)
            .where(CustomRole.revoked_at.is_(None))
        ).scalar_one_or_none()
        if custom_role is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"Custom role {body.custom_role_id} not found, not "
                    f"owned by this Admin, or revoked."
                ),
            )
        granted_perm_keys = _role_permission_keys(db, custom_role)

    _require_caller_holds_all(
        request, permission_keys=granted_perm_keys
    )

    target_uid = uuid.UUID(body.user_id)
    row = UserRoleAssignment(
        admin_id=admin_id,
        user_id=target_uid,
        locked_role=body.locked_role,
        custom_role_id=body.custom_role_id,
        scope_type=body.scope_type,
        instance_id=body.instance_id,
        assigned_by_user_id=actor_user_id,
    )
    db.add(row)
    db.flush()

    AdminAuditRepository(db).record(
        ctx=audit_ctx,
        admin_id=admin_id,
        action=ACTION_USER_ROLE_ASSIGNED,
        resource_type=RESOURCE_USER_ROLE_ASSIGNMENT,
        resource_pk=row.id,
        resource_natural_id=str(target_uid),
        after={
            "user_id": str(target_uid),
            "locked_role": body.locked_role,
            "custom_role_id": body.custom_role_id,
            "scope_type": body.scope_type,
            "instance_id": body.instance_id,
            "granted_permission_keys": sorted(granted_perm_keys),
        },
        note=(
            f"Assigned "
            f"{'locked:' + body.locked_role if body.locked_role else 'custom:' + str(body.custom_role_id)} "
            f"to user {target_uid} scope={body.scope_type}."
        ),
    )

    db.commit()
    db.refresh(row)
    return _serialize_assignment(row)


@assignments_router.post(
    "/{assignment_id}/revoke",
    response_model=UserRoleAssignmentRead,
)
def revoke_role_assignment(
    request: Request,
    assignment_id: int,
    db: TenantScopedDbSession,
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> UserRoleAssignmentRead:
    admin_id = _require_admin_id(request)
    _require_actor_user_id(request)
    ScopePolicy.enforce_admin_scope(request, admin_id)
    _require_enterprise_tier(db, admin_id)
    _require_caller_permission(request, permission_key=PERM_ASSIGN_ROLES)

    row = db.execute(
        select(UserRoleAssignment)
        .where(UserRoleAssignment.id == assignment_id)
        .where(UserRoleAssignment.admin_id == admin_id)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role assignment {assignment_id} not found.",
        )
    if row.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Role assignment {assignment_id} is already revoked.",
        )

    row.revoked_at = datetime.now(tz=timezone.utc)
    row.updated_at = row.revoked_at

    AdminAuditRepository(db).record(
        ctx=audit_ctx,
        admin_id=admin_id,
        action=ACTION_USER_ROLE_REVOKED,
        resource_type=RESOURCE_USER_ROLE_ASSIGNMENT,
        resource_pk=row.id,
        resource_natural_id=str(row.user_id),
        before={"revoked_at": None},
        after={"revoked_at": row.revoked_at.isoformat()},
        note=f"Revoked role assignment {assignment_id}.",
    )

    db.commit()
    db.refresh(row)
    return _serialize_assignment(row)
