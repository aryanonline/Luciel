"""§3.9 Analytics & Reporting admin API (Unit 13d).

  GET /api/v1/admin/analytics?period=...
      Returns the tier-shaped §3.9 metric bundle for the selected period.
      Free → BASIC subset (total conversations, total leads, budget
      utilization). Pro → the FULL metric surface.

  GET /api/v1/admin/analytics/export?view=...&period=...
      Pro-only CSV export of one analytics view for the period
      (text/csv + Content-Disposition filename). Free → 403.

Both endpoints are READ-ONLY. They run through the TenantScoped (RLS-
bound) request session and the AnalyticsService only SELECTs aggregates
scoped ``WHERE admin_id = :admin_id`` — a tenant's analytics can never
include another tenant's data. The tier is resolved the SAME way the
budget gate / usage panel resolve it (``resolve_billing_context``), and
the budget-utilization metric reuses the existing BudgetMeter rather than
duplicating it (this surface EXTENDS, not replaces, admin/usage.py).

Layered defences (mirror admin/usage.py + admin_escalation_ack.py):
  L1   _require_admin_id — authenticated admin context (401 otherwise).
  L2   Role gate: PERM_CONFIGURE_CHANNELS (or platform_admin), the same
       gate the other instance-scoped admin routes use.
  L3   TenantScopedDbSession — RLS GUC bound; AnalyticsService also filters
       admin_id explicitly (belt-and-suspenders).
  L4   No mutation, no audit row — a pure read surface.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.analytics.service import AnalyticsPeriod, AnalyticsService
from app.api.deps import TenantScopedDbSession
from app.policy.entitlements import TIER_FREE
from app.policy.permissions import PERM_CONFIGURE_CHANNELS, PermissionResolver
from app.policy.scope import ScopePolicy
from app.runtime.billing_period import resolve_billing_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/analytics", tags=["admin-analytics"])

# How many days the default rolling window covers when no explicit period
# is requested. The open billing period is the other supported anchor.
_DEFAULT_WINDOW_DAYS = 30

# The CSV views a Pro admin can export. Each maps to a flattening of one
# AnalyticsService metric for the period.
_CSV_VIEWS = (
    "conversations",
    "leads",
    "escalations_by_signal",
    "channel_mix",
    "conversion",
    "top_knowledge_sources",
    "busiest_times",
)


def _require_admin_id(request: Request) -> str:
    admin_id = getattr(request.state, "admin_id", None)
    if not admin_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No authenticated admin context.",
        )
    return admin_id


def _require_analytics_permission(request: Request) -> None:
    if ScopePolicy.is_platform_admin(request):
        return
    resolved = PermissionResolver.resolve(request)
    if PERM_CONFIGURE_CHANNELS not in resolved:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Caller does not hold required permission "
                f"{PERM_CONFIGURE_CHANNELS!r}."
            ),
        )


def _resolve_period(db, *, admin_id: str, period: str) -> AnalyticsPeriod:
    """Resolve the report window from the ``period`` query param.

    ``billing`` (default) → the open billing period (its start anchors the
    window; end is now). ``last_Nd`` → a rolling N-day window. Anything
    else → 422.
    """
    now = datetime.now(timezone.utc)
    if period in ("billing", "current", ""):
        ctx = resolve_billing_context(db, admin_id=admin_id)
        start = datetime.fromisoformat(ctx.period_start).replace(
            tzinfo=timezone.utc
        )
        return AnalyticsPeriod(start=start, end=now, label=ctx.period_start)
    if period.startswith("last_") and period.endswith("d"):
        try:
            days = int(period[len("last_"):-1])
        except ValueError:
            days = 0
        if days > 0:
            return AnalyticsPeriod.last_n_days(days, now=now)
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=(
            "Invalid period. Use 'billing' or 'last_<N>d' "
            "(e.g. 'last_30d')."
        ),
    )


@router.get("")
@router.get("/")
def get_analytics(
    request: Request,
    db: TenantScopedDbSession,
    period: str = Query("billing", description="'billing' or 'last_<N>d'."),
) -> dict:
    """Tier-shaped §3.9 metrics for the selected period."""
    admin_id = _require_admin_id(request)
    _require_analytics_permission(request)

    ctx = resolve_billing_context(db, admin_id=admin_id)
    window = _resolve_period(db, admin_id=admin_id, period=period)
    svc = AnalyticsService(db)
    return svc.compute(admin_id=admin_id, tier=ctx.tier, period=window)


@router.get("/export")
def export_analytics_csv(
    request: Request,
    db: TenantScopedDbSession,
    view: str = Query("conversations", description="Which analytics view."),
    period: str = Query("billing", description="'billing' or 'last_<N>d'."),
) -> StreamingResponse:
    """Pro-only CSV export of one analytics view for the period.

    Free → 403 (the export entitlement is Pro-only). Returns text/csv with
    a Content-Disposition filename.
    """
    admin_id = _require_admin_id(request)
    _require_analytics_permission(request)

    ctx = resolve_billing_context(db, admin_id=admin_id)
    if ctx.tier == TIER_FREE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSV export is a Pro feature.",
        )
    if view not in _CSV_VIEWS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown view {view!r}. One of: {', '.join(_CSV_VIEWS)}.",
        )

    window = _resolve_period(db, admin_id=admin_id, period=period)
    svc = AnalyticsService(db)
    rows = _view_rows(svc, admin_id=admin_id, view=view, period=window)

    buf = io.StringIO()
    writer = csv.writer(buf)
    if rows:
        writer.writerow(list(rows[0].keys()))
        for row in rows:
            writer.writerow(list(row.values()))
    else:
        writer.writerow(["metric", "value"])
    buf.seek(0)

    filename = f"analytics_{view}_{window.label}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _view_rows(
    svc: AnalyticsService, *, admin_id: str, view: str, period: AnalyticsPeriod
) -> list[dict]:
    """Flatten one analytics view into CSV-friendly row dicts."""
    if view == "conversations":
        m = svc.conversations(admin_id=admin_id, period=period)
        return [{"metric": k, "value": v} for k, v in m.items()]
    if view == "leads":
        m = svc.leads(admin_id=admin_id, period=period)
        return [{"metric": k, "value": v} for k, v in m.items()]
    if view == "escalations_by_signal":
        m = svc.escalations_by_signal(admin_id=admin_id, period=period)
        return [{"signal": k, "count": v} for k, v in m.items()]
    if view == "channel_mix":
        m = svc.channel_mix(admin_id=admin_id, period=period)
        return [
            {
                "channel": ch,
                "count": m["counts"][ch],
                "fraction": round(m["fractions"][ch], 4),
            }
            for ch in m["counts"]
        ]
    if view == "conversion":
        m = svc.conversion(admin_id=admin_id, period=period)
        out = [{"outcome": k, "count": v} for k, v in m["by_outcome"].items()]
        out.append({"outcome": "rate", "count": m["rate"]})
        return out
    if view == "top_knowledge_sources":
        return svc.top_knowledge_sources(admin_id=admin_id, period=period)
    if view == "busiest_times":
        return svc.busiest_times(admin_id=admin_id, period=period)
    return []
