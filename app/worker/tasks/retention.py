"""
Tenant retention hard-purge task (Step 30a.2).

Closes D-no-retention-worker-pipeda-principle-5-2026-05-14.

Why this task exists
--------------------

PIPEDA Principle 5 (Limiting Use, Disclosure, and Retention) requires
that personal data be destroyed when no longer needed for the purpose
it was collected for. The Step 30a.2 cancellation path soft-deactivates
a tenant and all its children via
``admin_service.deactivate_tenant_with_cascade`` (9-layer leaf-first
soft-delete), but soft-deletion alone is not destruction. The
retention worker is the scheduled job that converts soft-deletion into
hard-deletion after a defined retention window.

Schedule:
    Celery beat fires this task once nightly at 08:00 UTC (04:00 EDT
    in summer, 03:00 EST in winter -- off-peak in Markham both
    seasons). Wired in ``app.worker.celery_app::celery_app.conf.beat_schedule``.

Retention window:
    90 days, uniform across all tiers. Set at module-level constant
    ``RETENTION_WINDOW_DAYS`` so future per-tenant or per-tier
    overrides can land without touching the scan predicate.

Scan predicate (single SELECT per nightly run):
    SELECT tenant_id FROM tenant_configs
    WHERE active = false
      AND deactivated_at IS NOT NULL
      AND deactivated_at < (now() - INTERVAL '90 days')
    ORDER BY deactivated_at ASC

The composite index ``ix_tenant_configs_active_deactivated_at``
(Alembic dfea1a04e037) backs this query so the scan stays O(log n).

Per-tenant action:
    For each row returned by the scan, the task calls
    ``AdminService.hard_delete_tenant_after_retention(tenant_id)``,
    which runs a 12-step DELETE chain inside a single transaction.
    Any error rolls back that tenant's purge and the worker logs +
    moves on to the next tenant -- one bad row should not block a
    nightly run.

Idempotency:
    The hard-purge method re-verifies the 90-day predicate inside
    its own transaction before deleting. If two beat instances race
    (which cannot happen with embedded beat on a single replica, but
    is a defensive guard for future multi-replica scaling), the
    second one finds the tenant_configs row already gone and exits
    cleanly.

What this task does NOT do:
    - It does not delete AdminAuditLog rows. Those are the legal
      record that the purge happened; they reference tenant_id as
      a string, not via FK, so they survive the cascade.
    - It does not delete subscriptions rows. Those carry billing
      history (Stripe invoice numbers, period-end dates) needed for
      tax / accounting retention which has its own clock. A separate
      future task handles subscription-row retention; tracked as
      future-debt in CANONICAL_RECAP \u00a714.
    - It does not delete messages directly. ``messages.session_id``
      has ``ON DELETE CASCADE``, so deleting the parent ``sessions``
      row hard-purges its messages automatically.

Failure handling:
    - Per-tenant errors: caught, logged with full traceback to
      CloudWatch, audit row recorded as best-effort, then the loop
      continues to the next tenant.
    - Task-level errors (DB unreachable, audit chain broken): the
      task itself raises, Celery handles per-task retry policy
      (3 attempts, exponential backoff -- same as memory_extraction).

Observability:
    - INFO line at task start with the scan-result count.
    - INFO line per tenant purged with row-count map by table.
    - INFO line at task end with totals.
    - WARNING / ERROR lines on per-tenant exceptions.
"""
from __future__ import annotations

import logging
import traceback
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from celery import shared_task
from sqlalchemy import text

from app.db.session import SessionLocal

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


_log = logging.getLogger(__name__)


# 90-day uniform retention window, locked by Aryan 2026-05-14 09:55 EDT.
# Constant-as-policy: future per-tenant overrides land via a column on
# tenant_configs (e.g. retention_days_override) without changing this
# default. PIPEDA Principle 5 requires a defined retention period; this
# is it.
RETENTION_WINDOW_DAYS = 90


