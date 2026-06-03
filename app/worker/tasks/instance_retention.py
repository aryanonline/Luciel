"""Instance retention hard-purge task.

Arc 11 Closeout PR-A. Sister to ``app.worker.tasks.retention``
(tenant-level hard-purge). This worker handles the instance-level
30-day grace clock per Architecture §3.6.1 ("soft-delete window
measured from ``soft_deleted_at`` (locked)") and Customer Journey
§4.5 Phase 8 ("Delete this instance ... 30-day grace window, then
hard-deleted").

What this task does
-------------------

Nightly at 08:30 UTC (30 minutes after the tenant retention sweep, so
the two beat tasks do not contend for the worker's prefetch slot):

1. Scan ``instances`` for rows where
   ``instance_status = 'deleted'`` AND
   ``soft_deleted_at < now() - INTERVAL '30 days'``.
   Backed by the partial index ``ix_instances_soft_deleted_sweep``
   (Alembic ``arc11_closeout_a_instance_lifecycle``).

2. For each eligible instance, in its own transaction, hard-delete
   the cascade:
       knowledge_chunks   WHERE instance_id = ?
       knowledge_sources  WHERE instance_id = ?
       leads              WHERE luciel_instance_id = ?  (if table exists)
       traces             WHERE luciel_instance_id = ?
       sessions / messages WHERE luciel_instance_id = ?
                          (messages.session_id has ON DELETE CASCADE)
       api_keys           WHERE luciel_instance_id = ?
       instances          WHERE id = ? (the row itself)

3. Emit one ``ACTION_INSTANCE_HARD_PURGED`` audit row per purged
   instance with the row-count manifest in ``after_json``. The audit
   row references the (now-deleted) instance_slug as
   ``resource_natural_id`` so the audit chain remains walkable after
   the FK target is gone.

Why a separate worker (not piggybacking on ``retention.run_retention_purge``)
----------------------------------------------------------------------------

* Different scope: tenant retention triggers on
  ``admins.closure_initiated_at``; instance retention triggers on
  ``instances.soft_deleted_at``. A tenant can have surviving (active)
  instances while one of its instances is in the grace window — the
  two clocks must not be conflated.
* Different cascade shape: tenant hard-purge cleans up the full
  multi-instance state and tombstones the admin row; instance
  hard-purge cleans only this instance's children and removes the
  instance row itself (the tenant is still alive).
* Different audit verb: ``ACTION_INSTANCE_HARD_PURGED`` vs
  ``ACTION_TENANT_HARD_PURGED``. A regulator scanning the chain by
  verb gets a precise answer.

Uses ``OpsSessionLocal`` (Arc 9 C6.1 BYPASSRLS role) so the cross-
instance scan + per-instance DELETE chain runs without binding to a
single ``app.admin_id`` GUC. Mirrors the role posture of
``app.worker.tasks.retention``.
"""
from __future__ import annotations

import logging
import traceback
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from celery import shared_task
from sqlalchemy import text

from app.db.session import OpsSessionLocal
from app.models.admin_audit_log import (
    ACTION_INSTANCE_HARD_PURGED,
    RESOURCE_INSTANCE,
)
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)
from app.repositories.instance_repository import INSTANCE_RESTORE_GRACE_DAYS

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


_log = logging.getLogger(__name__)


# Re-export under a worker-local alias so anyone reading this file in
# isolation sees the constant. The doctrinal source-of-truth is the
# repository module; both ends of the lifecycle clock must agree.
INSTANCE_RETENTION_WINDOW_DAYS = INSTANCE_RESTORE_GRACE_DAYS


