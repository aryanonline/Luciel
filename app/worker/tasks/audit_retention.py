"""Arc 10 -- audit-tier retention beat task.

Nightly task that archives admin_audit_logs rows to S3 cold storage
per Vision section 6.5 / section 7 tier-conditional retention:

    Free       :  30 days hot
    Pro        :   1 year hot
    Enterprise :   7 years hot

Rows older than their tier's window get archived to S3 with the
hash chain extended; the cold_archived_at column on the hot row is
stamped so re-scans skip the row. The hot row itself stays in place
(chain integrity); a future arc may add a hot-purge step.

Runs under the luciel_audit_archiver Postgres role (Arc 10 migration)
via a dedicated SessionLocal bound to that role. The role has
SELECT + UPDATE on admin_audit_logs only -- no DELETE, no access to
other tables. This is the surgical-minimum privilege surface for
the tier-archive work.

Failure handling: per-tier exceptions are caught inside the service
so one bad tier does not block the rest of the run. Per-batch
exceptions are caught at the batch boundary. Worst case: the entire
nightly run logs and the next nightly run picks up the remainder.

Run cadence: nightly at 09:00 UTC -- one hour after the tenant
retention worker so the two beat tasks do not contend.
"""
from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name="app.worker.tasks.audit_retention.run_audit_tier_retention",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=10,
    retry_jitter=True,
    max_retries=3,
)
def run_audit_tier_retention(self) -> dict:
    """Scan + archive eligible audit rows per tier.

    Returns a summary dict suitable for observation:
        {
            "started_at": iso,
            "completed_at": iso,
            "rows_scanned": int,
            "rows_archived": int,
            "rows_errored": int,
            "s3_objects_written": int,
        }
    """
    # Lazy import for the same circular-import-avoidance pattern as
    # the other worker tasks.
    from app.core.config import settings
    from app.repositories.admin_audit_repository import AdminAuditRepository
    from app.services.audit_retention_service import AuditRetentionService
    import boto3

    # We do NOT use SessionLocal here. The worker must run under the
    # luciel_audit_archiver role, which is the only Postgres role
    # granted UPDATE on admin_audit_logs. Wiring of the dedicated
    # SessionLocal is deferred to a follow-up commit because the
    # settings field for the archiver DB URL lands in the same
    # commit; for the initial migration apply the worker runs
    # disabled. The beat schedule registers the task; the Celery
    # config can enable/disable per environment.
    archiver_url = getattr(settings, "audit_archiver_db_url", None)
    if not archiver_url:
        logger.info(
            "audit_retention task: archiver_db_url not configured; "
            "task is a no-op until Arc 10 deploy wires the SSM secret."
        )
        return {
            "rows_scanned": 0,
            "rows_archived": 0,
            "rows_errored": 0,
            "s3_objects_written": 0,
            "aborted": "archiver_db_url_unset",
        }

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(archiver_url, pool_pre_ping=True)
    archiver_session_factory = sessionmaker(bind=engine, autoflush=False)
    db = archiver_session_factory()
    try:
        s3 = boto3.client("s3", region_name=settings.aws_region)
        audit_repo = AdminAuditRepository(db)
        svc = AuditRetentionService(
            db=db,
            s3_client=s3,
            s3_bucket=getattr(
                settings,
                "audit_cold_archive_bucket",
                "luciel-audit-cold-archive",
            ),
            audit_repository=audit_repo,
        )
        summary = svc.run_audit_retention()
        return {
            "started_at": summary.started_at.isoformat(),
            "completed_at": summary.completed_at.isoformat(),
            "rows_scanned": summary.rows_scanned,
            "rows_archived": summary.rows_archived,
            "rows_errored": summary.rows_errored,
            "s3_objects_written": summary.s3_objects_written,
        }
    finally:
        db.close()
        engine.dispose()


@shared_task(
    bind=True,
    name="app.worker.tasks.audit_retention.run_downgrade_grace_enforcement",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def run_downgrade_grace_enforcement(self) -> dict:
    """Day-30 enforcement worker for the Pro -> Free downgrade path.

    Scans subscriptions for rows past their 30-day grace window and
    runs DowngradeArchiveService.archive_overflow_for_admin for each.
    Stamps pending_downgrade_enforced_at to mark the work done so
    re-scans skip the row.

    Run cadence: nightly. Lives in this module rather than its own
    file because it shares the audit-tier retention worker's lifecycle
    posture (system-actor work that mutates customer state at a
    scheduled cadence).
    """
    from app.db.session import SessionLocal
    from app.repositories.admin_audit_repository import AdminAuditRepository
    from app.services.downgrade_archive_service import DowngradeArchiveService
    from app.services.downgrade_grace_service import DowngradeGraceService

    db = SessionLocal()
    try:
        archive_svc = DowngradeArchiveService(db)
        audit_repo = AdminAuditRepository(db)
        grace_svc = DowngradeGraceService(
            db,
            downgrade_archive_service=archive_svc,
            audit_repository=audit_repo,
        )
        results = grace_svc.enforce_at_grace_expiry()
        return {
            "enforced_admin_count": len(results),
            "enforced_admin_ids": [r.admin_id for r in results],
        }
    finally:
        db.close()
