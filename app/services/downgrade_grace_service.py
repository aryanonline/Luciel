"""Downgrade grace service -- Arc 10.

Owns the read-only grace window between a downgrade initiation and
its day-30 enforcement. Per Customer Journey Phase 8 (Pro):

    "The system enters a read-only grace window (30 days): existing
    Luciels keep running, but he cannot create new instances or
    upload new knowledge. He gets nudges to either re-upgrade or
    trim down to Free caps. At day 30, the system enforces caps:
    oldest instances over the cap go inactive; oldest knowledge
    sources over the cap are archived (not deleted) until he
    upgrades again."

Three responsibilities:

  1. is_in_grace(admin_id) -> bool
     Read-only predicate the route layer / middleware uses to decide
     whether to allow a write. True iff:
         pending_downgrade_target IS NOT NULL
         AND pending_downgrade_initiated_at IS NOT NULL
         AND pending_downgrade_enforced_at IS NULL
         AND now() < pending_downgrade_initiated_at + 30 days

  2. assert_writable(admin_id) -> None
     Raises DowngradeGraceReadOnlyError if is_in_grace() is True.
     Used by every write-endpoint that's gated during grace
     (new-instance creation, new-knowledge upload, new-embed-key
     minting, new-CNAME, new-seat). The route layer translates
     the exception to HTTP 409 with a clear "read-only during
     downgrade grace until DATE" body.

  3. enforce_at_grace_expiry() -> Iterator[EnforcementResult]
     Celery beat task entry point. Scans subscriptions for rows
     where:
         pending_downgrade_target IS NOT NULL
         AND pending_downgrade_initiated_at < now() - 30 days
         AND pending_downgrade_enforced_at IS NULL
     For each, calls DowngradeArchiveService.archive_overflow_for_admin
     to do the actual cap-trim, then stamps pending_downgrade_enforced_at
     so re-scans skip the row.

The 30-day window matches the closure-grace window by design --
both come from Vision's lifecycle clocks. The constant is sourced
from closure_service.GRACE_WINDOW_DAYS so a future doctrine change
to that number propagates to both flows.

What this service does NOT do:

  * Does NOT initiate the downgrade itself. That's BillingService's
    POST /billing/downgrade -- it stamps pending_downgrade_target
    and pending_downgrade_initiated_at.
  * Does NOT touch knowledge_embeddings directly. The 5th axis on
    DowngradeArchiveService (AXIS_KNOWLEDGE) handles the per-source
    LRU archive at enforcement time.
  * Does NOT block reads. Existing Luciels keep running during
    grace; only writes are gated.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.models.admin_audit_log import (
    ACTION_DOWNGRADE_GRACE_ENFORCED,
    RESOURCE_TENANT,
)
from app.lifecycle.closure import GRACE_WINDOW_DAYS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------

class DowngradeGraceError(Exception):
    """Base for downgrade-grace flow errors."""


class DowngradeGraceReadOnlyError(DowngradeGraceError):
    """Write attempted on an admin currently in downgrade grace.

    Carries grace_expires_at so the route can include the date in
    its error body for the frontend to render.
    """

    def __init__(self, admin_id: str, grace_expires_at: datetime) -> None:
        super().__init__(
            f"Admin {admin_id!r} is in downgrade grace until "
            f"{grace_expires_at.isoformat()}; writes are not allowed."
        )
        self.admin_id = admin_id
        self.grace_expires_at = grace_expires_at


# ---------------------------------------------------------------------
# Result shape for enforcement.
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class EnforcementResult:
    """One per enforced admin in a run."""
    admin_id: str
    target_tier: str
    enforced_at: datetime
    axes_archived: dict[str, int]   # axis name -> rows archived


# ---------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------

class DowngradeGraceService:
    """Read-only middleware predicate + day-30 enforcement.

    Lifetime: one instance per request OR per Celery task invocation.
    downgrade_archive_service is injected so the enforcement path
    composes with the existing 5-axis archive logic.
    """

    def __init__(
        self,
        db: Session,
        *,
        downgrade_archive_service,
        audit_repository,
    ) -> None:
        self.db = db
        self.downgrade_archive_service = downgrade_archive_service
        self.audit_repository = audit_repository

    # -----------------------------------------------------------------
    # Public -- read-only predicate.
    # -----------------------------------------------------------------

    def is_in_grace(self, admin_id: str) -> bool:
        """Return True iff this admin is currently in downgrade grace.

        Single SELECT against the partial index
        ix_subscriptions_downgrade_grace_eligible. Designed to be cheap
        enough to call on every gated write request.
        """
        row = self.db.execute(
            sql_text(
                """
                SELECT pending_downgrade_initiated_at
                  FROM subscriptions
                 WHERE admin_id = :aid
                   AND pending_downgrade_target IS NOT NULL
                   AND pending_downgrade_initiated_at IS NOT NULL
                   AND pending_downgrade_enforced_at IS NULL
                 LIMIT 1
                """
            ),
            {"aid": admin_id},
        ).first()
        if row is None:
            return False
        initiated_at = row[0]
        expires_at = initiated_at + timedelta(days=GRACE_WINDOW_DAYS)
        return datetime.now(timezone.utc) < expires_at

    def grace_expires_at(self, admin_id: str) -> datetime | None:
        """Return the expiry timestamp, or None if not in grace.

        Used by the route layer to surface the date in error bodies.
        """
        row = self.db.execute(
            sql_text(
                """
                SELECT pending_downgrade_initiated_at
                  FROM subscriptions
                 WHERE admin_id = :aid
                   AND pending_downgrade_target IS NOT NULL
                   AND pending_downgrade_initiated_at IS NOT NULL
                   AND pending_downgrade_enforced_at IS NULL
                 LIMIT 1
                """
            ),
            {"aid": admin_id},
        ).first()
        if row is None:
            return None
        return row[0] + timedelta(days=GRACE_WINDOW_DAYS)

    def assert_writable(self, admin_id: str) -> None:
        """Raise if admin_id is in grace. Used by gated write endpoints.

        Pattern at the call site:

            downgrade_grace_service.assert_writable(admin_id)
            ... proceed with the write ...

        The route layer maps the exception to HTTP 409 with the
        grace_expires_at value in the response body.
        """
        if self.is_in_grace(admin_id):
            expires = self.grace_expires_at(admin_id)
            # grace_expires_at can only be non-None when is_in_grace
            # is True; assert for type-narrowing.
            assert expires is not None
            raise DowngradeGraceReadOnlyError(admin_id, expires)

    # -----------------------------------------------------------------
    # Public -- day-30 enforcement worker entry.
    # -----------------------------------------------------------------

    def enforce_at_grace_expiry(self) -> list[EnforcementResult]:
        """Scan subscriptions; for each grace-expired row, archive overflow.

        Predicate:
          pending_downgrade_target IS NOT NULL
          AND pending_downgrade_initiated_at < now() - 30 days
          AND pending_downgrade_enforced_at IS NULL

        Backed by the partial index
        ix_subscriptions_downgrade_grace_eligible.

        For each eligible row:
          1. Call downgrade_archive_service.archive_overflow_for_admin
             with the destination tier. This handles all 5 axes
             (instances, embed keys, CNAMEs, seats, knowledge).
          2. Stamp pending_downgrade_enforced_at = now().
          3. Emit ACTION_DOWNGRADE_GRACE_ENFORCED audit row with
             the per-axis archive counts.

        One bad admin should not block the rest of the run; we catch
        per-admin exceptions and continue.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=GRACE_WINDOW_DAYS
        )
        eligible = self.db.execute(
            sql_text(
                """
                SELECT admin_id, pending_downgrade_target,
                       pending_downgrade_initiated_at
                  FROM subscriptions
                 WHERE pending_downgrade_target IS NOT NULL
                   AND pending_downgrade_initiated_at < :cutoff
                   AND pending_downgrade_enforced_at IS NULL
              ORDER BY pending_downgrade_initiated_at ASC
                """
            ),
            {"cutoff": cutoff},
        ).all()

        logger.info(
            "downgrade_grace: %d admins eligible for enforcement at "
            "cutoff=%s",
            len(eligible), cutoff.isoformat(),
        )

        results: list[EnforcementResult] = []
        for row in eligible:
            admin_id, target_tier, initiated_at = row
            try:
                result = self._enforce_one(
                    admin_id=admin_id,
                    target_tier=target_tier,
                    initiated_at=initiated_at,
                )
                results.append(result)
                self.db.commit()
            except Exception:
                self.db.rollback()
                logger.exception(
                    "downgrade_grace: enforcement failed admin_id=%s "
                    "target_tier=%s",
                    admin_id, target_tier,
                )
        return results

    # -----------------------------------------------------------------
    # Per-admin enforcement.
    # -----------------------------------------------------------------

    def _enforce_one(
        self,
        *,
        admin_id: str,
        target_tier: str,
        initiated_at: datetime,
    ) -> EnforcementResult:
        """Run the archive + stamp + audit for one admin."""
        now = datetime.now(timezone.utc)

        # Run the archive. The downgrade_archive_service is the
        # existing Arc 6 service extended in Arc 10 with the
        # AXIS_KNOWLEDGE 5th axis.
        summary = self.downgrade_archive_service.archive_overflow_for_admin(
            admin_id=admin_id,
            target_tier=target_tier,
            autocommit=False,
        )

        # Stamp enforced_at so re-scans skip this row.
        self.db.execute(
            sql_text(
                """
                UPDATE subscriptions
                   SET pending_downgrade_enforced_at = :ts
                 WHERE admin_id = :aid
                   AND pending_downgrade_target = :tt
                   AND pending_downgrade_enforced_at IS NULL
                """
            ),
            {"ts": now, "aid": admin_id, "tt": target_tier},
        )

        # Audit row -- one per admin enforcement, with per-axis counts.
        per_axis_counts = {
            axis_name: axis.overflow
            for axis_name, axis in summary.axes.items()
        }
        from app.repositories.admin_audit_repository import AuditContext
        sys_ctx = AuditContext.system(label="downgrade_grace_worker")
        self.audit_repository.record(
            ctx=sys_ctx,
            admin_id=admin_id,
            action=ACTION_DOWNGRADE_GRACE_ENFORCED,
            resource_type=RESOURCE_TENANT,
            resource_natural_id=admin_id,
            before={
                "pending_downgrade_target": target_tier,
                "pending_downgrade_initiated_at": initiated_at.isoformat(),
            },
            after={
                "enforced_at": now.isoformat(),
                "total_overflow_archived": summary.total_overflow,
                "axes_archived": per_axis_counts,
                "grace_window_days": GRACE_WINDOW_DAYS,
            },
            note=(
                f"Downgrade grace enforced for {admin_id} -> {target_tier} "
                f"after {GRACE_WINDOW_DAYS}d grace; "
                f"{summary.total_overflow} rows archived across "
                f"{len([a for a in per_axis_counts.values() if a > 0])} axes."
            ),
            autocommit=False,
        )

        return EnforcementResult(
            admin_id=admin_id,
            target_tier=target_tier,
            enforced_at=now,
            axes_archived=per_axis_counts,
        )
