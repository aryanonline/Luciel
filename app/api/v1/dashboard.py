"""Step 31 sub-branch 3: hierarchical dashboard HTTP surface.

Three GET endpoints, one per scope level, that wrap the read-only
`DashboardService` (sub-branch 2) behind the `ScopePolicy` chain
(`enforce_tenant_scope` / `enforce_domain_scope` / `enforce_agent_scope`).
Cross-tenant access is denied by `ScopePolicy`; embed keys are denied at
the perimeter by `ApiKeyAuthMiddleware` because this router mounts under
`ADMIN_AUTH_PATHS` (`/api/v1/dashboard`) and embed keys carry
`EMBED_REQUIRED_PERMISSIONS = {"chat"}` only — they cannot also carry
`"admin"`, so the middleware rejects them with 403 before any route
handler runs. The same gate that blocks embed keys from `/api/v1/admin/*`
blocks them here.

The three endpoints:

  - GET /api/v1/dashboard/tenant
        Returns a TenantDashboard rollup for the caller's tenant.
        Platform-admin callers MAY pass `?tenant_id=...` to read any
        tenant; non-platform-admin callers MUST omit the query param
        (their key's tenant scope is used). Passing a tenant_id that
        does not match the caller's scope is rejected by
        `ScopePolicy.enforce_tenant_scope`.

  - GET /api/v1/dashboard/domain/{domain_id}
        Returns a DomainDashboard rollup. The tenant is derived from
        the caller's scope (or `?tenant_id=...` for platform_admin).
        Domain-scoped keys whose `caller_domain != domain_id` are
        rejected by `enforce_domain_scope`.

  - GET /api/v1/dashboard/agent/{agent_id}
        Returns an AgentDashboard rollup. Tenant + domain are derived
        from caller scope (or `?tenant_id=` / `?domain_id=` for
        platform_admin). Agent-scoped keys whose `caller_agent !=
        agent_id` are rejected by `enforce_agent_scope`.

Response shape
--------------

Each endpoint returns a JSON object produced via `dataclasses.asdict()`
applied to the corresponding dataclass result from `DashboardService`.
The dataclasses are frozen (sub-branch 2), so the shape is fixed at the
service boundary and cannot drift between HTTP envelope and service
result. FastAPI's default JSON encoder handles primitives + nested dicts
+ lists. The fields are documented in the dataclasses in
`app/services/dashboard_service.py`.

Step 32 (UI) will render against this envelope. To keep that render
loop honest, do NOT add convenience fields to the HTTP envelope that
are not on the dataclasses; if a field is needed, add it to the
dataclass (and contract test) first, then surface it here for free.

Rate limiting
-------------

The same `ADMIN_RATE_LIMIT` ("30/minute" — `app/middleware/rate_limit.py`)
that applies to `/api/v1/admin/*` is applied here. Dashboards are admin
reads and share the admin envelope.

Audit emission
--------------

Dashboard reads do NOT emit `admin_audit_logs` rows. Audit log emission
is reserved for state-changing admin actions (Step 24 / 24.5 / 28
contract). Read-side observability for dashboards lives in the trace
table itself — every turn that lands in the dashboard came through
`trace_service`, which is itself the audit trail for the underlying
events. Forensic surfaces (Step 29) consume traces directly.

Pattern E note
--------------

No row mutations. Pure read-side handlers. Closing-tag for Step 31 is
reserved for the sub-branch 5 doc-truthing commit (per Step 24.5c /
Step 30c precedent) — DO NOT cut a tag here.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.deps import DbSession
from app.middleware.rate_limit import (
    ADMIN_RATE_LIMIT,
    get_api_key_or_ip,
    limiter,
)
from app.policy.scope import ScopePolicy
from app.services.dashboard_service import DashboardService

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/dashboard", tags=["dashboard"])


# --------------------------------------------------------------------- #
# DI
# --------------------------------------------------------------------- #


def get_dashboard_service(db: DbSession) -> DashboardService:
    """FastAPI dependency that yields a `DashboardService` bound to the
    request-scoped SQLAlchemy session. Mirrors the `get_X_service` pattern
    used elsewhere in `app/api/deps.py`. Lives here, not in `deps.py`, to
    keep the dashboard router self-contained at v1 — if a second consumer
    of `DashboardService` appears, this hops over to `deps.py` then.
    """
    return DashboardService(db)


DashboardServiceDep = Annotated[DashboardService, Depends(get_dashboard_service)]


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _resolve_tenant_id(request: Request, query_tenant_id: str | None) -> str:
    """Resolve the target tenant_id for a dashboard read.

    Non-platform-admin callers: their key's tenant scope is authoritative;
    a `?tenant_id=` that contradicts the key is rejected by
    `enforce_tenant_scope` below. Omitting the query param is fine —
    we just use the caller's tenant.

    Platform-admin callers: MUST supply `?tenant_id=` because their
    key carries no tenant scope.

    Returns the resolved string; raises HTTP 400 if neither source
    yields one.
    """
    caller_tenant = getattr(request.state, "tenant_id", None)
    target = query_tenant_id or caller_tenant
    if not target:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "tenant_id is required: platform-admin callers must "
                "pass it as a query parameter; scope-bound callers "
                "use their key's tenant by default"
            ),
        )
    return target


def _resolve_domain_id(request: Request, query_domain_id: str | None) -> str:
    """Resolve the target domain_id when an endpoint needs one but the
    path didn't supply it (the agent endpoint). Mirrors `_resolve_tenant_id`.
    """
    caller_domain = getattr(request.state, "domain_id", None)
    target = query_domain_id or caller_domain
    if not target:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "domain_id is required: platform-admin / tenant-admin "
                "callers must pass it as a query parameter; "
                "domain-scoped callers use their key's domain by default"
            ),
        )
    return target


def _to_envelope(result: Any) -> dict[str, Any]:
    """Convert a frozen dataclass dashboard result into a JSON-serializable
    dict using `dataclasses.asdict`. Nested dataclasses (ScopeAggregates,
    TrendBucket, ChildRollup) are converted recursively by stdlib.
    """
    return asdict(result)


# --------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------- #


@router.get("/tenant")
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def get_tenant_dashboard(
    request: Request,
    service: DashboardServiceDep,
    tenant_id: str | None = Query(
        default=None,
        description=(
            "Target tenant. Required for platform-admin callers; "
            "ignored (defaults to caller's tenant) for scope-bound callers."
        ),
    ),
    window_days: int = Query(default=7, ge=1, le=90),
    top_n: int = Query(default=5, ge=1, le=50),
) -> dict[str, Any]:
    """Return the tenant-scope dashboard rollup.

    The `ScopePolicy.enforce_tenant_scope` call below is the
    authoritative isolation check — even if the middleware admitted the
    request (admin permission present), this still denies cross-tenant
    reads.
    """
    target_tenant = _resolve_tenant_id(request, tenant_id)
    ScopePolicy.enforce_tenant_scope(request, target_tenant)
    result = service.get_tenant_dashboard(
        target_tenant,
        window_days=window_days,
        top_n=top_n,
    )
    return _to_envelope(result)


@router.get("/domain/{domain_id}")
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def get_domain_dashboard(
    request: Request,
    domain_id: str,
    service: DashboardServiceDep,
    tenant_id: str | None = Query(
        default=None,
        description=(
            "Target tenant. Required for platform-admin callers; "
            "ignored for scope-bound callers."
        ),
    ),
    window_days: int = Query(default=7, ge=1, le=90),
    top_n: int = Query(default=5, ge=1, le=50),
) -> dict[str, Any]:
    """Return the domain-scope dashboard rollup."""
    target_tenant = _resolve_tenant_id(request, tenant_id)
    ScopePolicy.enforce_domain_scope(request, target_tenant, domain_id)
    result = service.get_domain_dashboard(
        target_tenant,
        domain_id,
        window_days=window_days,
        top_n=top_n,
    )
    return _to_envelope(result)


@router.get("/agent/{agent_id}")
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def get_agent_dashboard(
    request: Request,
    agent_id: str,
    service: DashboardServiceDep,
    tenant_id: str | None = Query(
        default=None,
        description=(
            "Target tenant. Required for platform-admin callers; "
            "ignored for scope-bound callers."
        ),
    ),
    domain_id: str | None = Query(
        default=None,
        description=(
            "Target domain. Required for platform-admin or tenant-admin "
            "callers; ignored for domain-scoped callers."
        ),
    ),
    window_days: int = Query(default=7, ge=1, le=90),
    top_n: int = Query(default=5, ge=1, le=50),
) -> dict[str, Any]:
    """Return the agent-scope dashboard rollup."""
    target_tenant = _resolve_tenant_id(request, tenant_id)
    target_domain = _resolve_domain_id(request, domain_id)
    ScopePolicy.enforce_agent_scope(
        request, target_tenant, target_domain, agent_id
    )
    result = service.get_agent_dashboard(
        target_tenant,
        target_domain,
        agent_id,
        window_days=window_days,
        top_n=top_n,
    )
    return _to_envelope(result)