@shared_task(
    bind=True,
    name="app.worker.tasks.retention.run_retention_purge",
    # Retry policy matches memory_extraction.py: 3 attempts, exponential
    # backoff (2s/4s/8s, jittered). A failed nightly run will be retried
    # by Celery; if all retries fail, the beat schedule runs again the
    # next night anyway so we self-heal within 24h either way.
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=10,
    retry_jitter=True,
    max_retries=3,
)
def run_retention_purge(self):
    """Nightly: hard-delete tenants soft-deactivated >90 days ago.

    Returns a dict summary for ease of observation / future scheduled-
    task return-value inspection:
        {
            "scanned_count": int,
            "purged_count": int,
            "errored_count": int,
            "errored_tenant_ids": list[str],
        }

    The return value is also logged at INFO level.
    """
    started_at = datetime.now(timezone.utc)
    cutoff = started_at - timedelta(days=RETENTION_WINDOW_DAYS)
    _log.info(
        "retention_purge starting: cutoff=%s (older than %d days "
        "from %s)",
        cutoff.isoformat(),
        RETENTION_WINDOW_DAYS,
        started_at.isoformat(),
    )

    # Use a fresh session for the scan; per-tenant purges use their
    # own sessions so a single bad tenant can roll back cleanly
    # without poisoning the scan-session transaction.
    scan_db: Session = SessionLocal()
    try:
        eligible_tenant_ids = _scan_eligible_tenants(scan_db, cutoff)
    finally:
        scan_db.close()

    _log.info(
        "retention_purge scan complete: %d tenant(s) eligible for purge",
        len(eligible_tenant_ids),
    )

    purged_count = 0
    errored_count = 0
    errored_tenant_ids: list[str] = []

    for tenant_id in eligible_tenant_ids:
        # Each tenant gets its own session + transaction so one
        # failing purge does not block the rest of the nightly batch.
        # AdminService.hard_delete_tenant_after_retention runs the
        # 12-step DELETE chain atomically; any error inside it rolls
        # back ONLY that tenant.
        per_tenant_db: Session = SessionLocal()
        try:
            # Import here (not at module top) to avoid a circular
            # import: admin_service imports from app.models which
            # imports from app.worker via the Celery audit-chain
            # signal wiring.
            from app.services.admin_service import AdminService

            admin_svc = AdminService(per_tenant_db)
            row_counts = admin_svc.hard_delete_tenant_after_retention(
                tenant_id=tenant_id,
                retention_window_days=RETENTION_WINDOW_DAYS,
            )
            per_tenant_db.commit()
            purged_count += 1
            _log.info(
                "retention_purge OK tenant_id=%s row_counts=%s",
                tenant_id,
                row_counts,
            )
        except Exception:
            per_tenant_db.rollback()
            errored_count += 1
            errored_tenant_ids.append(tenant_id)
            _log.error(
                "retention_purge FAILED tenant_id=%s traceback:\n%s",
                tenant_id,
                traceback.format_exc(),
            )
        finally:
            per_tenant_db.close()

    summary = {
        "scanned_count": len(eligible_tenant_ids),
        "purged_count": purged_count,
        "errored_count": errored_count,
        "errored_tenant_ids": errored_tenant_ids,
    }
    _log.info("retention_purge complete: %s", summary)
    return summary


def _scan_eligible_tenants(db: "Session", cutoff: datetime) -> list[str]:
    """Return tenant_ids whose tenant_configs row is past the retention cutoff.

    Single SELECT against the ``ix_tenant_configs_active_deactivated_at``
    composite index. Ordered by ``deactivated_at ASC`` so the oldest
    purges run first -- if the nightly job is interrupted partway, the
    next run picks up where this one left off in FIFO order.
    """
    # Plain SQL keeps the scan close to the index shape; using the ORM
    # here would load TenantConfig objects (heavier) when all we need
    # is the tenant_id string.
    sql = text(
        """
        SELECT tenant_id
          FROM tenant_configs
         WHERE active = false
           AND deactivated_at IS NOT NULL
           AND deactivated_at < :cutoff
         ORDER BY deactivated_at ASC
        """
    )
    result = db.execute(sql, {"cutoff": cutoff})
    return [row[0] for row in result]
