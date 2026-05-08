"""Session API routes.

Step 29.y Cluster 1 (G-4 resolution)
====================================

Pre-29.y, the four session routes had no rate limit and the POST route
trusted ``payload.tenant_id`` whenever the API key had no tenant binding
of its own. The auth middleware enforces ``tenant_id NOT NULL`` on every
key in steady state, but findings_phase1f F-7 noted there is no FK
backing that constraint, so a hand-edited orphaned key could land a
session under an arbitrary tenant. There was also no audit row for
the privileged platform_admin cross-tenant case.

Hardened contract:

  1. Every route is rate-limited. ``CHAT_RATE_LIMIT`` for the POST
     (session creation is per-conversation; chat-rate appropriate) and
     for the per-session GETs that the chat client polls. ``ADMIN_RATE_LIMIT``
     for the list route, which is operational.
  2. POST ``/sessions``:
       * If the API key has a tenant binding, it wins. Body-supplied
         ``tenant_id`` is ignored unless the caller is platform_admin
         AND it differs (in which case audit is required).
       * If the API key has NO tenant binding, the caller MUST be
         platform_admin AND MUST supply a tenant_id in the body. Any
         non-platform key without a tenant binding is rejected (it
         shouldn't be possible per F-7, but defense in depth).
       * Privileged cross-tenant creation (platform_admin acting on a
         tenant other than its own key binding) writes an audit row
         BEFORE the session row is committed.
  3. GET routes (list, get-one, list-messages) enforce that the
     returned session belongs to the caller's tenant. Cross-tenant
     reads return 404 (not 403) to avoid leaking session-id existence.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.deps import (
    DbSession,
    get_admin_audit_repository,
    get_audit_context,
    get_session_service,
)
from app.middleware.rate_limit import (
    ADMIN_RATE_LIMIT,
    CHAT_RATE_LIMIT,
    get_api_key_or_ip,
    limiter,
)
from app.models.admin_audit_log import (
    ACTION_SESSION_CREATE_CROSS_TENANT,
    RESOURCE_SESSION,
)
from app.policy.scope import ScopePolicy
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)
from app.schemas.session import MessageRead, SessionCreate, SessionRead
from app.services.session_service import SessionService

router = APIRouter(tags=["sessions"])


def _resolve_session_tenant(
    request: Request, payload_tenant_id: str | None
) -> tuple[str, bool]:
    """Resolve the session's effective tenant_id and report cross-tenant flag.

    Returns ``(effective_tenant_id, is_cross_tenant)``. The ``is_cross_tenant``
    flag is True only when a platform_admin caller is acting on a tenant
    other than their own key binding (or has no key binding at all). The
    POST route uses the flag to decide whether an audit row is required.
    """
    key_tenant_id = getattr(request.state, "tenant_id", None)
    is_platform = ScopePolicy.is_platform_admin(request)

    if key_tenant_id is not None:
        # Key is tenant-scoped. Body tenant_id is advisory; reject any
        # mismatch unless the caller is platform_admin acting cross-tenant.
        if (
            payload_tenant_id is not None
            and payload_tenant_id != key_tenant_id
        ):
            if not is_platform:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "code": "cross_tenant_denied",
                        "message": (
                            "tenant_id in payload does not match the "
                            "calling API key's tenant scope."
                        ),
                    },
                )
            return payload_tenant_id, True
        return key_tenant_id, False

    # Key has no tenant binding. Only platform_admin may proceed.
    if not is_platform:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "missing_tenant_scope",
                "message": (
                    "API key has no tenant binding; sessions require a "
                    "tenant-scoped key or platform_admin."
                ),
            },
        )
    if not payload_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "tenant_id_required",
                "message": (
                    "Platform-admin session creation requires "
                    "tenant_id in the request body."
                ),
            },
        )
    return payload_tenant_id, True


@router.post(
    "",
    response_model=SessionRead,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(CHAT_RATE_LIMIT, key_func=get_api_key_or_ip)
def create_session(
    request: Request,
    payload: SessionCreate,
    db: DbSession,
    service: Annotated[SessionService, Depends(get_session_service)],
    audit_repo: Annotated[
        AdminAuditRepository, Depends(get_admin_audit_repository)
    ],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> SessionRead:
    """Create a new session.

    tenant_id, domain_id, and agent_id come from the API key for
    tenant-scoped callers. Platform-admin keys may target a different
    tenant by passing tenant_id in the body, but every cross-tenant
    creation writes an ACTION_SESSION_CREATE_CROSS_TENANT audit row
    first.
    """
    key_domain_id = getattr(request.state, "domain_id", None)
    key_agent_id = getattr(request.state, "agent_id", None)

    effective_tenant_id, is_cross_tenant = _resolve_session_tenant(
        request, payload.tenant_id
    )

    domain_id = key_domain_id or payload.domain_id
    agent_id = key_agent_id or payload.agent_id

    if not domain_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="domain_id is required (from API key or request body)",
        )

    # Domain/agent locks: a tenant-scoped key with a specific domain
    # cannot create a session under a different domain (same rule
    # for agent). These checks are unchanged from pre-29.y.
    if key_domain_id and payload.domain_id and payload.domain_id != key_domain_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"API key is locked to domain '{key_domain_id}'",
        )
    if key_agent_id and payload.agent_id and payload.agent_id != key_agent_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"API key is locked to agent '{key_agent_id}'",
        )

    # Audit FIRST for the privileged cross-tenant case. Tenant-scoped
    # session creation is ordinary chat traffic and intentionally does
    # NOT audit -- only the platform_admin override does, because that
    # is the only operationally privileged path.
    if is_cross_tenant:
        key_tenant_id = getattr(request.state, "tenant_id", None)
        audit_repo.record(
            ctx=audit_ctx,
            tenant_id=effective_tenant_id,
            action=ACTION_SESSION_CREATE_CROSS_TENANT,
            resource_type=RESOURCE_SESSION,
            resource_pk=None,  # Not yet created.
            resource_natural_id=None,
            domain_id=domain_id,
            agent_id=agent_id,
            before=None,
            after={
                "key_tenant_id": key_tenant_id,
                "target_tenant_id": effective_tenant_id,
                "user_id": payload.user_id,
                "channel": payload.channel,
            },
            note="step-29y-c1-cross-tenant-session-create",
            autocommit=False,
        )

    session = service.create_session(
        tenant_id=effective_tenant_id,
        domain_id=domain_id,
        agent_id=agent_id,
        user_id=payload.user_id,
        channel=payload.channel,
    )
    if db.in_transaction():
        db.commit()
    return SessionRead.model_validate(session)


@router.get("", response_model=list[SessionRead])
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def list_sessions(
    request: Request,
    service: Annotated[SessionService, Depends(get_session_service)],
    tenant_id: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[SessionRead]:
    """List sessions scoped to the caller's tenant.

    Non-platform callers cannot override the tenant filter; platform
    admins may. Cross-tenant override attempts by tenant-scoped keys
    are silently downgraded to the caller's own tenant (matches the
    same convention used in audit_log.py:list_audit_log).
    """
    key_tenant_id = getattr(request.state, "tenant_id", None)
    if ScopePolicy.is_platform_admin(request):
        effective_tenant_id = tenant_id or key_tenant_id
    else:
        effective_tenant_id = key_tenant_id
    sessions = service.list_sessions(
        tenant_id=effective_tenant_id, user_id=user_id, limit=limit,
    )
    return [SessionRead.model_validate(item) for item in sessions]


@router.get("/{session_id}", response_model=SessionRead)
@limiter.limit(CHAT_RATE_LIMIT, key_func=get_api_key_or_ip)
def get_session(
    request: Request,
    session_id: str,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> SessionRead:
    """Read one session, scoped to the caller's tenant.

    Returns 404 (not 403) on cross-tenant access so a tenant-A holder
    cannot probe for the existence of a tenant-B session_id.
    """
    key_tenant_id = getattr(request.state, "tenant_id", None)
    is_platform = ScopePolicy.is_platform_admin(request)
    session = service.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    if (
        not is_platform
        and key_tenant_id is not None
        and session.tenant_id != key_tenant_id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    return SessionRead.model_validate(session)


@router.get("/{session_id}/messages", response_model=list[MessageRead])
@limiter.limit(CHAT_RATE_LIMIT, key_func=get_api_key_or_ip)
def list_messages(
    request: Request,
    session_id: str,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> list[MessageRead]:
    """Read messages for one session, scoped to the caller's tenant."""
    key_tenant_id = getattr(request.state, "tenant_id", None)
    is_platform = ScopePolicy.is_platform_admin(request)
    session = service.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    if (
        not is_platform
        and key_tenant_id is not None
        and session.tenant_id != key_tenant_id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    messages = service.list_messages(session_id)
    return [MessageRead.model_validate(item) for item in messages]