@shared_task(
    bind=True,
    name="app.worker.tasks.instance_retention.run_instance_retention_purge",
    # Retry policy matches the tenant-level retention worker: 3 attempts,
    # exponential backoff (jittered). A failed nightly run will be retried
    # by Celery; if all retries fail, the beat schedule runs again the
    # next night anyway so we self-heal within 24h either way.
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=10,
    retry_jitter=True,
    max_retries=3,
)
def run_instance_retention_purge(self):
    """Nightly: hard-delete instances soft-deleted >30 days ago.

    Returns a dict summary for ease of observation:
        {
            "scanned_count": int,
            "purged_count": int,
            "errored_count": int,
            "errored_instance_ids": list[int],
        }
    """
    started_at = datetime.now(timezone.utc)
    cutoff = started_at - timedelta(days=INSTANCE_RETENTION_WINDOW_DAYS)
    _log.info(
        "instance_retention_purge starting: cutoff=%s (older than %d days "
        "from %s)",
        cutoff.isoformat(),
        INSTANCE_RETENTION_WINDOW_DAYS,
        started_at.isoformat(),
    )

    if OpsSessionLocal is None:
        _log.error(
            "instance_retention_purge ABORTED: OpsSessionLocal is None. "
            "settings.luciel_ops_db_url must be configured for the "
            "instance retention worker to run."
        )
        return {
            "scanned_count": 0,
            "purged_count": 0,
            "errored_count": 0,
            "errored_instance_ids": [],
            "aborted": "ops_session_unavailable",
        }

    scan_db: Session = OpsSessionLocal()
    try:
        eligible = _scan_eligible_instances(scan_db, cutoff)
    finally:
        scan_db.close()

    _log.info(
        "instance_retention_purge scan complete: %d instance(s) eligible",
        len(eligible),
    )

    purged_count = 0
    errored_count = 0
    errored_instance_ids: list[int] = []

    for instance_id, admin_id, instance_slug in eligible:
        per_instance_db: Session = OpsSessionLocal()
        try:
            row_counts = _hard_delete_instance_cascade(
                per_instance_db,
                instance_id=instance_id,
                admin_id=admin_id,
                instance_slug=instance_slug,
            )
            per_instance_db.commit()
            purged_count += 1
            _log.info(
                "instance_retention_purge OK instance_id=%s admin_id=%s "
                "row_counts=%s",
                instance_id,
                admin_id,
                row_counts,
            )
        except Exception:
            per_instance_db.rollback()
            errored_count += 1
            errored_instance_ids.append(instance_id)
            _log.error(
                "instance_retention_purge FAILED instance_id=%s "
                "admin_id=%s traceback:\n%s",
                instance_id,
                admin_id,
                traceback.format_exc(),
            )
        finally:
            per_instance_db.close()

    summary = {
        "scanned_count": len(eligible),
        "purged_count": purged_count,
        "errored_count": errored_count,
        "errored_instance_ids": errored_instance_ids,
    }
    _log.info("instance_retention_purge complete: %s", summary)
    return summary


def _scan_eligible_instances(
    db: "Session",
    cutoff: datetime,
) -> list[tuple[int, str, str]]:
    """Return ``(id, admin_id, instance_slug)`` for every instance whose
    grace clock has expired.

    Single SELECT against the partial index
    ``ix_instances_soft_deleted_sweep`` (Alembic
    ``arc11_closeout_a_instance_lifecycle``). Ordered by
    ``soft_deleted_at ASC`` so the oldest purges run first — if the
    nightly job is interrupted partway, the next run picks up where
    this one left off in FIFO order.
    """
    sql = text(
        """
        SELECT id, admin_id, instance_slug
          FROM instances
         WHERE instance_status = 'deleted'
           AND soft_deleted_at IS NOT NULL
           AND soft_deleted_at < :cutoff
         ORDER BY soft_deleted_at ASC
        """
    )
    result = db.execute(sql, {"cutoff": cutoff})
    return [(row[0], row[1], row[2]) for row in result]


