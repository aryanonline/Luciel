"""Unit 13e §3.4.10 — session-summary retention hard-delete.

Why this task exists
--------------------
§3.4.10 gives the persisted session summary its OWN retention clock,
distinct from the transcript clock:

    * Free tier: 90 days.
    * Pro tier:  1 year (365 days).

(The transcript clock — 30 days Free / 1 year Pro, with an S3 cold
archive at 90 days — lives in the broader retention subsystem; the S3
cold-archive MOVE itself is flagged deploy-phase. This worker is the
summary leg only.)

Each hard-delete emits an ``ACTION_DATA_RETENTION_HARD_DELETE`` audit row
(payload: data_class, resolved_lead_id, retention_policy_applied,
deleted_at) so the destruction is a defensible legal record (PIPEDA
Principle 5, same posture as ``app.worker.tasks.retention``).

Scoping
-------
Uses OpsSessionLocal (luciel_ops, BYPASSRLS) like the tenant retention
worker so a single scan crosses every tenant's summaries. The audit row
is written with the summary row's OWN admin_id, so attribution stays
per-tenant correct even though the scan crosses tenants under BYPASSRLS.

Per-tier TTL is resolved by joining each summary to its admin's tier at
scan time; a tenant that upgrades Free→Pro between writes therefore gets
the Pro window applied at the next sweep (the clock is evaluated against
the CURRENT tier, not the tier at write time — simplest defensible rule).
"""
from __future__ import annotations

import logging
import traceback
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from celery import shared_task
from sqlalchemy import select

from app.models.admin import TIER_PRO

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)


# §3.4.10 per-tier summary retention windows. Platform constants, NOT
# admin-configurable. Free = 90 days, Pro = 1 year.
SUMMARY_RETENTION_DAYS_FREE = 90
SUMMARY_RETENTION_DAYS_PRO = 365

# data_class value stamped on the audit payload so an auditor can filter
# every retention hard-delete by the kind of data destroyed.
DATA_CLASS_SESSION_SUMMARY = "session_summary"


def _retention_days_for_tier(tier: str) -> int:
    """Per-tier summary TTL. Pro = 365d, everything else = Free 90d."""
    return (
        SUMMARY_RETENTION_DAYS_PRO
        if tier == TIER_PRO
        else SUMMARY_RETENTION_DAYS_FREE
    )


def find_and_hard_delete_expired_summaries(
    db: "Session",
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Core, DB-session-injected summary retention sweep.

    Deterministic + unit-testable. Scans session_summaries, resolves each
    row's per-tier TTL via its admin's CURRENT tier, hard-deletes every
    summary older than its TTL, and emits one
    ``ACTION_DATA_RETENTION_HARD_DELETE`` audit row per deletion. Returns
    a list of deletion-summary dicts. Caller owns the transaction commit.
    """
    from app.models.admin import Admin
    from app.models.admin_audit_log import (
        ACTION_DATA_RETENTION_HARD_DELETE,
        RESOURCE_SESSION_SUMMARY,
    )
    from app.models.session_summary import SessionSummary
    from app.repositories.admin_audit_repository import (
        AdminAuditRepository,
        AuditContext,
    )

    now = now or datetime.now(timezone.utc)
    audit_repo = AdminAuditRepository(db)

    # Join each summary to its admin's tier so the per-tier TTL is
    # resolved in a single scan. BYPASSRLS (OpsSessionLocal) means this
    # crosses tenants; attribution stays correct via the row's admin_id.
    stmt = select(SessionSummary, Admin.tier).join(
        Admin, Admin.id == SessionSummary.admin_id
    )
    rows = list(db.execute(stmt).all())

    deleted: list[dict] = []
    for summary, tier in rows:
        created = summary.created_at
        if created is None:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)

        ttl_days = _retention_days_for_tier(tier)
        cutoff = now - timedelta(days=ttl_days)
        if created >= cutoff:
            continue

        deleted_at = now.isoformat()
        admin_id = summary.admin_id
        resolved_lead_id = summary.resolved_lead_id
        session_id = summary.session_id
        luciel_instance_id = summary.luciel_instance_id

        # Emit the destruction audit BEFORE the delete so the row is in
        # the same transaction; if the commit fails, neither lands.
        audit_repo.record(
            ctx=AuditContext.system(label="session_summary_retention"),
            admin_id=admin_id,
            action=ACTION_DATA_RETENTION_HARD_DELETE,
            resource_type=RESOURCE_SESSION_SUMMARY,
            resource_natural_id=session_id,
            luciel_instance_id=luciel_instance_id,
            after={
                "data_class": DATA_CLASS_SESSION_SUMMARY,
                "resolved_lead_id": resolved_lead_id,
                "retention_policy_applied": f"{tier}:{ttl_days}d",
                "deleted_at": deleted_at,
            },
            note=f"retention:{DATA_CLASS_SESSION_SUMMARY}:{ttl_days}d",
        )
        db.delete(summary)
        deleted.append(
            {
                "session_id": session_id,
                "admin_id": admin_id,
                "resolved_lead_id": resolved_lead_id,
                "retention_policy_applied": f"{tier}:{ttl_days}d",
            }
        )

    return deleted


@shared_task(
    bind=True,
    name=(
        "app.worker.tasks.session_summary_retention."
        "run_session_summary_retention"
    ),
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=10,
    retry_jitter=True,
    max_retries=3,
)
def run_session_summary_retention(self):
    """Nightly: hard-delete session summaries past their per-tier TTL.

    Returns a dict summary for observability:
        {"deleted_count": int, "errored": bool}
    """
    from app.db.session import OpsSessionLocal

    if OpsSessionLocal is None:
        _log.error(
            "session_summary_retention ABORTED: OpsSessionLocal is None. "
            "settings.luciel_ops_db_url must be configured."
        )
        return {"deleted_count": 0, "aborted": "ops_session_unavailable"}

    db = OpsSessionLocal()
    try:
        deleted = find_and_hard_delete_expired_summaries(db)
        db.commit()
        _log.info(
            "session_summary_retention complete: deleted %d summary(ies)",
            len(deleted),
        )
        return {"deleted_count": len(deleted), "errored": False}
    except Exception:
        db.rollback()
        _log.error(
            "session_summary_retention FAILED traceback:\n%s",
            traceback.format_exc(),
        )
        raise
    finally:
        db.close()
