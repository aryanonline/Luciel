"""Step 31 sub-branch 3 (Arc 12 EX1c collapsed): tenant dashboard HTTP surface.

A single GET endpoint that wraps the read-only `DashboardService`
behind `ScopePolicy.enforce_tenant_scope`. Cross-tenant access is
denied by `ScopePolicy`; embed keys are denied at the perimeter by
`ApiKeyAuthMiddleware` because this router mounts under
`ADMIN_AUTH_PATHS` (`/api/v1/dashboard`) and embed keys carry
`EMBED_REQUIRED_PERMISSIONS = {"chat"}` only — they cannot also carry
`"admin"`, so the middleware rejects them with 403 before any route
handler runs. The same gate that blocks embed keys from `/api/v1/admin/*`
blocks them here.

The endpoint:

  - GET /api/v1/dashboard/tenant
        Returns a TenantDashboard rollup for the caller's tenant.
        Platform-admin callers MAY pass `?admin_id=...` to read any
        tenant; non-platform-admin callers MUST omit the query param
        (their key's tenant scope is used). Passing a admin_id that
        does not match the caller's scope is rejected by
        `ScopePolicy.enforce_tenant_scope`. The instance-scoped rollup
        is surfaced through the ``top_luciel_instances`` field on the
        envelope — V2 has no Domain or Agent layer (Architecture
        §3.7.2), so the per-domain / per-agent HTTP endpoints that
        used to live here were removed in Arc 12 EX1c.

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

The same Arc 7 C4 tier-aware limiter (pre-Arc-7: `ADMIN_RATE_LIMIT`="30/minute" - `app/middleware/rate_limit.py`;
now per-(tier, admin, instance) bucket with free=30 / pro=300 / enterprise=3000 rpm)
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
    limiter,
    get_tier_aware_key,
    get_tier_rate_limit_for_key,
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
    """Resolve the target admin_id for a dashboard read.

    Non-platform-admin callers: their key's tenant scope is authoritative;
    a `?admin_id=` that contradicts the key is rejected by
    `enforce_tenant_scope` below. Omitting the query param is fine —
    we just use the caller's tenant.

    Platform-admin callers: MUST supply `?admin_id=` because their
    key carries no tenant scope.

    Returns the resolved string; raises HTTP 400 if neither source
    yields one.
    """
    caller_tenant = getattr(request.state, "admin_id", None)
    target = query_tenant_id or caller_tenant
    if not target:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "admin_id is required: platform-admin callers must "
                "pass it as a query parameter; scope-bound callers "
                "use their key's tenant by default"
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
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def get_tenant_dashboard(
    request: Request,
    service: DashboardServiceDep,
    admin_id: str | None = Query(
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
    target_tenant = _resolve_tenant_id(request, admin_id)
    ScopePolicy.enforce_tenant_scope(request, target_tenant)
    result = service.get_tenant_dashboard(
        target_tenant,
        window_days=window_days,
        top_n=top_n,
    )
    return _to_envelope(result)


# Arc 12 EX1c — the legacy ``/domain/{domain_id}`` and
# ``/agent/{agent_id}`` dashboard endpoints have been removed at the
# HTTP surface. V2 has a single Admin→Instance boundary (Architecture
# §3.7.2 / §3.7.3); callers consume the instance-scoped rollup via the
# ``top_luciel_instances`` field on the tenant envelope. The
# DashboardService.get_domain_dashboard / .get_agent_dashboard methods
# persist in the service layer for forensic compatibility (owned by a
# later EX-step); they are no longer surfaced over HTTP.
#
# Public-contract removal (frontend will be told):
#   * GET /api/v1/dashboard/domain/{domain_id}
#   * GET /api/v1/dashboard/agent/{agent_id}
