"""
Tenant retention hard-purge task.

Historical references:
  * Step 30a.2 (Alembic dfea1a04e037) created the 90-day uniform
    retention window. Closed by Arc 10 (this file).
  * Arc 9 C6.1 (Alembic arc9_c6_1_luciel_ops_role) created the
    luciel_ops Postgres role with BYPASSRLS; Arc 9 C6.3 wired
    OpsSessionLocal in app/db/session.py. Arc 10 now wires the
    worker to use it (drift entry
    D-arc10-retention-worker-still-on-default-session-2026-05-27).
  * Arc 10 (Alembic arc10_lifecycle_subsystem):
      - Collapsed RETENTION_WINDOW_DAYS from 90 to 30 to match
        Vision §6.5 ("after 30 days: GDPR-style hard delete of all
        customer data"). Supersedes the prior 90-day lock dated
        2026-05-14 09:55 EDT.
      - Switched scan predicate from tenant_configs.deactivated_at
        to admins.closure_initiated_at. Closure is the only
        trigger for hard-delete; deactivation by other sources
        (platform-admin ToS action, webhook) does NOT advance a
        tenant toward hard-delete. See drift entry
        D-arc10-no-closure-clock-distinct-from-deactivation-2026-05-27.
      - Switched SessionLocal -> OpsSessionLocal so the worker uses
        the BYPASSRLS luciel_ops role. Removed the
        rls_tenant_context_enabled guard because BYPASSRLS makes
        the underlying Wall-3-with-empty-instance-id gap
        unreachable for that role.

Why this task exists
--------------------

Vision §6.5 and PIPEDA Principle 5 both require that personal data be
destroyed when no longer needed for the purpose it was collected for.
The Vision specifies a 30-day grace clock; PIPEDA requires a defined
retention period (30 days satisfies this). The closure cascade soft-
deactivates a tenant and all its children via
``admin_service.deactivate_tenant_with_cascade`` (12-layer leaf-first
soft-delete), but soft-deletion alone is not destruction. The
retention worker is the scheduled job that converts soft-deletion into
hard-deletion after the 30-day grace window has elapsed.

Schedule:
    Celery beat fires this task once nightly at 08:00 UTC (04:00 EDT
    in summer, 03:00 EST in winter -- off-peak in Markham both
    seasons). Wired in ``app.worker.celery_app::celery_app.conf.beat_schedule``.

Retention window:
    30 days, uniform across all tiers. Set at module-level constant
    ``RETENTION_WINDOW_DAYS``. Matches Vision §6.5 verbatim. Future
    per-tenant or per-tier overrides can land via a column on admins
    without touching the scan predicate shape.

Scan predicate (single SELECT per nightly run):
    SELECT id FROM admins
    WHERE active = false
      AND closure_initiated_at IS NOT NULL
      AND closure_initiated_at < (now() - INTERVAL '30 days')
      AND hard_deleted_at IS NULL
    ORDER BY closure_initiated_at ASC

The partial index ``ix_admins_closure_clock_eligible`` (Alembic
arc10_lifecycle_subsystem) backs this query so the scan stays O(log n).
The hard_deleted_at IS NULL clause makes the scan tombstone-aware: an
admin that has already been tombstoned in a prior run is not re-
selected, even if the row is still in the table.

Per-tenant action:
    For each row returned by the scan, the task calls
    ``AdminService.hard_delete_tenant_after_retention(admin_id)``,
    which runs the cascade DELETE chain (step 11 is now a tombstone
    UPDATE per Arc 10, not a DELETE) inside a single transaction.
    Any error rolls back that tenant's purge and the worker logs +
    moves on to the next tenant -- one bad row should not block a
    nightly run.

Idempotency:
    The hard-purge method re-verifies the 30-day predicate inside
    its own transaction before deleting. If two beat instances race
    (which cannot happen with embedded beat on a single replica, but
    is a defensive guard for future multi-replica scaling), the
    second one finds the admins row already tombstoned (hard_deleted_at
    IS NOT NULL) and exits cleanly.

What this task does NOT do:
    - It does not delete AdminAuditLog rows. Those are the legal
      record that the purge happened; they reference admin_id as
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

# Arc 10: switched from SessionLocal to OpsSessionLocal.
# OpsSessionLocal binds the connection to the luciel_ops role created
# in Arc 9 C6.1, which has BYPASSRLS. This makes the underlying
# Wall-3-with-empty-instance-id gap unreachable for this worker, so the
# rls_tenant_context_enabled guard (previously required as a safety
# net) is removed below.
from app.db.session import OpsSessionLocal, get_ops_db_session  # noqa: F401
from app.db.tenant_scope import bind_tenant_scope  # Arc 9 C4.4 (unused under BYPASSRLS; kept for import compatibility)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


_log = logging.getLogger(__name__)


# 30-day uniform retention window, locked by Aryan 2026-05-27 (Arc 10)
# in alignment with Vision §6.5 ("after 30 days: GDPR-style hard delete
# of all customer data"). Supersedes the prior 90-day lock dated
# 2026-05-14 09:55 EDT, which carried a PIPEDA-Principle-5-only
# justification. 30 days is also a defined retention period under PIPEDA
# Principle 5 -- strictly tighter than the prior lock -- and matches
# the founder-approved Vision verbatim.
#
# Constant-as-policy: future per-tenant overrides land via a column on
# admins (e.g. retention_days_override) without changing this default.
RETENTION_WINDOW_DAYS = 30


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

    # Arc 10: use OpsSessionLocal (luciel_ops role, BYPASSRLS) for the
    # scan. Per-tenant purges also use OpsSessionLocal sessions so the
    # cascade DELETEs / tombstone UPDATE all run under the same role.
    # A single bad tenant rolls back cleanly without poisoning the
    # scan-session transaction.
    if OpsSessionLocal is None:
        # Defense in depth: get_ops_db_session() raises if luciel_ops
        # is not wired in this environment. We mirror that posture
        # here so the worker emits a clear log line rather than
        # falling back to SessionLocal (which would re-introduce the
        # Wall-3 gap C6.1 was created to close).
        _log.error(
            "retention_purge ABORTED: OpsSessionLocal is None. "
            "settings.luciel_ops_db_url must be configured for the "
            "retention worker to run. See Arc 9 C6.3 + Arc 10 paired "
            "code change."
        )
        return {
            "scanned_count": 0,
            "purged_count": 0,
            "errored_count": 0,
            "errored_tenant_ids": [],
            "aborted": "ops_session_unavailable",
        }

    scan_db: Session = OpsSessionLocal()
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

    for admin_id in eligible_tenant_ids:
        # Each tenant gets its own session + transaction so one
        # failing purge does not block the rest of the nightly batch.
        # AdminService.hard_delete_tenant_after_retention runs the
        # cascade DELETE chain (step 11 is a tombstone UPDATE per
        # Arc 10, not a DELETE) atomically; any error inside it
        # rolls back ONLY that tenant.
        #
        # Arc 10: BYPASSRLS via OpsSessionLocal removes the prior
        # rls_tenant_context_enabled guard. The Wall-3 gap C6.1 was
        # created to close is unreachable for the luciel_ops role
        # because RLS policies don't fire for it at all.
        #
        # bind_tenant_scope is no longer called here. The per-tenant
        # context binding existed to populate app.admin_id /
        # app.instance_id for RLS. Under BYPASSRLS those GUCs have
        # no effect on the worker's view. AdminService's audit-row
        # writer reads admin_id from its method argument, not from
        # any context var, so removing bind_tenant_scope here does
        # not affect audit emission. (Belt + suspenders: see test
        # tests/services/test_arc10_retention_audit_emission.py.)
        per_tenant_db: Session = OpsSessionLocal()
        try:
            # Import here (not at module top) to avoid a circular
            # import: admin_service imports from app.models which
            # imports from app.worker via the Celery audit-chain
            # signal wiring.
            from app.services.admin_service import AdminService

            admin_svc = AdminService(per_tenant_db)
            row_counts = admin_svc.hard_delete_tenant_after_retention(
                admin_id=admin_id,
                retention_window_days=RETENTION_WINDOW_DAYS,
            )
            per_tenant_db.commit()
            purged_count += 1
            _log.info(
                "retention_purge OK admin_id=%s row_counts=%s",
                admin_id,
                row_counts,
            )
        except Exception:
            per_tenant_db.rollback()
            errored_count += 1
            errored_tenant_ids.append(admin_id)
            _log.error(
                "retention_purge FAILED admin_id=%s traceback:\n%s",
                admin_id,
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
    """Return admin ids whose closure-grace clock has expired.

    Arc 10: predicate changed from tenant_configs.deactivated_at to
    admins.closure_initiated_at. Closure is the only trigger for
    hard-delete; deactivation by other sources (platform-admin ToS
    action, webhook for sub-cancellation without admin closure) does
    NOT advance a tenant toward hard-delete. See drift entry
    D-arc10-no-closure-clock-distinct-from-deactivation-2026-05-27.

    Single SELECT against the partial index
    ``ix_admins_closure_clock_eligible`` (arc10_lifecycle_subsystem).
    Ordered by ``closure_initiated_at ASC`` so the oldest purges run
    first -- if the nightly job is interrupted partway, the next run
    picks up where this one left off in FIFO order.

    The hard_deleted_at IS NULL clause makes the scan tombstone-aware:
    an admin row already tombstoned (and thus already purged of its
    customer-data children) is not re-selected on subsequent runs.
    """
    # Plain SQL keeps the scan close to the index shape; using the ORM
    # here would load Admin objects (heavier) when all we need is the
    # admin id string.
    sql = text(
        """
        SELECT id
          FROM admins
         WHERE active = false
           AND closure_initiated_at IS NOT NULL
           AND closure_initiated_at < :cutoff
           AND hard_deleted_at IS NULL
         ORDER BY closure_initiated_at ASC
        """
    )
    result = db.execute(sql, {"cutoff": cutoff})
    return [row[0] for row in result]
