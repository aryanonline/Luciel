"""
Admin audit-log read API (Step 28 Phase 2 — Commit 2).

Resolves canonical-recap §4.1 item 4: "/api/v1/admin/audit-log returns
404 currently". Mounts under the existing /api/v1/admin prefix family
behind the same admin-permission gate enforced in ApiKeyAuthMiddleware
for all /api/v1/admin/* paths.

Surface (read-only — append-only at the data layer):
  GET /api/v1/admin/audit-log
       Tenant-scoped paginated listing. Platform-admin may filter by
       any tenant_id; non-platform-admin callers are forced to their
       own tenant_id.

  GET /api/v1/admin/audit-log/resource/{resource_type}/{resource_pk}
       History of a specific resource over time. Tenant-scoped via
       the resource's own tenant on each row (filtered defensively
       even though the index is resource-keyed).

  GET /api/v1/admin/audit-log/actor/{actor_key_prefix}
       Forensic view: all rows written by an actor key prefix.
       Platform-admin only (cross-tenant by definition).

Security contract:
- No mutation routes. The audit log is append-only by DB grant
  (Phase 2 worker role swap completes the DB-layer enforcement; the
  API surface enforces it at the HTTP layer today).
- Raw API keys are never returned. Only the 12-char key_prefix.
- before_json / after_json may contain operator-facing config diffs
  but never user content (chat messages, knowledge body) — those
  live in trace / message / knowledge tables, not here. PII risk is
  bounded to operator labels (display_name, escalation_contact)
  which the operator already chose to expose.
- Rate-limited via the same ADMIN_RATE_LIMIT bucket as every other
  admin endpoint to prevent log-scraping abuse.
- All filter args are validated as bounded enums or length-bounded
  strings via pydantic / FastAPI Query — no raw SQL composition,
  no LIKE injection surface (we only use IN / equality).
"""
from __future__ import annotations

from typing import Annotated, Iterable

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.deps import DbSession, get_admin_audit_repository
from app.middleware.rate_limit import (
    ADMIN_RATE_LIMIT,
    get_api_key_or_ip,
    limiter,
)
from app.models.admin_audit_log import (
    ALLOWED_ACTIONS,
    ALLOWED_RESOURCE_TYPES,
    AdminAuditLog,
)
from app.policy.scope import ScopePolicy
from app.repositories.admin_audit_repository import AdminAuditRepository
from app.schemas.audit_log import AdminAuditLogPage, AdminAuditLogRead


router = APIRouter(prefix="/admin/audit-log", tags=["admin", "audit-log"])


# Hard cap to prevent unbounded scans even with the supporting indexes.
# 500 covers any reasonable single-page audit dashboard view; deeper
# history uses offset pagination, not a single huge page.
MAX_LIMIT = 500
DEFAULT_LIMIT = 100


def _validate_actions(actions: Iterable[str] | None) -> tuple[str, ...] | None:
    """Allow-list any caller-supplied action filter against the
    canonical ALLOWED_ACTIONS tuple. Unknown actions raise 400 rather
    than silently returning empty results — silent emptiness would
    mask client bugs that misspell action names.
    """
    if not actions:
        return None
    bad = [a for a in actions if a not in ALLOWED_ACTIONS]
    if bad:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown action filter values: {bad}",
        )
    return tuple(actions)


def _validate_resource_types(resource_types: Iterable[str] | None) -> tuple[str, ...] | None:
    if not resource_types:
        return None
    bad = [r for r in resource_types if r not in ALLOWED_RESOURCE_TYPES]
    if bad:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown resource_type filter values: {bad}",
        )
    return tuple(resource_types)


# ---------------------------------------------------------------------
# GET /admin/audit-log
# ---------------------------------------------------------------------

