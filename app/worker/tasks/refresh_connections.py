"""Connection token-refresh + secret-cleanup workers — Arc 17 Task 5c.

Two nightly Celery tasks, both running under the BYPASSRLS ops role
(``OpsSessionLocal``) so a single cross-tenant sweep can process every
admin's rows without binding ``app.admin_id`` — same posture as
``app.lifecycle.retention``.

1. ``run_connection_token_refresh``
   Re-verifies ALL live (non-revoked) connections via
   :class:`app.services.connection_health_service.ConnectionHealthService`,
   which dispatches on the row's §3.8.5 ``auth_class``:
     * ``api_key`` / ``long_lived_token`` (record_source / outbound_webhook)
       → config-presence liveness probe → connected / error.
     * ``provisioned_resource`` (email_sender / sms_sender) → sender-identity
       presence probe → connected / error.
     * ``oauth_token`` (calendar / crm) → silent token refresh. DEPLOY-GATED
       on OAuth client creds + a stored refresh token; absent them the row
       stays an honest ``unconfigured`` (+ arc17_pending) and is SKIPPED
       (no status write, no audit) so the sweep does not churn rows it
       cannot honestly change. A real refresh → connected (+ rotated
       secret_ref); a rejected token → expired (+ notify_admin marker).
   Each honest check writes one ``ACTION_CONNECTION_TOKEN_REFRESHED`` audit
   row (system actor); a status TRANSITION additionally writes a
   ``ACTION_CONNECTION_STATUS_CHANGED`` row carrying the notify_admin
   marker — both in the same per-row transaction.

2. ``run_secret_cleanup_drain``
   Drains ``secret_cleanup_outbox`` (rows enqueued by the lifecycle
   cascade when a connection with a non-null ``secret_ref`` was
   revoked). For each pending row it calls ``SecretStore.delete`` on the
   POINTER (never a value) and marks the row done; a failure is retried
   on the next sweep until ``max_attempts``. The AWS deletion itself is
   DEPLOY-GATED behind ``connections_live_secrets_enabled`` (the factory
   returns the local fake otherwise), so this worker is exercised in
   tests with ``LocalFakeSecretStore`` and never touches AWS without
   real creds.

Honesty invariant
-----------------
Neither task ever fabricates ``connected``. The token-refresh task only
writes ``connected`` when ``ConnectionHealthService`` returned it off a
real token exchange. The drain task only ever sees the secret POINTER.
"""
from __future__ import annotations

import logging
import traceback
from typing import TYPE_CHECKING

from celery import shared_task
from sqlalchemy import select

from app.core.config import settings
from app.db.session import OpsSessionLocal
from app.integrations.secrets import SecretStoreError, get_secret_store
from app.models.admin_audit_log import (
    ACTION_CONNECTION_STATUS_CHANGED,
    ACTION_CONNECTION_TOKEN_REFRESHED,
    RESOURCE_INSTANCE_CONNECTION,
)
from app.connections.instance_connection import InstanceConnection
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)
from app.connections.repository import (
    InstanceConnectionRepository,
)
from app.connections.secret_cleanup_outbox_repository import (
    SecretCleanupOutboxRepository,
)
from app.services.connection_health_service import ConnectionHealthService

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)


# =====================================================================
# Task 1 — OAuth token refresh.
# =====================================================================


