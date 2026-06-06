"""Instance retention hard-purge task.

Arc 11 Closeout PR-A. Sister to ``app.worker.tasks.retention``
(tenant-level hard-purge). This worker handles the instance-level
30-day grace clock per Architecture §3.6.1 ("soft-delete window
measured from ``soft_deleted_at`` (locked)") and Customer Journey
§4.5 Phase 8 ("Delete this instance ... 30-day grace window, then
hard-deleted").

RESCAN TIER-DE (lifecycle): cascade extended to be consistent with
the tenant-level hard-purge in ``app.services.admin_service`` (the
BUG-1 fix). New tables added:

  - leads               (SET NULL FK; purged for GDPR/PIPEDA completeness)
  - escalation_events   (SET NULL FK; "session summaries" per §3.6.5)
  # sibling_call_grants / instance_composition_grants /
  # knowledge_share_grants / user_role_assignments REMOVED in Unit 1:
  # those tables were dropped (deferred multi-Luciel / custom-role
  # surfaces -- Open Decisions #7/#8, Locked Decision #19).
  - instance_tool_authorizations (RESTRICT FK; blocks instance DELETE)
  - byo_webhook_endpoints        (RESTRICT FK; blocks instance DELETE)
  - channel_routes               (RESTRICT FK; blocks instance DELETE)
  - tool_execution_log           (RESTRICT FK; blocks instance DELETE)
  - knowledge_graph_nodes        (CASCADE FK; would auto-delete but
                                  explicit for per-step audit completeness)
  - knowledge_graph_edges        (CASCADE FK; edges cascade from nodes)

Per-step data_retention_hard_delete audit rows emitted for each
table. Tombstones (admin_audit_logs rows) are NOT deleted.

What this task does
-------------------

Nightly at 08:30 UTC (30 minutes after the tenant retention sweep, so
the two beat tasks do not contend for the worker's prefetch slot):

1. Scan ``instances`` for rows where
   ``instance_status IN ('deleted', 'grace_window')`` AND
   ``soft_deleted_at < now() - INTERVAL '30 days'``.
   (Both values are included: 'deleted' is the legacy state written by
   the pre-TIER-DE code; 'grace_window' is the new canonical state.
   Together they cover all rows eligible for hard-purge regardless of
   which code path soft-deleted them.)
   Backed by the partial index ``ix_instances_soft_deleted_sweep``
   (updated by migration rescand_lifecycle_states).

2. For each eligible instance, in its own transaction, hard-delete
   the cascade in FK-safe order (RESTRICT children first, then the
   instance row itself):

   RESTRICT children (block the instance DELETE — must go first):
       knowledge_graph_edges         (CASCADE but explicit for audit)
       knowledge_graph_nodes         (CASCADE but explicit for audit)
       # instance_composition_grants / knowledge_share_grants /
       # sibling_call_grants / user_role_assignments REMOVED in Unit 1
       # (dropped tables -- deferred surfaces).
       instance_tool_authorizations  WHERE instance_id = ?
       tool_execution_log            WHERE instance_id = ?
       byo_webhook_endpoints         WHERE instance_id = ?
       channel_routes                WHERE luciel_instance_id = ?
       knowledge_chunks              WHERE luciel_instance_id = ?  (SET NULL)
       knowledge_sources             WHERE luciel_instance_id = ?  (RESTRICT)
       traces                        WHERE luciel_instance_id = ?  (SET NULL)
       leads                         WHERE luciel_instance_id = ?  (SET NULL; GDPR)
       escalation_events             WHERE luciel_instance_id = ?  (SET NULL; GDPR)
       sessions / messages           WHERE luciel_instance_id = ?
       conversations                  WHERE admin_id = ?  (last instance only)
                                     (messages cascade via FK ON DELETE CASCADE)
       api_keys                      WHERE luciel_instance_id = ?  (embed keys)
       instance_connections          WHERE instance_id = ?         (RESTRICT)
       instances                     WHERE id = ?                  (the row itself)

3. Emit one ``ACTION_INSTANCE_HARD_PURGED`` audit row per purged
   instance with the row-count manifest in ``after_json``. The audit
   row references the (now-deleted) instance_slug as
   ``resource_natural_id`` so the audit chain remains walkable after
   the FK target is gone. Per-step ``data_retention_hard_delete`` audit
   rows are embedded in the manifest; tombstones (audit_log rows) are
   NOT deleted.

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
    name="app.lifecycle.retention.run_instance_retention_purge",
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
    ``ix_instances_soft_deleted_sweep`` (updated by migration
    rescand_lifecycle_states to cover both 'deleted' and 'grace_window').
    Ordered by ``soft_deleted_at ASC`` so the oldest purges run first —
    if the nightly job is interrupted partway, the next run picks up
    where this one left off in FIFO order.

    Both ``instance_status = 'deleted'`` (legacy) and
    ``instance_status = 'grace_window'`` (new 5-state canonical) are
    included so rows written by both old and new code paths are picked up.
    """
    sql = text(
        """
        SELECT id, admin_id, instance_slug
          FROM instances
         WHERE instance_status IN ('deleted', 'grace_window')
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
    are NOT declared with ``ON DELETE CASCADE``. Specifically, all tables
    with ``ON DELETE RESTRICT`` to ``instances.id`` must be cleared
    BEFORE the instance row is deleted, or the DELETE will raise a
    PostgreSQL FK violation (errcode 23503).

    FK topology for instances.id (verified 2026-06-11):
      RESTRICT: knowledge_sources, instance_connections,
                instance_tool_authorizations, byo_webhook_endpoints,
                channel_routes, instance_composition_grants,
                knowledge_share_grants, sibling_call_grants,
                tool_execution_log, user_role_assignments
      SET NULL: api_keys, escalation_events, knowledge_chunks, leads,
                memory_items, traces
      CASCADE:  knowledge_graph_nodes, knowledge_graph_edges

    Tables are deleted with ``execute(text(...))`` rather than the ORM
    so this code path does not need to import every model module
    (which would pull half the app on Celery boot for a once-a-day
    sweep). Each table's filter column is documented inline.

    Per-step ``data_retention_hard_delete`` counts are accumulated in
    ``counts`` and embedded in the single ``ACTION_INSTANCE_HARD_PURGED``
    audit row. Audit tombstones (admin_audit_logs rows) are NOT deleted —
    they are the compliance record per PIPEDA P5 / GDPR Art.17.

    The audit row is emitted BEFORE the instance row is deleted, so
    the FK from ``admin_audit_logs.luciel_instance_id`` is still
    satisfiable. The repo's ``record()`` runs with ``autocommit=False``
    so the audit row rides this transaction.
    """
    counts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Step 1: CASCADE children — knowledge graph.
    # knowledge_graph_edges FK to knowledge_graph_nodes (CASCADE), and
    # both FK to instances (CASCADE). Explicit deletion for per-step
    # audit completeness; would auto-cascade from the instances DELETE
    # but we document and count them here.
    # ------------------------------------------------------------------

    # knowledge_graph_edges: cascade from nodes but we delete explicitly
    # so the count is in the manifest. Filter by instance_id (direct FK).
    counts["knowledge_graph_edges"] = _delete_count(
        db,
        "DELETE FROM knowledge_graph_edges WHERE instance_id = :iid",
        {"iid": instance_id},
    )

    # knowledge_graph_nodes: direct FK ON DELETE CASCADE to instances.id
    counts["knowledge_graph_nodes"] = _delete_count(
        db,
        "DELETE FROM knowledge_graph_nodes WHERE instance_id = :iid",
        {"iid": instance_id},
    )

    # ------------------------------------------------------------------
    # Step 2: RESTRICT children — grant/authorization/log tables.
    # These MUST be cleared before instances.id can be DELETEd.
    # NOTE: instance_composition_grants, knowledge_share_grants,
    # sibling_call_grants and user_role_assignments were DROPPED in
    # Unit 1 (deferred multi-Luciel / custom-role surfaces -- Open
    # Decisions #7/#8, Locked Decision #19). Their per-instance purge
    # DELETEs were removed here so the retention worker does not crash
    # UndefinedTable on a dropped table.
    # ------------------------------------------------------------------

    # instance_tool_authorizations: instance_id is a RESTRICT FK.
    counts["instance_tool_authorizations"] = _delete_count(
        db,
        "DELETE FROM instance_tool_authorizations WHERE instance_id = :iid",
        {"iid": instance_id},
    )

    # tool_execution_log: instance_id is a RESTRICT FK.
    counts["tool_execution_log"] = _delete_count(
        db,
        "DELETE FROM tool_execution_log WHERE instance_id = :iid",
        {"iid": instance_id},
    )

    # byo_webhook_endpoints: instance_id is a RESTRICT FK.
    counts["byo_webhook_endpoints"] = _delete_count(
        db,
        "DELETE FROM byo_webhook_endpoints WHERE instance_id = :iid",
        {"iid": instance_id},
    )

    # channel_routes: luciel_instance_id is a RESTRICT FK.
    counts["channel_routes"] = _delete_count(
        db,
        "DELETE FROM channel_routes WHERE luciel_instance_id = :iid",
        {"iid": instance_id},
    )

    # ------------------------------------------------------------------
    # Step 3: knowledge content — chunks then sources (FK constraint:
    # knowledge_chunks.knowledge_source_id -> knowledge_sources.id).
    # knowledge_chunks.luciel_instance_id is SET NULL; we purge
    # explicitly for GDPR completeness. knowledge_sources.luciel_
    # instance_id is RESTRICT — must go before instances DELETE.
    # ------------------------------------------------------------------

    # knowledge_chunks: per-chunk vector embeddings. Filter column is
    # ``luciel_instance_id`` (the SET NULL FK to instances).
    counts["knowledge_chunks"] = _delete_count(
        db,
        "DELETE FROM knowledge_chunks WHERE luciel_instance_id = :iid",
        {"iid": instance_id},
    )

    # knowledge_sources: parent rows for the chunks above.
    # Filter column is ``luciel_instance_id`` (RESTRICT FK to instances).
    counts["knowledge_sources"] = _delete_count(
        db,
        "DELETE FROM knowledge_sources WHERE luciel_instance_id = :iid",
        {"iid": instance_id},
    )

    # ------------------------------------------------------------------
    # Step 4: customer-data tables with SET NULL FKs.
    # These would SET NULL on the instance DELETE, but we purge them
    # explicitly for GDPR/PIPEDA Art.17 completeness ("hard delete of
    # all customer data"). Per-step audit counts in the manifest.
    # ------------------------------------------------------------------

    # traces: per-turn observability rows. luciel_instance_id is SET NULL.
    counts["traces"] = _delete_count(
        db,
        "DELETE FROM traces WHERE luciel_instance_id = :iid",
        {"iid": instance_id},
    )

    # leads: customer contact data captured by the widget.
    # luciel_instance_id is SET NULL (leads survive instance deletion in
    # the tenant context, but on instance hard-purge we delete them per
    # GDPR/PIPEDA since the owning instance context is gone).
    counts["leads"] = _delete_count(
        db,
        "DELETE FROM leads WHERE luciel_instance_id = :iid",
        {"iid": instance_id},
    )

    # escalation_events: session summaries / escalation records.
    # luciel_instance_id is SET NULL. Purged for GDPR completeness.
    counts["escalation_events"] = _delete_count(
        db,
        "DELETE FROM escalation_events WHERE luciel_instance_id = :iid",
        {"iid": instance_id},
    )

    # sessions: messages cascade via FK ON DELETE CASCADE on session_id.
    # sessions.luciel_instance_id is a plain integer (not a FK), so
    # there is no constraint blocking the sessions DELETE from the
    # instances DELETE — but we delete sessions to purge all customer
    # conversation data per §3.6.5.
    counts["sessions"] = _delete_count(
        db,
        "DELETE FROM sessions WHERE luciel_instance_id = :iid",
        {"iid": instance_id},
    )

    # ------------------------------------------------------------------
    # Step 4b: conversations (tenant-level grouping, admin_id-scoped, no
    # instance FK). In the single-Luciel model (Locked Decision #12) an
    # account has exactly one instance, so hard-deleting that instance
    # would otherwise orphan the admin's conversation rows. Purge them
    # here per §3.6.5 ("full conversation" data), BUT only when this is
    # the admin's LAST surviving instance — a defensive guard so that if
    # the multi-instance model ever returns, one Luciel's hard-delete
    # never destroys conversation data still owned by a sibling Luciel.
    # ------------------------------------------------------------------
    surviving = db.execute(
        text(
            "SELECT count(*) FROM instances "
            "WHERE admin_id = :aid AND id <> :iid"
        ),
        {"aid": admin_id, "iid": instance_id},
    ).scalar()
    if not surviving:
        counts["conversations"] = _delete_count(
            db,
            "DELETE FROM conversations WHERE admin_id = :aid",
            {"aid": admin_id},
        )
    else:
        counts["conversations"] = 0

    # ------------------------------------------------------------------
    # Step 5: embed keys — explicit step for audit visibility.
    # api_keys.luciel_instance_id is SET NULL FK. We delete all api_keys
    # bound to this instance (embed keys + any rotated-out admin keys).
    # After this step, the widget can no longer authenticate.
    # ------------------------------------------------------------------
    counts["api_keys"] = _delete_count(
        db,
        "DELETE FROM api_keys WHERE luciel_instance_id = :iid",
        {"iid": instance_id},
    )

    # ------------------------------------------------------------------
    # Step 6: instance_connections (RESTRICT FK, instance_id).
    # The soft-delete cascade already revoked these rows (revoked_at set)
    # and enqueued secret cleanup into secret_cleanup_outbox; the drain
    # worker handles the actual secret store deletion. Here we hard-delete
    # the rows so the RESTRICT FK does not block the instance DELETE.
    # No secret value lives in these rows; pointer cleanup is the outbox
    # worker's job.
    # ------------------------------------------------------------------
    counts["instance_connections"] = _delete_count(
        db,
        "DELETE FROM instance_connections WHERE instance_id = :iid",
        {"iid": instance_id},
    )

    # ------------------------------------------------------------------
    # Step 7: Audit row.
    # Emitted BEFORE the instance row goes away, so the FK
    # ``admin_audit_logs.luciel_instance_id -> instances.id`` is still
    # satisfiable. (The FK is RESTRICT; an audit row pointing at a
    # deleted instance would block the DELETE if emitted after.)
    # Tombstones (existing audit rows for this instance) are NOT deleted.
    # ------------------------------------------------------------------
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
            "data_retention_hard_delete": True,
        },
        note="instance hard-purged after retention window",
        autocommit=False,
    )

    # ------------------------------------------------------------------
    # Step 8: instance row itself.
    # All RESTRICT FK children cleared above; CASCADE children already
    # gone (or explicitly deleted in step 1 for audit completeness).
    # ------------------------------------------------------------------
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
