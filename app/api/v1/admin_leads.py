"""Unit 13d (§3.9) — admin lead-outcome admin API.

  PATCH /api/v1/admin/leads/{lead_id}/outcome
      Sets/changes a captured lead's ``outcome`` (converted / lost /
      in_progress) — the business data the §3.9 conversion-rate metric
      reads. This is the ONLY write path the Analytics unit adds, and it
      writes lead business-data the admin owns; analytics itself stays
      read-only. Emits an ACTION_LEAD_OUTCOME_SET audit row in the same
      transaction. Idempotent: setting the same outcome twice is a 200
      no-op (still records the audit row — the operator touched it).
      Returns 404 if the lead is not the caller's tenant (existence is
      not leaked). 422 on an out-of-vocabulary outcome (Pydantic enum).

Layered defences (mirror admin_handoff.py / admin_escalation_ack.py):
  L1   _require_admin_id — authenticated admin context (401 otherwise).
  L2   Role gate: PERM_CONFIGURE_CHANNELS (or platform_admin), the same
       permission the other instance-scoped admin write routes use.
  L3   TenantScopedDbSession — RLS GUC bound; the explicit admin_id WHERE
       fence on the lookup is belt-and-suspenders on top of RLS.
  L4   admin_audit_log row in the same txn as the mutation.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import TenantScopedDbSession, get_audit_context
from app.models.admin_audit_log import ACTION_LEAD_OUTCOME_SET, RESOURCE_LEAD
from app.models.lead import (
    OUTCOME_CONVERTED,
    OUTCOME_IN_PROGRESS,
    OUTCOME_LOST,
    Lead,
)
from app.policy.permissions import PERM_CONFIGURE_CHANNELS, PermissionResolver
from app.policy.scope import ScopePolicy
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/leads",
    tags=["admin-leads"],
)


class LeadOutcome(str, Enum):
    """The §3.9 sales-outcome vocabulary (mirrors ALLOWED_LEAD_OUTCOMES).

    A str-Enum on the request body gives FastAPI a free 422 on any
    out-of-vocabulary value before the handler runs.
    """

    converted = OUTCOME_CONVERTED
    lost = OUTCOME_LOST
    in_progress = OUTCOME_IN_PROGRESS


class LeadOutcomeRequest(BaseModel):
    outcome: LeadOutcome


class LeadOutcomeResponse(BaseModel):
    lead_id: int
    outcome: str
    message: str


def _require_admin_id(request: Request) -> str:
    admin_id = getattr(request.state, "admin_id", None)
    if not admin_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No authenticated admin context.",
        )
    return admin_id


def _require_outcome_permission(request: Request) -> None:
    """platform_admin passes; otherwise the caller must hold
    PERM_CONFIGURE_CHANNELS (the owner permission under single-login).

    The lead is identified by lead id (not instance id); the tenant
    fence on admin_id in the lookup is the cross-tenant guard.
    """
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


@router.patch(
    "/{lead_id}/outcome",
    response_model=LeadOutcomeResponse,
    status_code=status.HTTP_200_OK,
)
def set_lead_outcome(
    request: Request,
    lead_id: int,
    body: LeadOutcomeRequest,
    db: TenantScopedDbSession,
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> LeadOutcomeResponse:
    """Record the sales outcome for a captured lead + audit row.

    Tenant-fenced (404 cross-tenant), enum-validated (422 bad value).
    """
    admin_id = _require_admin_id(request)
    _require_outcome_permission(request)

    # Tenant fence: explicit admin_id WHERE on top of RLS. 404 (not 403)
    # so a foreign lead id is not distinguishable from a missing one.
    lead = db.execute(
        select(Lead).where(Lead.id == lead_id, Lead.admin_id == admin_id)
    ).scalar_one_or_none()
    if lead is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Lead {lead_id} not found.",
        )

    new_outcome = body.outcome.value
    before_outcome = lead.outcome
    lead.outcome = new_outcome

    AdminAuditRepository(db).record(
        ctx=audit_ctx,
        admin_id=admin_id,
        action=ACTION_LEAD_OUTCOME_SET,
        resource_type=RESOURCE_LEAD,
        resource_pk=lead.id,
        resource_natural_id=lead.session_id,
        luciel_instance_id=lead.luciel_instance_id,
        before={"outcome": before_outcome},
        after={"outcome": new_outcome},
        note=f"lead outcome set to {new_outcome}",
        autocommit=False,
    )
    db.commit()
    db.refresh(lead)

    logger.info(
        "lead_outcome_set",
        extra={
            "event": "lead_outcome_set",
            "lead_id": lead_id,
            "outcome": new_outcome,
        },
    )
    return LeadOutcomeResponse(
        lead_id=lead_id,
        outcome=new_outcome,
        message="Lead outcome recorded.",
    )