@shared_task(
    bind=True,
    name="app.worker.tasks.refresh_connections.run_connection_token_refresh",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=10,
    retry_jitter=True,
    max_retries=3,
)
def run_connection_token_refresh(self):
    """Nightly: silently refresh OAuth connection tokens.

    Returns a dict summary:
        {
            "scanned_count": int,
            "refreshed_count": int,   # → connected
            "expired_count": int,     # → expired (reconnect needed)
            "skipped_count": int,     # deploy-gated / unconfigured (no-op)
            "errored_count": int,
            "errored_connection_ids": list[int],
        }
    """
    if OpsSessionLocal is None:
        _log.error(
            "connection_token_refresh ABORTED: OpsSessionLocal is None. "
            "settings.luciel_ops_db_url must be configured."
        )
        return {
            "scanned_count": 0,
            "refreshed_count": 0,
            "expired_count": 0,
            "skipped_count": 0,
            "errored_count": 0,
            "errored_connection_ids": [],
            "aborted": "ops_session_unavailable",
        }

    scan_db: "Session" = OpsSessionLocal()
    try:
        eligible_ids = _scan_due_connection_ids(scan_db)
    finally:
        scan_db.close()

    _log.info(
        "connection_token_refresh scan: %d live connection(s) eligible",
        len(eligible_ids),
    )

    health = ConnectionHealthService(settings)
    refreshed = expired = skipped = errored = 0
    errored_ids: list[int] = []

    for connection_id in eligible_ids:
        db: "Session" = OpsSessionLocal()
        try:
            outcome = _refresh_one(db, connection_id=connection_id, health=health)
            db.commit()
            if outcome == "connected":
                refreshed += 1
            elif outcome == "expired":
                expired += 1
            else:
                skipped += 1
        except Exception:
            db.rollback()
            errored += 1
            errored_ids.append(connection_id)
            _log.error(
                "connection_token_refresh FAILED connection_id=%s:\n%s",
                connection_id,
                traceback.format_exc(),
            )
        finally:
            db.close()

    summary = {
        "scanned_count": len(eligible_ids),
        "refreshed_count": refreshed,
        "expired_count": expired,
        "skipped_count": skipped,
        "errored_count": errored,
        "errored_connection_ids": errored_ids,
    }
    _log.info("connection_token_refresh complete: %s", summary)
    return summary


def _scan_due_connection_ids(db: "Session") -> list[int]:
    """Return ids of ALL live (non-revoked) connection rows (§3.8.5).

    Every auth_class is swept: the health service decides per-row whether
    a check is honest (oauth refresh / config-presence liveness) or a
    deploy-gated no-op (``checked_at=None`` → skipped). api_key /
    provisioned_resource rows liveness-probe to confirm config presence;
    oauth_token rows attempt a real refresh. Ordered by id for
    deterministic FIFO processing across interrupted runs.
    """
    stmt = (
        select(InstanceConnection.id)
        .where(InstanceConnection.revoked_at.is_(None))
        .order_by(InstanceConnection.id.asc())
    )
    return [row[0] for row in db.execute(stmt)]


def _refresh_one(
    db: "Session",
    *,
    connection_id: int,
    health: ConnectionHealthService,
) -> str:
    """Refresh a single connection in its own transaction.

    Returns the resulting disposition: ``"connected"``, ``"expired"``,
    or ``"skipped"`` (deploy-gated/unconfigured no-op — no status write,
    no audit). Never fabricates connected.
    """
    row = db.get(InstanceConnection, connection_id)
    if row is None or row.revoked_at is not None:
        return "skipped"

    result = health.check_health(row)

    # Deploy-gated / unconfigured no-op: the service returns
    # checked_at=None when it could not honestly verify (OAuth client
    # not configured, or no stored refresh token). Skip — do not churn
    # the row or write a misleading audit line.
    if result.checked_at is None:
        _log.debug(
            "connection_token_refresh skip connection_id=%s: %s",
            connection_id,
            result.detail,
        )
        return "skipped"

    prior_status = row.status

    repo = InstanceConnectionRepository(db)
    # Populate status_detail on the expired path (§3.8.5 / CJ §7 Reconnect chip).
    # HealthCheckResult.detail carries the human-readable message from the
    # health service; pass it through to apply_health_check which writes it
    # to status_detail when status='expired' (and clears it on connected).
    repo.apply_health_check(
        row=row,
        status=result.status,
        last_health_check_at=result.checked_at,
        secret_ref=result.new_secret_ref,
        status_detail=result.detail if result.status == "expired" else None,
        autocommit=False,
    )

    audit = AdminAuditRepository(db)
    audit.record(
        ctx=AuditContext.system(label="connection_token_refresh"),
        admin_id=row.admin_id,
        action=ACTION_CONNECTION_TOKEN_REFRESHED,
        resource_type=RESOURCE_INSTANCE_CONNECTION,
        resource_pk=row.id,
        resource_natural_id=f"{row.instance_id}:{row.connection_type}",
        luciel_instance_id=row.instance_id,
        after={
            "connection_type": row.connection_type,
            "status": result.status,
            "credential_rotated": result.new_secret_ref is not None,
        },
        note=f"Token refresh worker ({row.connection_type}={result.status}).",
        autocommit=False,
    )

    # §3.8.5: on an honest status transition emit a second
    # connection_status_changed audit (old→new). notify_admin rides in
    # after_json on a refresh-fail→expired transition — the documented
    # admin-reconnect seam (no new delivery channel here).
    if result.status != prior_status:
        audit.record(
            ctx=AuditContext.system(label="connection_token_refresh"),
            admin_id=row.admin_id,
            action=ACTION_CONNECTION_STATUS_CHANGED,
            resource_type=RESOURCE_INSTANCE_CONNECTION,
            resource_pk=row.id,
            resource_natural_id=f"{row.instance_id}:{row.connection_type}",
            luciel_instance_id=row.instance_id,
            before={"status": prior_status},
            after={
                "connection_type": row.connection_type,
                "auth_class": getattr(row, "auth_class", None),
                "status": result.status,
                "notify_admin": result.notify_admin,
            },
            note=(
                f"Connection status {prior_status}→{result.status} "
                f"({row.connection_type})."
            ),
            autocommit=False,
        )

    return "connected" if result.status == "connected" else "expired"


