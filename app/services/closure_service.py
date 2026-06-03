"""Closure service — Arc 10.

Owns the POST /api/v1/admin/account/close path. Per Vision §6.3 and
Architecture §3.6.2, account closure is the customer-initiated action
that starts the 30-day grace clock and (eventually) the hard-delete
cascade.

This service does the small set of things closure must do *atomically*
inside the request transaction:

  1. Validate the confirmation token (the admin must type their own
     account name to proceed; the route layer normalizes the input
     and passes the validated string in).
  2. Stamp ``admins.closure_initiated_at = now()`` and
     ``admins.closure_cancel_mode = <mode>``. This starts the
     30-day grace clock.
  3. Mark the admin row inactive so the cascade and the runtime
     gates honor the closed state immediately. (Vision §6.3:
     "All instances deactivated. All team members invalidated. All
     embed keys revoked." \u2014 the deactivation cascade runs via the
     existing AdminService.deactivate_tenant_with_cascade.)
  4. Call into the existing AdminService.deactivate_tenant_with_cascade
     to run the 12-layer leaf-first soft-delete. Reuses Arc 5 B5
     work; no duplication.
  5. Tell Stripe to cancel the subscription \u2014 immediate or
     end-of-period per the admin's choice. The existing
     BillingService primitives (Arc 6) own the Stripe call shape;
     this service composes with them. Stripe's webhook then fires
     ``customer.subscription.deleted``, which is idempotent against
     the local state we've already set (Arc 6 Commit 8.5b webhook
     handler tolerates the "already cancelled locally" path).
  6. Optionally enqueue a pre-closure data export via
     DataExportService. If requested, the export job is created
     with ``triggered_by='admin_request'`` and returned to the
     caller for status polling.
  7. Emit an audit row with ``ACTION_ACCOUNT_CLOSURE_INITIATED``
     including the cancel_mode, the chosen export behavior, and
     the next-action timestamps so forensics has the full closure
     picture from one row.

What this service does NOT do:

  * Does not run hard-delete. That's the retention worker's job,
    30 days from now, in a separate transaction.
  * Does not write to admin_audit_log directly \u2014 it uses the
    AdminAuditRepository so the chain-of-custody hashes land
    correctly via the install_audit_chain_event hook.
  * Does not handle the closure modal's UI \u2014 that's the route
    layer + frontend.
  * Does not handle reactivation \u2014 that's ReactivationService.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy.orm import Session

from app.models.admin import Admin
from app.models.admin_audit_log import (
    ACTION_ACCOUNT_CLOSURE_INITIATED,
    RESOURCE_TENANT,
)

logger = logging.getLogger(__name__)


CancelMode = Literal["immediate", "period_end"]


# ---------------------------------------------------------------------
# Errors \u2014 typed so the route layer can map them to clear HTTP codes.
# ---------------------------------------------------------------------

class AccountClosureError(Exception):
    """Base for closure-flow errors."""


class AccountNotFoundError(AccountClosureError):
    """Arc 10 re-open Gap 1: the admin_id passed to get_lifecycle_state
    does not resolve.

    Distinct from AccountAlreadyTombstoneError -- a tombstoned admin's
    row still exists (Vision 6.5 'minimal compliance record retained')
    and get_lifecycle_state returns it with hard_deleted=True. This
    error is raised only when the row truly does not exist, which in
    practice means the cookie carries a stale admin_id that has been
    removed by some other path. The /admin/account/lifecycle-state
    route maps this to HTTP 404.
    """


class AccountAlreadyClosedError(AccountClosureError):
    """Admin row already carries closure_initiated_at."""


class AccountAlreadyTombstoneError(AccountClosureError):
    """Admin row already hard-deleted; nothing to close."""


class InvalidConfirmationError(AccountClosureError):
    """The typed confirmation string did not match the admin's name.

    Route layer maps to HTTP 400 with a deliberate generic message
    (no enumeration of which field was wrong) so a probe cannot use
    this endpoint to enumerate account names.
    """


class InvalidCancelModeError(AccountClosureError):
    """cancel_mode is not 'immediate' or 'period_end'."""


# ---------------------------------------------------------------------
# Result shape \u2014 what the route returns to the frontend.
# ---------------------------------------------------------------------

@dataclass
class LifecycleState:
    """In-memory shape of the get_lifecycle_state return.

    Maps 1:1 to app.schemas.lifecycle.LifecycleStateResponse so the
    route can pass straight through. Kept as a dataclass (not a
    Pydantic model) so the service layer stays Pydantic-free.
    """

    admin_id: str
    closed: bool
    in_grace: bool
    hard_deleted: bool
    cancel_mode: Literal["immediate", "period_end"] | None
    closure_initiated_at: datetime | None
    grace_window_expires_at: datetime | None
    hard_deleted_at: datetime | None


@dataclass(frozen=True)
class ClosureOutcome:
    """Returned from initiate_closure().

    admin_id: the admin that was closed (echoed for the audit trail
        the frontend renders in its receipt UI).
    closure_initiated_at: the grace clock anchor. The frontend
        renders "Your account will be permanently deleted on
        <closure_initiated_at + 30 days>." against this.
    grace_window_expires_at: convenience \u2014 closure_initiated_at + 30d.
    cancel_mode: echoed for the receipt.
    stripe_cancellation_applied: True iff a Stripe modification was
        actually issued. False on Free admins (no subscription) or
        when the admin had already cancelled at Stripe by some other
        path.
    data_export_job_id: present iff the closure modal requested an
        export. The frontend polls /admin/account/export/{job_id}
        for status.
    """
    admin_id: str
    closure_initiated_at: datetime
    grace_window_expires_at: datetime
    cancel_mode: CancelMode
    stripe_cancellation_applied: bool
    data_export_job_id: str | None


# ---------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------

# Vision \u00a76.3 lock \u2014 30-day grace. Sourced as a module constant so the
# frontend and the receipt copy can import it; do NOT duplicate this
# value elsewhere.
GRACE_WINDOW_DAYS = 30


# ---------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------

class ClosureService:
    """Orchestrates account closure.

    Lifetime: one instance per HTTP request. Holds references to the
    services it composes with (AdminService for the cascade,
    BillingService for the Stripe call, DataExportService for the
    optional pre-closure bundle). Those services are injected so unit
    tests can stub them.
    """

    def __init__(
        self,
        db: Session,
        *,
        admin_service,
        billing_service,
        data_export_service,
        audit_repository,
    ) -> None:
        self.db = db
        self.admin_service = admin_service
        self.billing_service = billing_service
        self.data_export_service = data_export_service
        self.audit_repository = audit_repository

    # -----------------------------------------------------------------
    # Public \u2014 the closure entry point.
    # -----------------------------------------------------------------

    def initiate_closure(
        self,
        *,
        admin_id: str,
        cancel_mode: str,
        confirm_account_name: str,
        request_export: bool,
        audit_ctx,
        luciel_instance_service,
        agent_repo,
    ) -> ClosureOutcome:
        """Run the full closure flow for ``admin_id``.

        All steps run inside the caller's DB transaction except the
        Stripe call (which is on the external side) and the export
        enqueue (which creates a row but the Celery worker runs the
        actual bundle generation asynchronously).

        Idempotency:
          * If closure_initiated_at is already set, raises
            AccountAlreadyClosedError. The route maps this to HTTP
            409 so the frontend can show "you've already closed
            this account; the grace window expires on \u2026" without
            silently re-running the cascade.
          * If hard_deleted_at is already set, raises
            AccountAlreadyTombstoneError. The route maps to HTTP
            410 because the account is gone.

        Failure isolation:
          * Cascade failure rolls back closure_initiated_at + the
            cascade in the same transaction (Invariant 4).
          * Stripe failure does NOT roll back the local state \u2014 the
            local closure is the source of truth for "this admin
            asked to close." If Stripe is unreachable, the webhook
            will fire later, idempotently. The audit row records
            stripe_cancellation_applied=False so forensics can
            spot it.
          * Export-enqueue failure is logged but does NOT block the
            closure. The admin can request the export separately
            from the dashboard during the grace window.
        """
        _validate_cancel_mode(cancel_mode)

        # --- Step 1: Load the admin row and validate state. ---
        admin: Admin | None = self.db.get(Admin, admin_id)
        if admin is None:
            raise AccountClosureError(
                f"Admin {admin_id!r} not found"
            )
        if admin.hard_deleted_at is not None:
            raise AccountAlreadyTombstoneError(
                f"Admin {admin_id!r} is already hard-deleted; "
                f"closure has no effect."
            )
        if admin.closure_initiated_at is not None:
            raise AccountAlreadyClosedError(
                f"Admin {admin_id!r} is already closed "
                f"(initiated at {admin.closure_initiated_at.isoformat()})."
            )

        # --- Step 2: Validate the typed confirmation. ---
        # The admin must type their account name exactly. We compare
        # normalized strings (strip + casefold) so trivial whitespace
        # or case differences do not block a real closure, but the
        # underlying name must match. Empty input is rejected
        # categorically so a missing field never accidentally passes.
        expected = (admin.name or "").strip().casefold()
        provided = (confirm_account_name or "").strip().casefold()
        if not provided or provided != expected:
            raise InvalidConfirmationError(
                "Account-name confirmation did not match."
            )

        # --- Step 3: Stamp the closure clock + cancel mode. ---
        # This is the single load-bearing write: from here onward,
        # the retention worker will count down 30 days against this
        # timestamp. We stamp BEFORE the cascade so a cascade failure
        # rolls back the closure intent with it (Invariant 4).
        now = datetime.now(timezone.utc)
        admin.closure_initiated_at = now
        admin.closure_cancel_mode = cancel_mode
        admin.active = False
        # admin.deactivated_at is also stamped here so the legacy
        # cascade-layer queries (which still read deactivated_at on
        # admins) see the row as deactivated in the same instant.
        admin.deactivated_at = now
        self.db.flush()

        # --- Step 4: Run the soft-delete cascade. ---
        # AdminService.deactivate_tenant_with_cascade handles the
        # 12-layer soft-delete (sessions, conversations, identity_
        # claims, memory_items, api_keys, instances, etc.). Audit
        # rows for each layer are emitted by that service.
        #
        # autocommit=False because this is happening inside the
        # closure transaction; the route layer commits the whole
        # transaction at the end.
        self.admin_service.deactivate_tenant_with_cascade(
            admin_id=admin_id,
            audit_ctx=audit_ctx,
            luciel_instance_service=luciel_instance_service,
            agent_repo=agent_repo,
            autocommit=False,
        )

        # --- Step 4b: Arc 17 connection cascade. ---
        # Account closure is destructive intent, so every external-system
        # connection across all of the admin's instances is revoked here,
        # and secret cleanup is enqueued for any non-null credential_ref.
        # Runs in the SAME transaction as the cascade above (autocommit
        # is the route's job) so a rollback un-revokes atomically. The
        # repo audits each revocation (ACTION_CONNECTION_REVOKED). Imported
        # lazily to keep the service's import graph thin.
        from app.repositories.instance_repository import InstanceRepository

        revoked_connections = InstanceRepository(
            self.db
        ).cascade_revoke_connections_for_admin(
            admin_id=admin_id,
            audit_ctx=audit_ctx,
        )

        # --- Step 5: Stripe cancel \u2014 best-effort. ---
        # The local closure is the source of truth; Stripe is the
        # billing-system mirror. If Stripe fails, the local state
        # stands and the webhook (when it eventually fires) is
        # idempotent against the already-closed admin row.
        stripe_applied = False
        try:
            stripe_applied = self.billing_service.cancel_for_closure(
                admin_id=admin_id,
                cancel_mode=cancel_mode,
                audit_ctx=audit_ctx,
            )
        except Exception as exc:
            # Log + continue. Forensics gets the partial state via
            # the audit row (stripe_cancellation_applied=False).
            logger.error(
                "closure_service: Stripe cancel failed admin_id=%s "
                "cancel_mode=%s err=%s",
                admin_id, cancel_mode, exc,
            )

        # --- Step 6: Optionally enqueue the pre-closure export. ---
        export_job_id: str | None = None
        if request_export:
            try:
                export_job = self.data_export_service.enqueue(
                    admin_id=admin_id,
                    triggered_by="admin_request",
                    tier_at_request=admin.tier,
                    audit_ctx=audit_ctx,
                )
                export_job_id = str(export_job.id)
            except Exception as exc:
                # Same posture: don't block the closure on the
                # export-enqueue failure. The admin can re-request
                # the export from the dashboard during grace.
                logger.error(
                    "closure_service: data export enqueue failed "
                    "admin_id=%s err=%s",
                    admin_id, exc,
                )

        # --- Step 7: Audit row \u2014 the single durable record. ---
        self.audit_repository.record(
            ctx=audit_ctx,
            admin_id=admin_id,
            action=ACTION_ACCOUNT_CLOSURE_INITIATED,
            resource_type=RESOURCE_TENANT,
            resource_natural_id=admin_id,
            before={
                "active": True,
                "closure_initiated_at": None,
            },
            after={
                "active": False,
                "closure_initiated_at": now.isoformat(),
                "closure_cancel_mode": cancel_mode,
                "stripe_cancellation_applied": stripe_applied,
                "data_export_requested": request_export,
                "data_export_job_id": export_job_id,
                "connections_revoked": revoked_connections,
                "grace_window_days": GRACE_WINDOW_DAYS,
                "hard_delete_eligible_at": (
                    _add_days(now, GRACE_WINDOW_DAYS).isoformat()
                ),
            },
            note=(
                f"Account closure initiated for {admin_id}; "
                f"30-day grace begins."
            ),
            autocommit=False,
        )

        return ClosureOutcome(
            admin_id=admin_id,
            closure_initiated_at=now,
            grace_window_expires_at=_add_days(now, GRACE_WINDOW_DAYS),
            cancel_mode=cancel_mode,  # type: ignore[arg-type]
            stripe_cancellation_applied=stripe_applied,
            data_export_job_id=export_job_id,
        )

    # -----------------------------------------------------------------
    # Public -- read-only lifecycle state (Arc 10 re-open Gap 1).
    # -----------------------------------------------------------------

    def get_lifecycle_state(self, admin_id: str) -> "LifecycleState":
        """Return the authoritative lifecycle state for ``admin_id``.

        Computed entirely from the admins row plus the
        ``GRACE_WINDOW_DAYS`` module constant; no Stripe round-trip,
        no Celery touch. Cheap enough to call on every page load.

        Returns a populated LifecycleState even for admins that have
        never been closed -- in that case ``closed=False, in_grace=
        False, hard_deleted=False`` and the four datetime fields are
        None.

        Raises ``AccountNotFoundError`` if ``admin_id`` does not
        resolve. The route maps this to HTTP 404. A 401
        ('unauthenticated') is the route's responsibility, not ours.
        """
        admin = (
            self.db.query(Admin)
            .filter(Admin.id == admin_id)
            .first()
        )
        if admin is None:
            raise AccountNotFoundError(
                f"admin {admin_id!r} not found"
            )

        closure_initiated_at = admin.closure_initiated_at
        hard_deleted_at = admin.hard_deleted_at
        cancel_mode = admin.closure_cancel_mode

        closed = closure_initiated_at is not None
        hard_deleted = hard_deleted_at is not None

        grace_window_expires_at: datetime | None = None
        in_grace = False
        if closure_initiated_at is not None:
            grace_window_expires_at = _add_days(
                closure_initiated_at, GRACE_WINDOW_DAYS
            )
            now = datetime.now(timezone.utc)
            in_grace = (
                not hard_deleted
                and now < grace_window_expires_at
            )

        return LifecycleState(
            admin_id=admin_id,
            closed=closed,
            in_grace=in_grace,
            hard_deleted=hard_deleted,
            cancel_mode=cancel_mode,  # type: ignore[arg-type]
            closure_initiated_at=closure_initiated_at,
            grace_window_expires_at=grace_window_expires_at,
            hard_deleted_at=hard_deleted_at,
        )


# ---------------------------------------------------------------------
# Module helpers.
# ---------------------------------------------------------------------

_VALID_CANCEL_MODES: tuple[str, ...] = ("immediate", "period_end")


def _validate_cancel_mode(cancel_mode: str) -> None:
    """Raise InvalidCancelModeError if cancel_mode is not legal.

    Matches the CHECK constraint on admins.closure_cancel_mode in the
    Arc 10 migration so a malformed input fails at the service layer
    rather than at the DB layer (clearer error for the route).
    """
    if cancel_mode not in _VALID_CANCEL_MODES:
        raise InvalidCancelModeError(
            f"cancel_mode {cancel_mode!r} is not legal; "
            f"expected one of {_VALID_CANCEL_MODES}."
        )


def _add_days(ts: datetime, days: int) -> datetime:
    """Compute ts + N days. Inlined to avoid a dependency on dateutil."""
    from datetime import timedelta
    return ts + timedelta(days=days)
