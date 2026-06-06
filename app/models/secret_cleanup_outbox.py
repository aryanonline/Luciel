"""SecretCleanupOutbox ORM — Arc 17 (lifecycle secret-cleanup seam).

A transactional outbox: when a connection carrying a non-null
``secret_ref`` is revoked (instance delete / account closure), the
lifecycle cascade INSERTs one row here IN THE SAME TRANSACTION as the
revocation. A Celery worker later drains the outbox and deletes the
secret from the secret store.

Why an outbox (not a direct ``SecretStore.delete()`` in the request)
--------------------------------------------------------------------
The revocation must commit atomically with the audit row even when the
secret store (AWS Secrets Manager) is unreachable. A direct delete on
the request path would couple the lifecycle write to an external
network call. The outbox decouples them: the revocation always commits;
the secret deletion is retried by the worker until it succeeds.

Honesty / security invariant
-----------------------------
This row stores ONLY the ``secret_ref`` (the secret NAME/ARN
pointer) — NEVER the secret VALUE (Locked Decision #18). The worker
resolves the pointer to a value only transiently inside the store's
``delete`` call; nothing here or in the audit trail ever holds a value.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Drain lifecycle. ``pending`` rows are claimed by the worker; a
# successful store delete → ``done``; an exhausted-retry failure →
# ``failed`` (left for an operator to inspect — the pointer is inert).
OUTBOX_STATUSES = ("pending", "done", "failed")


class SecretCleanupOutbox(Base):
    __tablename__ = "secret_cleanup_outbox"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    # NOT an FK to instance_connections: the connection row may be
    # hard-purged by the retention worker before this outbox row is
    # drained, and the cleanup must still proceed. We keep the ids as
    # plain columns for forensics, not as enforced references.
    admin_id: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    instance_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True
    )
    connection_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    # The secret NAME/ARN pointer — NEVER the value (Locked Decision #18).
    secret_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending", index=True
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<SecretCleanupOutbox id={self.id} admin={self.admin_id} "
            f"ref={self.secret_ref} status={self.status} "
            f"attempts={self.attempts}>"
        )