# =====================================================================
# Task 2 — secret-cleanup outbox drain.
# =====================================================================


@shared_task(
    bind=True,
    name="app.worker.tasks.refresh_connections.run_secret_cleanup_drain",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=10,
    retry_jitter=True,
    max_retries=3,
)
def run_secret_cleanup_drain(self):
    """Nightly: delete revoked connections' secrets from the store.

    Drains ``secret_cleanup_outbox``. For each pending row, deletes the
    secret POINTER from the store (AWS deletion DEPLOY-GATED behind
    ``connections_live_secrets_enabled``; local fake otherwise) and marks
    the row done. A failure increments ``attempts`` and is retried until
    ``max_attempts``, after which the row flips to ``failed`` for operator
    triage. Never reads or logs a secret value.
    """
    if OpsSessionLocal is None:
        _log.error(
            "secret_cleanup_drain ABORTED: OpsSessionLocal is None."
        )
        return {
            "scanned_count": 0,
            "deleted_count": 0,
            "errored_count": 0,
            "aborted": "ops_session_unavailable",
        }

    store = get_secret_store(settings)
    db: "Session" = OpsSessionLocal()
    deleted = errored = 0
    try:
        repo = SecretCleanupOutboxRepository(db)
        pending = repo.list_pending(limit=500)
        for outbox_row in pending:
            try:
                # DEPLOY-GATED: real AWS Secrets Manager deletion runs only
                # when connections_live_secrets_enabled selects the AWS
                # store; otherwise the local fake performs the delete. The
                # argument is the secret NAME/ARN pointer — never a value.
                store.delete(outbox_row.secret_ref)
                repo.mark_done(row=outbox_row, autocommit=False)
                deleted += 1
            except SecretStoreError as exc:
                repo.mark_failed(
                    row=outbox_row, error=str(exc), autocommit=False
                )
                errored += 1
                _log.warning(
                    "secret_cleanup_drain delete failed outbox_id=%s "
                    "attempts=%s: %s",
                    outbox_row.id,
                    outbox_row.attempts,
                    exc,
                )
        db.commit()
    except Exception:
        db.rollback()
        _log.error(
            "secret_cleanup_drain FAILED:\n%s", traceback.format_exc()
        )
        raise
    finally:
        db.close()

    summary = {
        "scanned_count": deleted + errored,
        "deleted_count": deleted,
        "errored_count": errored,
    }
    _log.info("secret_cleanup_drain complete: %s", summary)
    return summary
