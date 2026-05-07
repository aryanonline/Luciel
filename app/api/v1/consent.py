"""Consent API endpoints.

POST /api/v1/consent/grant      -- user grants consent
POST /api/v1/consent/withdraw   -- user withdraws consent
GET  /api/v1/consent/status     -- check current consent state

Step 29.y Cluster 1 (G-3 + G-7 resolution)
==========================================

Pre-29.y, all three routes accepted ``tenant_id`` from the request body
and trusted it. There was no rate limit, no scope enforcement, and no
audit row written for grant/withdraw. This was a PIPEDA-deceptive
forgery surface: a holder of any tenant-A API key could write to
tenant-B's user_consents table by simply passing ``tenant_id="tenant-B"``
in the body. See findings_phase1g.md G-3 for the four documented
attacks (cross-tenant forgery, withdrawal-DoS, status enumeration,
table-flood).

The hardened contract:

  1. ``tenant_id`` is derived from ``request.state.tenant_id`` (set by
     the auth middleware from the API key). It can never come from the
     request body for non-platform-admin callers. Platform-admin keys
     MAY override via the body but the override is logged and audited.
  2. Every route is rate-limited (``CHAT_RATE_LIMIT`` for grant/withdraw,
     which are user-initiated mutations bound to chat-turn cadence;
     ``ADMIN_RATE_LIMIT`` for status, which is read-only and rare).
  3. ``grant`` and ``withdraw`` write an ``admin_audit_logs`` row BEFORE
     committing the consent mutation, using the same audit-first-then-
     mutate pattern locked in admin_forensics.py:779-800. ``status`` is
     read-only and intentionally does not audit (volume noise; matches
     the same convention applied to GET /admin/verification).
  4. The body still carries ``user_id`` because that is the chat
     end-user identifier (free-form, supplied by the caller's client),
     distinct from the platform User UUID. ``tenant_id`` in the body
     becomes optional (default None); if supplied AND the caller is
     not platform_admin AND it does not match the key's tenant, the
     request is rejected with 403.
  5. The pre-29.y file had a UTF-8 BOM and Windows-saved smart-character
     em-dashes (``--"``) at the top. Both are gone -- this file is now
     ASCII-only and matches the repo's editor conventions.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.deps import (
    DbSession,
    get_admin_audit_repository,
    get_audit_context,
    get_consent_repository,
)
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)
from app.middleware.rate_limit import (
    ADMIN_RATE_LIMIT,
    CHAT_RATE_LIMIT,
    get_api_key_or_ip,
    limiter,
)
from app.models.admin_audit_log import (
    ACTION_CONSENT_GRANT,
    ACTION_CONSENT_WITHDRAW,
    RESOURCE_CONSENT,
)
from app.policy.scope import ScopePolicy
from app.repositories.consent_repository import ConsentRepository
from app.schemas.consent import (
    ConsentActionResponse,
    ConsentGrantRequest,
    ConsentStatusResponse,
    ConsentWithdrawRequest,
)

router = APIRouter(prefix="/consent", tags=["consent"])


def _resolve_tenant_id(request: Request, body_tenant_id: str | None) -> str:
    """Derive the effective tenant_id for a consent mutation.

    Rules:
      * Non-platform-admin: tenant_id comes from the API key. If the
        body also supplies a tenant_id and it does not match, reject.
      * Platform-admin: may target any tenant. If the body supplies one
        we use it; otherwise fall back to the key's tenant binding (if
        any). If neither is set, reject -- platform_admin keys without
        an explicit target tenant cannot be implicit.

    Raises HTTPException(403) on cross-tenant attempts by non-platform
    callers, HTTPException(400) when neither path can resolve.
    """
    key_tenant_id = getattr(request.state, "tenant_id", None)
    is_platform = ScopePolicy.is_platform_admin(request)

    if not is_platform:
        if key_tenant_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "missing_tenant_scope",
                    "message": (
                        "API key has no tenant binding; consent routes "
                        "require a tenant-scoped key."
                    ),
                },
            )
        if body_tenant_id is not None and body_tenant_id != key_tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "cross_tenant_denied",
                    "message": (
                        "tenant_id in request body does not match the "
                        "calling API key's tenant scope."
                    ),
                },
            )
        return key_tenant_id

    # Platform-admin path.
    effective = body_tenant_id or key_tenant_id
    if not effective:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "tenant_id_required",
                "message": (
                    "Platform-admin consent calls must specify "
                    "tenant_id in the request body."
                ),
            },
        )
    return effective


@router.post("/grant", response_model=ConsentActionResponse)
@limiter.limit(CHAT_RATE_LIMIT, key_func=get_api_key_or_ip)
def grant_consent(
    request: Request,
    body: ConsentGrantRequest,
    db: DbSession,
    repo: Annotated[ConsentRepository, Depends(get_consent_repository)],
    audit_repo: Annotated[
        AdminAuditRepository, Depends(get_admin_audit_repository)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> ConsentActionResponse:
    """Record that a user has granted consent for a given consent_type.

    Auditable: writes an ACTION_CONSENT_GRANT row before committing.
    Idempotent: re-granting an already-granted record updates the
    collection_method/consent_text/consent_context fields on the
    existing row.
    """
    effective_tenant_id = _resolve_tenant_id(request, body.tenant_id)

    existing = repo.get_consent(
        user_id=body.user_id,
        tenant_id=effective_tenant_id,
        consent_type=body.consent_type,
    )
    before = (
        {"granted": existing.granted, "collection_method": existing.collection_method}
        if existing is not None
        else None
    )

    # Audit FIRST -- if record() raises (action not in ALLOWED_ACTIONS,
    # FK violation, etc.), the consent mutation below never executes.
    # Same invariant locked at admin_forensics.py line 779.
    audit_repo.record(
        ctx=audit_ctx,
        tenant_id=effective_tenant_id,
        action=ACTION_CONSENT_GRANT,
        resource_type=RESOURCE_CONSENT,
        resource_pk=existing.id if existing is not None else None,
        resource_natural_id=(
            f"{effective_tenant_id}:{body.user_id}:{body.consent_type}"
        ),
        before=before,
        after={
            "granted": True,
            "collection_method": body.collection_method,
        },
        note="step-29y-c1-consent-grant",
        autocommit=False,
    )

    record = repo.grant_consent(
        user_id=body.user_id,
        tenant_id=effective_tenant_id,
        consent_type=body.consent_type,
        collection_method=body.collection_method,
        consent_text=body.consent_text,
        consent_context=body.consent_context,
    )
    # repo.grant_consent does its own db.commit() which flushes the audit
    # row alongside the consent row in the same transaction. If the repo
    # ever stops auto-committing, the explicit db.commit() below picks up
    # the slack so the audit row never lingers uncommitted.
    if db.in_transaction():
        db.commit()
    _ = record  # repo committed; surface stable response below
    return ConsentActionResponse(
        status="granted",
        message="Consent recorded. Luciel will now remember your preferences.",
    )


@router.post("/withdraw", response_model=ConsentActionResponse)
@limiter.limit(CHAT_RATE_LIMIT, key_func=get_api_key_or_ip)
def withdraw_consent(
    request: Request,
    body: ConsentWithdrawRequest,
    db: DbSession,
    repo: Annotated[ConsentRepository, Depends(get_consent_repository)],
    audit_repo: Annotated[
        AdminAuditRepository, Depends(get_admin_audit_repository)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> ConsentActionResponse:
    """Withdraw a previously-granted consent.

    Auditable: writes an ACTION_CONSENT_WITHDRAW row before committing.
    404 if no consent record exists for (user_id, tenant_id, consent_type).
    """
    effective_tenant_id = _resolve_tenant_id(request, body.tenant_id)

    existing = repo.get_consent(
        user_id=body.user_id,
        tenant_id=effective_tenant_id,
        consent_type=body.consent_type,
    )
    if existing is None:
        # 404 BEFORE audit: there is no resource to mutate, so writing
        # an audit row would be misleading. The 404 itself is not a
        # state change.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No consent record found",
        )

    audit_repo.record(
        ctx=audit_ctx,
        tenant_id=effective_tenant_id,
        action=ACTION_CONSENT_WITHDRAW,
        resource_type=RESOURCE_CONSENT,
        resource_pk=existing.id,
        resource_natural_id=(
            f"{effective_tenant_id}:{body.user_id}:{body.consent_type}"
        ),
        before={"granted": existing.granted},
        after={"granted": False},
        note="step-29y-c1-consent-withdraw",
        autocommit=False,
    )

    repo.withdraw_consent(
        user_id=body.user_id,
        tenant_id=effective_tenant_id,
        consent_type=body.consent_type,
    )
    if db.in_transaction():
        db.commit()
    return ConsentActionResponse(
        status="withdrawn",
        message="Consent withdrawn. Luciel will no longer persist new memories.",
    )


@router.get("/status", response_model=ConsentStatusResponse)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def consent_status(
    request: Request,
    user_id: Annotated[str, Query(min_length=1, max_length=200)],
    repo: Annotated[ConsentRepository, Depends(get_consent_repository)],
    tenant_id: Annotated[str | None, Query(max_length=100)] = None,
    consent_type: Annotated[str, Query(max_length=100)] = "memory_persistence",
) -> ConsentStatusResponse:
    """Return current consent state for a user under the caller's tenant.

    Read-only; does NOT audit (volume noise; matches the same convention
    applied to GET /admin/verification). Cross-tenant enumeration is
    blocked by ``_resolve_tenant_id`` -- a tenant-A key cannot read
    tenant-B's consent records.
    """
    effective_tenant_id = _resolve_tenant_id(request, tenant_id)
    record = repo.get_consent(
        user_id=user_id,
        tenant_id=effective_tenant_id,
        consent_type=consent_type,
    )
    if record is None:
        return ConsentStatusResponse(
            user_id=user_id,
            tenant_id=effective_tenant_id,
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
