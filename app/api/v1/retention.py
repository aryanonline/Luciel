"""
Retention policy admin routes.

All routes require admin access and are rate-limited.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.deps import DbSession
from app.middleware.rate_limit import (
    limiter,
    get_api_key_or_ip,
    ADMIN_RATE_LIMIT,
)
from app.models.retention import RetentionPolicy
from app.policy.retention import RetentionService
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


# ---- Policy CRUD ----

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
) -> RetentionPolicyRead:
    repo = RetentionRepository(db)

    existing = repo.get_policy_for_category(
        data_category=payload.data_category,
        tenant_id=payload.tenant_id,
    )
    if existing and existing.tenant_id == payload.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Policy for '{payload.data_category}' already exists"
                   f" (tenant={payload.tenant_id})",
        )

    policy = RetentionPolicy(
        tenant_id=payload.tenant_id,
        data_category=payload.data_category,
        retention_days=payload.retention_days,
        action=payload.action,
        purpose=payload.purpose,
        created_by=payload.created_by,
    )
    created = repo.create_policy(policy)
    return RetentionPolicyRead.model_validate(created)


@router.get("/policies", response_model=list[RetentionPolicyRead])
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def list_policies(
    request: Request,
    db: DbSession,
    tenant_id: str | None = Query(default=None),
) -> list[RetentionPolicyRead]:
    repo = RetentionRepository(db)
    policies = repo.list_policies(tenant_id=tenant_id)
    return [RetentionPolicyRead.model_validate(p) for p in policies]


@router.get("/policies/{policy_id}", response_model=RetentionPolicyRead)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def get_policy(
    request: Request,
    policy_id: int,
    db: DbSession,
) -> RetentionPolicyRead:
    repo = RetentionRepository(db)
    policy = repo.get_policy(policy_id)
    if not policy:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Policy not found",
        )
    return RetentionPolicyRead.model_validate(policy)


@router.patch("/policies/{policy_id}", response_model=RetentionPolicyRead)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def update_policy(
    request: Request,
    policy_id: int,
    payload: RetentionPolicyUpdate,
    db: DbSession,
) -> RetentionPolicyRead:
    repo = RetentionRepository(db)
    policy = repo.update_policy(
        policy_id,
        **payload.model_dump(exclude_unset=True),
    )
    if not policy:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Policy not found",
        )
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
) -> None:
    repo = RetentionRepository(db)
    success = repo.delete_policy(policy_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Policy not found",
        )


# ---- Deletion Logs ----

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
    logs = repo.list_deletion_logs(
        tenant_id=tenant_id,
        data_category=data_category,
        limit=limit,
    )
    return [DeletionLogRead.model_validate(log) for log in logs]


# ---- Enforcement ----

@router.post("/enforce", response_model=list[EnforceResult])
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def enforce_policies(
    request: Request,
    db: DbSession,
    tenant_id: str | None = Query(default=None),
) -> list[EnforceResult]:
    service = _get_retention_service(db)

    if tenant_id:
        results = service.enforce_for_tenant(
            tenant_id=tenant_id,
            triggered_by="admin",
        )
    else:
        results = service.enforce_all_policies(triggered_by="admin")

    return [EnforceResult(**r) for r in results]


@router.post("/purge", response_model=EnforceResult)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def manual_purge(
    request: Request,
    payload: ManualPurgeRequest,
    db: DbSession,
) -> EnforceResult:
    service = _get_retention_service(db)

    try:
        result = service.manual_purge(
            data_category=payload.data_category,
            tenant_id=payload.tenant_id,
            reason=payload.reason,
            triggered_by="admin",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return EnforceResult(**result)