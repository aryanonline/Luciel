"""
Consent API endpoints.

POST /api/v1/consent/grant   — user grants consent
POST /api/v1/consent/withdraw — user withdraws consent
GET  /api/v1/consent/status   — check current consent state
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_consent_repository
from app.repositories.consent_repository import ConsentRepository
from app.schemas.consent import (
    ConsentActionResponse,
    ConsentGrantRequest,
    ConsentStatusResponse,
    ConsentWithdrawRequest,
)

router = APIRouter(prefix="/api/v1/consent", tags=["consent"])


@router.post("/grant", response_model=ConsentActionResponse)
def grant_consent(
    body: ConsentGrantRequest,
    repo: Annotated[ConsentRepository, Depends(get_consent_repository)],
):
    repo.grant_consent(
        user_id=body.user_id,
        tenant_id=body.tenant_id,
        consent_type=body.consent_type,
        collection_method=body.collection_method,
        consent_text=body.consent_text,
        consent_context=body.consent_context,
    )
    return ConsentActionResponse(
        status="granted",
        message="Consent recorded. Luciel will now remember your preferences.",
    )


@router.post("/withdraw", response_model=ConsentActionResponse)
def withdraw_consent(
    body: ConsentWithdrawRequest,
    repo: Annotated[ConsentRepository, Depends(get_consent_repository)],
):
    result = repo.withdraw_consent(
        user_id=body.user_id,
        tenant_id=body.tenant_id,
        consent_type=body.consent_type,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="No consent record found")
    return ConsentActionResponse(
        status="withdrawn",
        message="Consent withdrawn. Luciel will no longer persist new memories.",
    )


@router.get("/status", response_model=ConsentStatusResponse)
def consent_status(
    user_id: str,
    tenant_id: str,
    consent_type: str = "memory_persistence",
    repo: ConsentRepository = Depends(get_consent_repository),
):
    record = repo.get_consent(
        user_id=user_id,
        tenant_id=tenant_id,
        consent_type=consent_type,
    )
    if record is None:
        return ConsentStatusResponse(
            user_id=user_id,
            tenant_id=tenant_id,
            consent_type=consent_type,
            granted=False,
        )
    return ConsentStatusResponse(
        user_id=record.user_id,
        tenant_id=record.tenant_id,
        consent_type=record.consent_type,
        granted=record.granted,
        collection_method=record.collection_method,
        granted_at=str(record.created_at) if record.created_at else None,
    )