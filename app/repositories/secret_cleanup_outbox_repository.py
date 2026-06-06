"""SecretCleanupOutbox repository — Arc 17 lifecycle secret-cleanup.

Pure CRUD against the ``secret_cleanup_outbox`` table. The lifecycle
cascade ENQUEUEs (one row per revoked connection with a non-null
``secret_ref``); the Celery drain worker CLAIMS pending rows and
marks them done/failed.

No policy decisions, no HTTP exceptions. The enqueue writes ONLY the
secret pointer (``secret_ref``) — never a secret value.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.secret_cleanup_outbox import SecretCleanupOutbox

logger = logging.getLogger(__name__)


class SecretCleanupOutboxRepository:
    """Data-access for the transactional secret-cleanup outbox."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def enqueue(
        self,
        *,
        admin_id: str,
        secret_ref: str,
        instance_id: Optional[int] = None,
        connection_id: Optional[int] = None,
        autocommit: bool = False,
    ) -> SecretCleanupOutbox:
        """Insert one pending cleanup row.

        Defaults to ``autocommit=False`` so the enqueue rides the
        lifecycle cascade's transaction — the secret pointer is recorded
        atomically with the connection revocation + its audit row.
        """
        row = SecretCleanupOutbox(
            admin_id=admin_id,
            instance_id=instance_id,
            connection_id=connection_id,
            secret_ref=secret_ref,
            status="pending",
            attempts=0,
        )
        self.db.add(row)
        if autocommit:
            self.db.commit()
            self.db.refresh(row)
        else:
            self.db.flush()
        return row

    def list_pending(self, *, limit: int = 100) -> list[SecretCleanupOutbox]:
        """Return pending rows oldest-first (drain order)."""
        stmt = (
            select(SecretCleanupOutbox)
            .where(SecretCleanupOutbox.status == "pending")
            .order_by(SecretCleanupOutbox.enqueued_at.asc())
            .limit(limit)
        )
        return list(self.db.execute(stmt).scalars())

    def mark_done(
        self, *, row: SecretCleanupOutbox, autocommit: bool = False
    ) -> SecretCleanupOutbox:
        row.status = "done"
        row.attempts += 1
        row.processed_at = datetime.now(timezone.utc)
        row.last_error = None
        self.db.add(row)
        if autocommit:
            self.db.commit()
        else:
            self.db.flush()
        return row

    def mark_failed(
        self,
        *,
        row: SecretCleanupOutbox,
        error: str,
        max_attempts: int = 5,
        autocommit: bool = False,
    ) -> SecretCleanupOutbox:
        """Record a failed drain attempt.

        Stays ``pending`` (retried next sweep) until ``attempts`` reaches
        ``max_attempts``, then flips to ``failed`` for operator triage.
        The pointer is inert, so a stuck row leaks no secret value.
        """
        row.attempts += 1
        row.last_error = error[:1000]
        if row.attempts >= max_attempts:
            row.status = "failed"
            row.processed_at = datetime.now(timezone.utc)
        self.db.add(row)
        if autocommit:
            self.db.commit()
        else:
            self.db.flush()
        return row