@router.get("", response_model=AdminAuditLogPage)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def list_audit_log(
    request: Request,
    db: DbSession,
    repo: Annotated[AdminAuditRepository, Depends(get_admin_audit_repository)],
    tenant_id: str | None = Query(
        default=None,
        min_length=2,
        max_length=100,
        description=(
            "Tenant whose audit rows to return. Platform-admin may "
            "supply any tenant_id; non-platform-admin callers have "
            "this argument forced to their own tenant_id regardless "
            "of what they pass."
        ),
    ),
    action: list[str] | None = Query(
        default=None,
        description=(
            "Filter by action verb. Repeat the query parameter to "
            "supply multiple. Values must come from ALLOWED_ACTIONS."
        ),
    ),
    resource_type: list[str] | None = Query(
        default=None,
        description=(
            "Filter by resource type. Repeat the query parameter to "
            "supply multiple. Values must come from "
            "ALLOWED_RESOURCE_TYPES."
        ),
    ),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> AdminAuditLogPage:
    """List audit rows for a tenant, most-recent-first.

    Authorization:
      - Platform-admin: may query any tenant. If `tenant_id` is
        omitted, defaults to the platform sentinel ('platform') so
        platform-level system actions are surfaced.
      - Tenant-admin (or any non-platform-admin admin): tenant_id is
        forced to the caller's own tenant_id. Any value passed in the
        query string is ignored. This is defense-in-depth on top of
        ApiKeyAuthMiddleware's admin-perm gate.
    """
    # Defense-in-depth: tenant scoping at the API layer in addition
    # to the middleware's admin-perm check.
    if not ScopePolicy.is_platform_admin(request):
        # Force tenant filter to caller's own tenant. Caller-supplied
        # tenant_id (even if it matches) is ignored to make this
        # behavior trivially auditable: non-platform-admin can NEVER
        # see audit rows outside their tenant, regardless of input.
        caller_tenant = getattr(request.state, "tenant_id", None)
        if caller_tenant is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Caller has no tenant_id on the API key; cannot "
                    "infer which tenant's audit log to return."
                ),
            )
        tenant_id = caller_tenant
    else:
        # Platform-admin: default to platform-sentinel tenant when
        # no filter supplied. This surfaces system actions (the
        # 'platform' sentinel from AdminAuditRepository) which are
        # otherwise invisible.
        if tenant_id is None:
            tenant_id = "platform"

    actions_tuple = _validate_actions(action)
    resource_types_tuple = _validate_resource_types(resource_type)

    rows: list[AdminAuditLog] = repo.list_for_tenant(
        tenant_id=tenant_id,
        limit=limit,
        offset=offset,
        actions=actions_tuple,
        resource_types=resource_types_tuple,
    )

    items = [AdminAuditLogRead.model_validate(r) for r in rows]
    return AdminAuditLogPage(
        items=items,
        limit=limit,
        offset=offset,
        returned=len(items),
    )


# ---------------------------------------------------------------------
# GET /admin/audit-log/resource/{resource_type}/{resource_pk}
# ---------------------------------------------------------------------

@router.get(
    "/resource/{resource_type}/{resource_pk}",
    response_model=list[AdminAuditLogRead],
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def list_audit_log_for_resource(
    request: Request,
    resource_type: str,
    resource_pk: int,
    db: DbSession,
    repo: Annotated[AdminAuditRepository, Depends(get_admin_audit_repository)],
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> list[AdminAuditLogRead]:
    """All audit events for one specific resource over time.

    Powers the per-resource history pane on the Step 31 dashboard.

    Authorization:
      - Platform-admin: full access.
      - Non-platform-admin: results are post-filtered to the caller's
        tenant_id. The tenant_time index doesn't cover this query
        shape, but resource_pk is high-selectivity (PK-narrow) so
        the post-filter is cheap.
    """
    if resource_type not in ALLOWED_RESOURCE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown resource_type {resource_type!r}",
        )

    rows: list[AdminAuditLog] = repo.list_for_resource(
        resource_type=resource_type,
        resource_pk=resource_pk,
        limit=limit,
    )

    if not ScopePolicy.is_platform_admin(request):
        caller_tenant = getattr(request.state, "tenant_id", None)
        if caller_tenant is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Caller has no tenant_id on the API key; cannot "
                    "filter resource audit history."
                ),
            )
        rows = [r for r in rows if r.tenant_id == caller_tenant]

    return [AdminAuditLogRead.model_validate(r) for r in rows]


# ---------------------------------------------------------------------
# GET /admin/audit-log/actor/{actor_key_prefix}
# ---------------------------------------------------------------------

@router.get(
    "/actor/{actor_key_prefix}",
    response_model=list[AdminAuditLogRead],
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def list_audit_log_for_actor(
    request: Request,
    actor_key_prefix: str,
    db: DbSession,
    repo: Annotated[AdminAuditRepository, Depends(get_admin_audit_repository)],
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> list[AdminAuditLogRead]:
    """Forensic view: every row written by an actor key prefix.

    Cross-tenant by definition (a single key prefix lives within one
    tenant by construction, but querying without a tenant filter is
    a forensic operation, not an operational one). Platform-admin
    only.
    """
    if not ScopePolicy.is_platform_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only platform_admin may query audit rows by actor "
                "key prefix (cross-tenant by construction)."
            ),
        )

    # Light input validation — key prefixes are 12 chars in our
    # current scheme but we accept up to 20 to match the column
    # width and tolerate prefix-format evolution.
    if not actor_key_prefix or len(actor_key_prefix) > 20:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="actor_key_prefix must be 1..20 characters",
        )

    rows: list[AdminAuditLog] = repo.list_for_actor(
        actor_key_prefix=actor_key_prefix,
        limit=limit,
    )
    return [AdminAuditLogRead.model_validate(r) for r in rows]
