"""Unit 13e §3.4.10 — transcript S3 cold-archive policy (SELECT/mark path).

§3.4.10 transcript retention has two clocks:
    * HOT (Postgres) deletion: 30 days Free / 1 year Pro.
    * COLD (S3) archive: at 90 days the transcript is moved to S3 cold
      storage rather than kept hot.

This module is the DETERMINISTIC SELECT/mark policy leg of the cold
archive: given the current time it identifies which sessions' transcripts
have crossed the 90-day cold-archive horizon and are therefore eligible to
be moved to S3.

The actual S3 MOVE (the boto3 PUT of the transcript object + the hot-row
delete) is FLAGGED DEPLOY-PHASE: it requires a configured cold-archive
bucket + an archiver IAM role, mirroring the audit cold-archive subsystem
(``app.worker.tasks.audit_retention`` / ``AuditRetentionService``). Until
that infra is wired, this module's job is to make the eligibility decision
deterministic and testable so the move can be bolted on without changing
the policy.

No new schema is introduced here. Eligibility is computed purely from
``sessions.updated_at`` (last activity) against the 90-day horizon; a
``cold_archived_at`` marker column is a deploy-phase migration that lands
with the S3 move.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# §3.4.10 cold-archive horizon. Platform constant, NOT admin-configurable.
# At 90 days a transcript is moved from hot Postgres to S3 cold storage.
TRANSCRIPT_COLD_ARCHIVE_DAYS = 90


@dataclass(frozen=True)
class ColdArchiveCandidate:
    """A session transcript eligible for S3 cold-archive.

    Frozen so the policy output is a contract, not a mutable hint. The
    deploy-phase mover consumes these and performs the S3 PUT + hot-row
    delete (flagged deploy-phase).
    """

    session_id: str
    admin_id: str
    luciel_instance_id: int | None
    last_activity: datetime


def select_cold_archive_candidates(
    sessions: list,
    *,
    now: datetime | None = None,
    horizon_days: int = TRANSCRIPT_COLD_ARCHIVE_DAYS,
) -> list[ColdArchiveCandidate]:
    """Return the transcripts past the cold-archive horizon.

    Deterministic (no LLM, no I/O). ``sessions`` is any iterable of
    objects exposing ``id``, ``admin_id``, ``luciel_instance_id`` and
    ``updated_at`` (the SessionModel shape). A session is eligible when
    its last activity is older than ``horizon_days``.

    The caller is expected to have pre-SELECTed sessions under the
    appropriate scope/role; this function is the pure horizon filter so it
    can be unit-tested without a DB and reused by the deploy-phase mover.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=horizon_days)

    candidates: list[ColdArchiveCandidate] = []
    for s in sessions:
        last = s.updated_at
        if last is None:
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last < cutoff:
            candidates.append(
                ColdArchiveCandidate(
                    session_id=s.id,
                    admin_id=s.admin_id,
                    luciel_instance_id=s.luciel_instance_id,
                    last_activity=last,
                )
            )
    return candidates
