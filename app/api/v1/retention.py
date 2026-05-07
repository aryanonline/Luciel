"""Retention policy admin routes.

Step 29.y Cluster 1 (G-5 resolution)
====================================

Pre-29.y, every route in this module was rate-limited but none enforced
that the caller's API key had scope over the tenant being touched. The
five mutating routes (POST/PATCH/DELETE/enforce/manual_purge) wrote
nothing to admin_audit_logs, leaving PIPEDA legal-compliance changes
without an evidentiary trail. See findings_phase1g.md G-5 for the four
documented attack shapes (cross-tenant policy creation, cross-tenant
config leak via list/get/patch/delete, retention enforcement on a
foreign tenant, manual purge of foreign data).

Hardened contract:

  1. Every route is rate-limited (unchanged from pre-29.y).
  2. Every route resolves an effective ``tenant_id`` and enforces that
     the caller has scope over it. Non-platform callers cannot read or
     write another tenant's policies; the GET/list routes silently
     downgrade to the caller's own tenant when a foreign tenant_id is
     supplied (matches the audit_log.py convention).
  3. POST / PATCH / DELETE / POST-enforce / POST-purge write an
     ``admin_audit_logs`` row BEFORE the mutation, using the same
     audit-first-then-mutate invariant locked at admin_forensics.py
     line 779-800. The audit row carries before/after snapshots so a
     regulator can reconstruct the policy state at any point in time.
  4. GET routes (list, get-one, list-logs) do NOT audit -- they are
     read-only and the volume would noise the trail. Same convention
     as ``GET /admin/audit-log`` and ``GET /admin/verification``.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.deps import (
    DbSession,
    get_admin_audit_repository,
    get_audit_context,
)
from app.middleware.rate_limit import (
    ADMIN_RATE_LIMIT,
    get_api_key_or_ip,
    limiter,
)
from app.models.admin_audit_log import (
    ACTION_CREATE,
    ACTION_DELETE_HARD,
    ACTION_RETENTION_ENFORCE,
    ACTION_RETENTION_MANUAL_PURGE,
    ACTION_UPDATE,
    RESOURCE_RETENTION_POLICY,
)
from app.models.retention import RetentionPolicy
from app.policy.retention import RetentionService
from app.policy.scope import ScopePolicy
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)
from app.repositories.retention_repository import RetentionRepository
from app.schemas.retention import (
    DeletionLogRead,
    EnforceResult,
    ManualPurgeRequest,
    RetentionPolicyCreate,
    RetentionPolicyRead,
    RetentionPolicyUpdate,
)

router = APIRouter(prefix="/admin/retention", tags=["retention"])


def _get_retention_service(db: DbSession) -> RetentionService:
    repo = RetentionRepository(db)
    return RetentionService(db=db, repository=repo)


def _resolve_target_tenant(
    request: Request, supplied_tenant_id: str | None
) -> str | None:
    """Resolve effective tenant_id for a retention operation.

    Returns the caller's own tenant_id for non-platform callers,
    silently ignoring any cross-tenant value supplied by the caller.
    For platform_admin callers, returns the supplied value (or None
    for "all tenants" operations like enforce_all_policies).

    Raises 403 only when the caller has NO tenant binding AND is not
    platform_admin -- a state that should be impossible per the F-7
    NOT NULL constraint on api_keys.tenant_id, but defended against
    here.
    """
    is_platform = ScopePolicy.is_platform_admin(request)
    key_tenant_id = getattr(request.state, "tenant_id", None)

    if is_platform:
        return supplied_tenant_id  # may be None (means "all")

    if key_tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "missing_tenant_scope",
                "message": (
                    "API key has no tenant binding; retention routes "
                    "require a tenant-scoped key or platform_admin."
                ),
            },
        )
    return key_tenant_id


def _enforce_policy_owned_by_caller(
    request: Request, policy: RetentionPolicy | None
) -> RetentionPolicy:
    """Common 404-or-403 guard for routes that load a policy by id.

    Returns 404 (not 403) on cross-tenant access so the surrogate PK
    space can't be probed for existence. The 404 is indistinguishable
    from "no such row at all" by the caller.
    """
    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Policy not found",
        )

    is_platform = ScopePolicy.is_platform_admin(request)
    if is_platform:
        return policy

    key_tenant_id = getattr(request.state, "tenant_id", None)
    if key_tenant_id is None:
        # Defense in depth: a non-platform caller without tenant
        # binding should have been rejected upstream already.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Policy not found",
        )
    if policy.tenant_id != key_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Policy not found",
        )
    return policy


def _policy_snapshot(policy: RetentionPolicy) -> dict[str, Any]:
    """Snapshot the audit-relevant fields of a policy row.

    Used as the ``before``/``after`` payload on PATCH/DELETE rows so a
    regulator can see exactly which retention parameters changed.
    """
    return {
        "tenant_id": policy.tenant_id,
        "data_category": policy.data_category,
        "retention_days": policy.retention_days,
        "action": policy.action,
        "purpose": policy.purpose,
    }


# ---- Policy CRUD -----------------------------------------------------


@router.post(
    "/policies",
    response_model=RetentionPolicyRead,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def create_policy(
    request: Request,
    payload: RetentionPolicyCreate,
    db: DbSession,
    audit_repo: Annotated[
        AdminAuditRepository, Depends(get_admin_audit_repository)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> RetentionPolicyRead:
    repo = RetentionRepository(db)

    effective_tenant_id = _resolve_target_tenant(request, payload.tenant_id)
    if effective_tenant_id is None:
        # Platform-admin must explicitly target a tenant for policy
        # creation -- "all tenants" is not a legal target for create.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "tenant_id_required",
                "message": "Retention policy creation requires tenant_id.",
            },
        )

    existing = repo.get_policy_for_category(
        data_category=payload.data_category,
        tenant_id=effective_tenant_id,
    )
    if existing and existing.tenant_id == effective_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Policy for '{payload.data_category}' already exists "
                f"(tenant={effective_tenant_id})"
            ),
        )

    audit_repo.record(
        ctx=audit_ctx,
        tenant_id=effective_tenant_id,
        action=ACTION_CREATE,
        resource_type=RESOURCE_RETENTION_POLICY,
        resource_pk=None,
        resource_natural_id=(
            f"{effective_tenant_id}:{payload.data_category}"
        ),
        before=None,
        after={
            "tenant_id": effective_tenant_id,
            "data_category": payload.data_category,
            "retention_days": payload.retention_days,
            "action": payload.action,
            "purpose": payload.purpose,
        },
        note="step-29y-c1-retention-policy-create",
        autocommit=False,
    )

    policy = RetentionPolicy(
        tenant_id=effective_tenant_id,
        data_category=payload.data_category,
        retention_days=payload.retention_days,
        action=payload.action,
        purpose=payload.purpose,
        created_by=payload.created_by,
    )
    created = repo.create_policy(policy)
    if db.in_transaction():
        db.commit()
    return RetentionPolicyRead.model_validate(created)


@router.get("/policies", response_model=list[RetentionPolicyRead])
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def list_policies(
    request: Request,
    db: DbSession,
    tenant_id: str | None = Query(default=None),
) -> list[RetentionPolicyRead]:
    repo = RetentionRepository(db)
    effective_tenant_id = _resolve_target_tenant(request, tenant_id)
    policies = repo.list_policies(tenant_id=effective_tenant_id)
    return [RetentionPolicyRead.model_validate(p) for p in policies]


@router.get("/policies/{policy_id}", response_model=RetentionPolicyRead)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def get_policy(
    request: Request,
    policy_id: int,
    db: DbSession,
) -> RetentionPolicyRead:
    repo = RetentionRepository(db)
    policy = _enforce_policy_owned_by_caller(request, repo.get_policy(policy_id))
    return RetentionPolicyRead.model_validate(policy)


@router.patch("/policies/{policy_id}", response_model=RetentionPolicyRead)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def update_policy(
    request: Request,
    policy_id: int,
    payload: RetentionPolicyUpdate,
    db: DbSession,
    audit_repo: Annotated[
        AdminAuditRepository, Depends(get_admin_audit_repository)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> RetentionPolicyRead:
    repo = RetentionRepository(db)
    existing = _enforce_policy_owned_by_caller(request, repo.get_policy(policy_id))

    before = _policy_snapshot(existing)
    patch_fields = payload.model_dump(exclude_unset=True)
    after = {**before, **patch_fields}

    audit_repo.record(
        ctx=audit_ctx,
        tenant_id=existing.tenant_id,
        action=ACTION_UPDATE,
        resource_type=RESOURCE_RETENTION_POLICY,
        resource_pk=existing.id,
        resource_natural_id=(
            f"{existing.tenant_id}:{existing.data_category}"
        ),
        before=before,
        after=after,
        note="step-29y-c1-retention-policy-update",
        autocommit=False,
    )

    policy = repo.update_policy(policy_id, **patch_fields)
    if policy is None:
        # Race: deleted between the load above and the update. Roll
        # back the audit row by raising; the outer FastAPI dependency
        # tear-down will rollback the session.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Policy not found",
        )
    if db.in_transaction():
        db.commit()
    return RetentionPolicyRead.model_validate(policy)


@router.delete(
    "/policies/{policy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def delete_policy(
    request: Request,
    policy_id: int,
    db: DbSession,
    audit_repo: Annotated[
        AdminAuditRepository, Depends(get_admin_audit_repository)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> None:
    repo = RetentionRepository(db)
    existing = _enforce_policy_owned_by_caller(request, repo.get_policy(policy_id))

    audit_repo.record(
        ctx=audit_ctx,
        tenant_id=existing.tenant_id,
        action=ACTION_DELETE_HARD,
        resource_type=RESOURCE_RETENTION_POLICY,
        resource_pk=existing.id,
        resource_natural_id=(
            f"{existing.tenant_id}:{existing.data_category}"
        ),
        before=_policy_snapshot(existing),
        after=None,
        note="step-29y-c1-retention-policy-delete",
        autocommit=False,
    )

    success = repo.delete_policy(policy_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Policy not found",
        )
    if db.in_transaction():
        db.commit()


# ---- Deletion Logs ---------------------------------------------------


@router.get("/logs", response_model=list[DeletionLogRead])
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def list_logs(
    request: Request,
    db: DbSession,
    tenant_id: str | None = Query(default=None),
    data_category: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[DeletionLogRead]:
    repo = RetentionRepository(db)
    effective_tenant_id = _resolve_target_tenant(request, tenant_id)
    logs = repo.list_deletion_logs(
        tenant_id=effective_tenant_id,
        data_category=data_category,
        limit=limit,
    )
    return [DeletionLogRead.model_validate(log) for log in logs]


# ---- Enforcement -----------------------------------------------------


@router.post("/enforce", response_model=list[EnforceResult])
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def enforce_policies(
    request: Request,
    db: DbSession,
    audit_repo: Annotated[
        AdminAuditRepository, Depends(get_admin_audit_repository)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
    tenant_id: str | None = Query(default=None),
) -> list[EnforceResult]:
    """Run retention enforcement.

    Tenant-scoped callers can only enforce on their own tenant. Platform
    admins may enforce on a specified tenant or across all tenants.
    Every enforcement run writes one audit row capturing the scope.
    """
    is_platform = ScopePolicy.is_platform_admin(request)
    if is_platform:
        target_tenant_id = tenant_id  # may be None for "all"
    else:
        # Non-platform: ignore cross-tenant requests, force own tenant.
        target_tenant_id = _resolve_target_tenant(request, tenant_id)

    audit_repo.record(
        ctx=audit_ctx,
        tenant_id=target_tenant_id,
        action=ACTION_RETENTION_ENFORCE,
        resource_type=RESOURCE_RETENTION_POLICY,
        resource_pk=None,
        resource_natural_id=None,
        before=None,
        after={
            "scope": "tenant" if target_tenant_id else "all_tenants",
            "tenant_id": target_tenant_id,
        },
        note="step-29y-c1-retention-enforce",
        autocommit=False,
    )

    service = _get_retention_service(db)
    if target_tenant_id:
        results = service.enforce_for_tenant(
            tenant_id=target_tenant_id,
            triggered_by="admin",
        )
    else:
        # Platform-admin "all tenants" path. Only reachable when
        # is_platform=True (non-platform callers were forced to a
        # specific tenant_id by _resolve_target_tenant above).
        results = service.enforce_all_policies(triggered_by="admin")

    if db.in_transaction():
        db.commit()
    return [EnforceResult(**r) for r in results]


@router.post("/purge", response_model=EnforceResult)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def manual_purge(
    request: Request,
    payload: ManualPurgeRequest,
    db: DbSession,
    audit_repo: Annotated[
        AdminAuditRepository, Depends(get_admin_audit_repository)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> EnforceResult:
    """Trigger a manual purge for a specific data_category + tenant.

    Always tenant-scoped; no "all tenants" path because a manual purge
    needs an explicit reason and target. Non-platform callers may only
    purge their own tenant's data.
    """
    effective_tenant_id = _resolve_target_tenant(request, payload.tenant_id)
    if effective_tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "tenant_id_required",
                "message": "Manual purge requires tenant_id.",
            },
        )

    audit_repo.record(
        ctx=audit_ctx,
        tenant_id=effective_tenant_id,
        action=ACTION_RETENTION_MANUAL_PURGE,
        resource_type=RESOURCE_RETENTION_POLICY,
        resource_pk=None,
        resource_natural_id=(
            f"{effective_tenant_id}:{payload.data_category}"
        ),
        before=None,
        after={
            "tenant_id": effective_tenant_id,
            "data_category": payload.data_category,
            "reason": payload.reason,
        },
        note="step-29y-c1-retention-manual-purge",
        autocommit=False,
    )

    service = _get_retention_service(db)
    try:
        result = service.manual_purge(
            data_category=payload.data_category,
            tenant_id=effective_tenant_id,
            reason=payload.reason,
            triggered_by="admin",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    if db.in_transaction():
        db.commit()
    return EnforceResult(**result)