def _hard_delete_instance_cascade(
    db: "Session",
    *,
    instance_id: int,
    admin_id: str,
    instance_slug: str,
) -> dict[str, int]:
    """Run the per-instance DELETE chain in a single transaction.

    The order matters: children before parents to satisfy the FKs that
    are not declared with ``ON DELETE CASCADE``. ``messages`` is
    deleted indirectly via ``sessions.session_id ON DELETE CASCADE``.

    Tables are deleted with ``execute(text(...))`` rather than the ORM
    so this code path does not need to import every model module
    (which would pull half the app on Celery boot for a once-a-day
    sweep). Each table's filter column is documented inline.

    The audit row is emitted BEFORE the instance row is deleted, so
    the FK from ``admin_audit_logs.luciel_instance_id`` is still
    satisfiable. The repo's ``record()`` runs with ``autocommit=False``
    so the audit row rides this transaction.
    """
    counts: dict[str, int] = {}

    # knowledge_chunks: per-chunk vector embeddings. Filter column is
    # ``instance_id`` (renamed from luciel_instance_id in Arc 11 B).
    counts["knowledge_chunks"] = _delete_count(
        db,
        "DELETE FROM knowledge_chunks WHERE instance_id = :iid",
        {"iid": instance_id},
    )

    # knowledge_sources: parent rows for the chunks above.
    counts["knowledge_sources"] = _delete_count(
        db,
        "DELETE FROM knowledge_sources WHERE instance_id = :iid",
        {"iid": instance_id},
    )

    # traces: per-turn observability rows.
    counts["traces"] = _delete_count(
        db,
        "DELETE FROM traces WHERE luciel_instance_id = :iid",
        {"iid": instance_id},
    )

    # sessions: messages cascade via FK ON DELETE CASCADE.
    counts["sessions"] = _delete_count(
        db,
        "DELETE FROM sessions WHERE luciel_instance_id = :iid",
        {"iid": instance_id},
    )

    # api_keys: embed keys + any rotated-out admin keys still bound to
    # the instance. The widget can no longer authenticate after this.
    counts["api_keys"] = _delete_count(
        db,
        "DELETE FROM api_keys WHERE luciel_instance_id = :iid",
        {"iid": instance_id},
    )

    # instance_connections: Arc 17. The soft-delete cascade already
    # revoked these rows (revoked_at set) + enqueued secret cleanup into
    # secret_cleanup_outbox; the drain worker handles the actual secret
    # store deletion. Here we hard-delete the rows so the RESTRICT FK
    # instance_connections.instance_id -> instances.id does not block the
    # instance DELETE below. No secret value lives in these rows; the
    # pointer cleanup is the outbox worker's job, not this purge.
    counts["instance_connections"] = _delete_count(
        db,
        "DELETE FROM instance_connections WHERE instance_id = :iid",
        {"iid": instance_id},
    )

    # Audit row BEFORE the instance row goes away, so the FK
    # ``admin_audit_logs.luciel_instance_id -> instances.id`` is still
    # satisfiable. (The FK is RESTRICT; an audit row pointing at a
    # deleted instance would block the DELETE if emitted after.)
    audit_repo = AdminAuditRepository(db)
    audit_repo.record(
        ctx=AuditContext.system(label="instance_retention_purge"),
        admin_id=admin_id,
        action=ACTION_INSTANCE_HARD_PURGED,
        resource_type=RESOURCE_INSTANCE,
        resource_pk=instance_id,
        resource_natural_id=instance_slug,
        luciel_instance_id=instance_id,
        after={
            "row_counts": counts,
            "grace_window_days": INSTANCE_RETENTION_WINDOW_DAYS,
        },
        note="instance hard-purged after retention window",
        autocommit=False,
    )

    # Finally, the instance row itself.
    counts["instances"] = _delete_count(
        db,
        "DELETE FROM instances WHERE id = :iid",
        {"iid": instance_id},
    )

    return counts


def _delete_count(db: "Session", sql: str, params: dict) -> int:
    """Helper: execute a DELETE and return ``rowcount``.

    The ``text()`` wrapping happens here so the caller's SQL strings
    stay readable. ``rowcount`` is reliable across psycopg2 +
    SQLAlchemy for plain DELETE statements (not affected by RETURNING
    or autoflush quirks)."""
    result = db.execute(text(sql), params)
    return int(result.rowcount or 0)
