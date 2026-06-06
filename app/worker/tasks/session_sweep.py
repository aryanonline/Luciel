"""Unit 13e §3.4.8 — deterministic session inactivity sweep + finalization.

Why this task exists
--------------------
§3.4.8 says: live session state is Redis-keyed with TTL = the channel's
inactivity timeout, and on TTL expiry the §3.4.7 summarization/finalization
pipeline fires. The clean production trigger is a Redis keyspace-expiry
notification — but that needs ``notify-keyspace-events`` enabled on the
deployed Redis, which is a deploy-phase config. To keep the expiry →
finalization path VERIFIABLE in-sandbox (and as the reliable fallback even
in production), this module implements a DETERMINISTIC SWEEP: a periodic
task that finds active sessions whose last activity is older than their
channel-class inactivity timeout and finalizes them.

Finalization here = mark the session ended (status='ended') and emit the
§3.4.8 ``ACTION_SESSION_FINALIZED_INACTIVITY`` audit row. The §3.4.7
summary fold-in already happens per-turn in the cognition finalizer; the
sweep is the lifecycle event that closes the session so the §3.4.9
reopened-thread rule (a new inbound after end → NEW session / NEW budget
unit) applies.

The Redis keyspace-notification optimization is FLAGGED deploy-phase (see
``app.runtime.session_timeouts.session_redis_key``). The sweep does not
require it.

Scoping
-------
Uses OpsSessionLocal (luciel_ops, BYPASSRLS) like the retention worker so
a single scan sees every tenant's sessions. The audit row is written with
the session's OWN admin_id (read from the row), so audit attribution stays
correct per-tenant even though the scan crosses tenants under BYPASSRLS.
"""
from __future__ import annotations

import logging
import traceback
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from celery import shared_task
from sqlalchemy import select

from app.runtime.session_timeouts import (
    channel_class,
    inactivity_timeout_seconds,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)


def find_and_finalize_expired_sessions(
    db: "Session",
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Core, DB-session-injected sweep. Deterministic + unit-testable.

    Scans active sessions and finalizes every one whose idle time
    (now - updated_at) exceeds its channel-class inactivity timeout.
    Returns a list of finalization-summary dicts (one per finalized
    session). Caller owns the transaction commit.

    The per-session comparison uses the channel-class timeout from
    ``app.runtime.session_timeouts`` so a new channel inherits a class
    without touching this sweep.
    """
    from app.cognition.summarizer import summarize
    from app.models.admin_audit_log import (
        ACTION_SESSION_FINALIZED_INACTIVITY,
        RESOURCE_SESSION,
    )
    from app.models.message import MessageModel
    from app.models.session import SessionModel
    from app.models.session_summary import SessionSummary
    from app.repositories.admin_audit_repository import (
        AdminAuditRepository,
        AuditContext,
    )

    now = now or datetime.now(timezone.utc)

    # Only active sessions are finalization candidates. An already-ended
    # session is a tombstone — re-finalizing it would double-emit the
    # audit row (the sweep is idempotent by this filter).
    stmt = select(SessionModel).where(SessionModel.status == "active")
    candidates = list(db.scalars(stmt).all())

    finalized: list[dict] = []
    audit_repo = AdminAuditRepository(db)

    for sess in candidates:
        last_activity = sess.updated_at
        if last_activity is None:
            continue
        # updated_at is tz-aware (DateTime(timezone=True)); guard a naive
        # value defensively so the subtraction never raises.
        if last_activity.tzinfo is None:
            last_activity = last_activity.replace(tzinfo=timezone.utc)

        timeout_s = inactivity_timeout_seconds(sess.channel)
        idle_s = (now - last_activity).total_seconds()
        if idle_s < timeout_s:
            continue

        cls = channel_class(sess.channel)
        sess.status = "ended"
        db.add(sess)

        # §3.4.10 — persist the cross-session summary at session end.
        # Build the recap from this session's message history via the
        # single §3.4.7 summarizer, then write one SessionSummary row.
        # Best-effort: a summary that fails to build (e.g. no messages)
        # must not block the lifecycle finalization.
        try:
            msgs = list(
                db.scalars(
                    select(MessageModel)
                    .where(MessageModel.session_id == sess.id)
                    .order_by(MessageModel.created_at.asc())
                ).all()
            )
            summary_text = summarize(
                [{"role": m.role, "content": m.content} for m in msgs]
            )
            if summary_text:
                db.add(
                    SessionSummary(
                        admin_id=sess.admin_id,
                        luciel_instance_id=sess.luciel_instance_id,
                        resolved_lead_id=sess.resolved_lead_id,
                        session_id=sess.id,
                        summary=summary_text,
                    )
                )
        except Exception:  # noqa: BLE001
            _log.warning(
                "session_sweep: summary persistence failed for session=%s "
                "— lifecycle finalization continues",
                sess.id,
            )

        audit_repo.record(
            ctx=AuditContext.system(label="session_inactivity_sweep"),
            admin_id=sess.admin_id,
            action=ACTION_SESSION_FINALIZED_INACTIVITY,
            resource_type=RESOURCE_SESSION,
            resource_natural_id=sess.id,
            luciel_instance_id=sess.luciel_instance_id,
            after={
                "session_id": sess.id,
                "channel": sess.channel,
                "channel_class": cls,
                "timeout_seconds": timeout_s,
                "resolved_lead_id": sess.resolved_lead_id,
                "idle_seconds": int(idle_s),
            },
            note=f"inactivity:{cls}",
        )
        finalized.append(
            {
                "session_id": sess.id,
                "admin_id": sess.admin_id,
                "channel_class": cls,
                "idle_seconds": int(idle_s),
            }
        )

    return finalized


@shared_task(
    bind=True,
    name="app.worker.tasks.session_sweep.run_session_inactivity_sweep",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=10,
    retry_jitter=True,
    max_retries=3,
)
def run_session_inactivity_sweep(self):
    """Periodic: finalize sessions past their channel-class TTL.

    Returns a dict summary for observability:
        {"finalized_count": int, "errored": bool}
    """
    from app.db.session import OpsSessionLocal

    if OpsSessionLocal is None:
        _log.error(
            "session_inactivity_sweep ABORTED: OpsSessionLocal is None. "
            "settings.luciel_ops_db_url must be configured."
        )
        return {"finalized_count": 0, "aborted": "ops_session_unavailable"}

    db = OpsSessionLocal()
    try:
        finalized = find_and_finalize_expired_sessions(db)
        db.commit()
        _log.info(
            "session_inactivity_sweep complete: finalized %d session(s)",
            len(finalized),
        )
        return {"finalized_count": len(finalized), "errored": False}
    except Exception:
        db.rollback()
        _log.error(
            "session_inactivity_sweep FAILED traceback:\n%s",
            traceback.format_exc(),
        )
        raise
    finally:
        db.close()
