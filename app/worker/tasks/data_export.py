"""Arc 10 -- data export bundle generation task.

Triggered by:
  * POST /admin/account/close with request_export=true (via
    ClosureService.initiate_closure -> DataExportService.enqueue ->
    this task)
  * POST /admin/account/export (standalone export request, also via
    DataExportService.enqueue)

Reads a pending data_export_jobs row, generates the bundle per
Architecture 3.6.3 (Arc 10 Option-2 -- knowledge as reconstructed
chunks, originals not retained), uploads to S3, stamps ready_at.

Run cadence: on-demand. NOT a beat task. The enqueue path calls
generate_export_bundle.delay(job_id) and returns immediately; this
task does the heavy work in the worker process.

Failure handling:
  * Transient errors (boto3 timeout, DB connection drop): Celery
    retries up to 3 times with exponential backoff.
  * Permanent errors: DataExportService.generate_bundle catches the
    exception, stamps the job as 'failed' with error_message, and
    re-raises wrapped in ExportGenerationError. The audit row is
    emitted before the re-raise.
"""
from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name="app.worker.tasks.data_export.generate_export_bundle",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def generate_export_bundle(self, job_id: str) -> dict:
    """Generate the bundle for the given job_id.

    Returns a small summary dict for observability:
        {
            "job_id": str,
            "status": "ready" | "failed",
        }
    """
    # Lazy imports avoid the circular-import dance described in the
    # parallel retention.py task.
    from app.core.config import settings
    from app.db.session import OpsSessionLocal, SessionLocal
    from app.repositories.admin_audit_repository import AdminAuditRepository
    from app.services.data_export_service import DataExportService
    import boto3

    # Arc 10 re-open Gap 5: use OpsSessionLocal (luciel_ops, BYPASSRLS)
    # when available so cross-admin bundle reads are not silently blocked
    # by per-admin RLS policies. Fall back to SessionLocal only when the
    # ops role is not wired (local dev / CI without LUCIEL_OPS_DB_URL).
    # Same pattern as app/worker/tasks/retention.py.
    db = (OpsSessionLocal or SessionLocal)()
    try:
        s3 = boto3.client("s3", region_name=settings.aws_region)
        audit_repo = AdminAuditRepository(db)
        svc = DataExportService(
            db=db,
            s3_client=s3,
            s3_bucket=getattr(
                settings, "data_export_bucket", "luciel-data-exports"
            ),
            audit_repository=audit_repo,
        )
        try:
            svc.generate_bundle(job_id)
            return {"job_id": job_id, "status": "ready"}
        except Exception:
            # DataExportService.generate_bundle has already stamped
            # the job failed and emitted an audit row. We re-raise
            # so Celery records the failure for observability.
            logger.exception(
                "data_export task: generate_bundle failed job_id=%s",
                job_id,
            )
            raise
    finally:
        db.close()


@shared_task(
    bind=True,
    name="app.worker.tasks.data_export.expire_old_signed_urls",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=2,
)
def expire_old_signed_urls(self) -> dict:
    """Beat task -- mark ready jobs whose signed-URL TTL elapsed as 'expired'.

    Tier-aware via the sticky tier_at_request on each job: an
    Enterprise admin's 90-day bundle stays ready for 90 days even if
    they downgrade meanwhile. The signed_url_expires_at column
    captures the absolute deadline at signing time.

    Run cadence: once daily, same beat slot as the retention worker.
    Cheap query backed by the partial indexes on data_export_jobs.
    """
    from datetime import datetime, timezone

    from sqlalchemy import text as sql_text

    from app.db.session import OpsSessionLocal, SessionLocal

    # Arc 10 re-open Gap 5: BYPASSRLS via OpsSessionLocal so the
    # cross-admin expiry UPDATE actually matches rows. Without this,
    # SessionLocal (luciel_app role) ran against the data_export_jobs
    # RLS policy with no app.admin_id GUC set -> zero matches per scan
    # -> exports never expire (silent dead letter). Caught by the new
    # Gap 5 test suite (test_arc10_data_export.py).
    db = (OpsSessionLocal or SessionLocal)()
    try:
        now = datetime.now(timezone.utc)
        res = db.execute(
            sql_text(
                """
                UPDATE data_export_jobs
                   SET status = 'expired'
                 WHERE status = 'ready'
                   AND signed_url_expires_at IS NOT NULL
                   AND signed_url_expires_at < :now
                """
            ),
            {"now": now},
        )
        db.commit()
        expired_count = int(res.rowcount or 0)
        logger.info(
            "data_export.expire_old_signed_urls: expired_count=%d",
            expired_count,
        )
        return {"expired_count": expired_count}
    finally:
        db.close()
