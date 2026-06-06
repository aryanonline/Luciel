"""Arc 18 — per-instance conversation-budget Usage API (§3.4.1b).

Read-only endpoints mounted at ``/admin/usage/...`` backing the usage
panel. They expose, per Instance owned by the calling Admin, the live
conversation count against the resolved budget cap, the raw overage, the
utilization percentage, and the alert state (none / 80 / 100):

  * GET "/admin/usage"               -- one row per owned Instance.
  * GET "/admin/usage/{instance_pk}" -- a single Instance's usage.

Live count source (honesty invariant)
--------------------------------------
``current`` is read straight from the SAME ``BudgetMeter`` the runtime
loop increments — never a fabricated or cached number. ``cap`` is the
entitlement (``conversation_budget(tier, cadence)``); ``overage`` is the
raw ``max(0, current - cap)`` (NOT rounded to the billing hundreds — this
surface reports actuals, the cycle-close billing path rounds). When Redis
is unreachable the meter returns 0 (fail-open, same as the gate), so the
panel shows 0 rather than crashing.

Layered defences (mirror admin_connections.py)
-----------------------------------------------
  L1 ``_require_admin_id`` — authenticated admin context (401 otherwise).
  L2 ``ScopePolicy.enforce_admin_owns_instance`` — cross-Admin guard on
     the single-instance route (404/403 if not the caller's).
  L3 The list route enumerates ONLY ``instance_service.list_for_admin``
     for the caller's admin_id, and the meter read is itself keyed by
     that admin_id — an explicit WHERE-admin fence (belt-and-suspenders).
  L4 No mutation, no audit row — this is a pure read surface.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.deps import TenantScopedDbSession, get_luciel_instance_service
from app.policy.entitlements import (
    ALERT_THRESHOLD_80,
    ALERT_THRESHOLD_100,
    conversation_budget,
)
from app.policy.scope import ScopePolicy
from app.runtime.billing_period import resolve_billing_context
from app.services.instance_service import InstanceService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/usage", tags=["admin-usage"])


# Alert-state thresholds (percent of cap). ``alert_state`` reports the
# HIGHEST threshold the instance has reached this period: 100 at/over cap,
# 80 at/over the 80% line, else none. This mirrors the budget-alert
# threshold ladder (entitlements.budget_alert_channels) so the panel and
# the alert pipeline agree.
ALERT_STATE_NONE = "none"


class InstanceUsageView(BaseModel):
    """One Instance's conversation-budget usage for the open period."""

    instance_id: int = Field(..., description="Instance primary key.")
    instance_name: str = Field(..., description="Instance display name.")
    tier: str
    cadence: str
    current: int = Field(..., description="Conversations used this period.")
    cap: int = Field(..., description="Per-instance budget for (tier, cadence).")
    overage: int = Field(
        ..., description="Raw conversations over cap (max(0, current - cap))."
    )
    billing_period_start: str = Field(
        ..., description="ISO date anchor of the open billing period."
    )
    utilization_pct: int = Field(
        ..., description="round(100 * current / cap); 0 when cap is 0."
    )
    alert_state: str = Field(
        ..., description="Highest threshold reached: 'none' | '80' | '100'."
    )


class UsageListResponse(BaseModel):
    admin_id: str
    instances: list[InstanceUsageView]


def _require_admin_id(request: Request) -> str:
    admin_id = getattr(request.state, "admin_id", None)
    if not admin_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No authenticated admin context.",
        )
    return admin_id


def _meter():
    """Resolve the process BudgetMeter (Redis-backed in production).

    Local import keeps the module importable without redis installed and
    lets tests monkeypatch a meter with an InMemoryBackend.
    """
    from app.billing.metering import BudgetMeter

    return BudgetMeter()


def _utilization_pct(current: int, cap: int) -> int:
    if cap <= 0:
        return 0
    return round(100 * current / cap)


def _alert_state(current: int, cap: int) -> str:
    if cap <= 0:
        return ALERT_STATE_NONE
    pct = 100 * current / cap
    if pct >= ALERT_THRESHOLD_100:
        return str(ALERT_THRESHOLD_100)
    if pct >= ALERT_THRESHOLD_80:
        return str(ALERT_THRESHOLD_80)
    return ALERT_STATE_NONE


def _build_view(*, db, admin_id: str, instance, meter) -> InstanceUsageView:
    """Assemble the usage row for one Instance.

    Resolves the (tier, cadence, period_start) the SAME way the runtime
    gate does (``resolve_billing_context``) so the panel's cap/period and
    the gate's never diverge, then reads the live count from the meter.
    """
    ctx = resolve_billing_context(db, admin_id=admin_id)
    cap = conversation_budget(ctx.tier, ctx.cadence)
    current = meter.current_count(
        admin_id=admin_id,
        instance_id=instance.id,
        period_start=ctx.period_start,
    )
    overage = max(0, current - cap)
    return InstanceUsageView(
        instance_id=instance.id,
        instance_name=instance.display_name,
        tier=ctx.tier,
        cadence=ctx.cadence,
        current=current,
        cap=cap,
        overage=overage,
        billing_period_start=ctx.period_start,
        utilization_pct=_utilization_pct(current, cap),
        alert_state=_alert_state(current, cap),
    )


@router.get("", response_model=UsageListResponse)
@router.get("/", response_model=UsageListResponse)
def list_usage(
    request: Request,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
) -> UsageListResponse:
    """List conversation-budget usage for every Instance the caller owns."""
    admin_id = _require_admin_id(request)
    meter = _meter()
    instances = instance_service.list_for_admin(
        admin_id=admin_id, active_only=False
    )
    rows = [
        _build_view(db=db, admin_id=admin_id, instance=inst, meter=meter)
        for inst in instances
    ]
    return UsageListResponse(admin_id=admin_id, instances=rows)


@router.get("/{instance_pk}", response_model=InstanceUsageView)
def get_instance_usage(
    request: Request,
    instance_pk: int,
    db: TenantScopedDbSession,
    instance_service: Annotated[
        InstanceService, Depends(get_luciel_instance_service)
    ],
) -> InstanceUsageView:
    """Conversation-budget usage for a single owned Instance."""
    admin_id = _require_admin_id(request)
    instance = instance_service.get_by_pk(instance_pk)
    if instance is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Instance {instance_pk} not found",
        )
    # Cross-Admin guard — 404/403 unless the caller owns this instance.
    ScopePolicy.enforce_admin_owns_instance(request, instance)
    return _build_view(
        db=db, admin_id=admin_id, instance=instance, meter=_meter()
    )
