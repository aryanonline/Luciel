"""Reactivation service \u2014 Arc 10.

Owns the POST /api/v1/admin/account/reactivate path. Per Vision \u00a76.4:

    "Account: Log in within 30 days \u2192 resubscribe \u2192 full restore."

Reactivation is the inverse of closure. The 30-day grace window
between ``closure_initiated_at`` and ``closure_initiated_at + 30d``
is the customer's "I changed my mind" window. After day 30 the
retention worker fires hard-delete and reactivation is no longer
possible.

What reactivation restores:
  * admins.active                = true
  * admins.closure_initiated_at  = NULL
  * admins.closure_cancel_mode   = NULL
  * admins.deactivated_at        = NULL (the soft-delete cascade
    timestamp also gets cleared so the cascade undo logic does not
    re-fire the soft-delete on the same row)
  * Every child row the cascade soft-deleted with active=false +
    deactivated_at set gets active=true + deactivated_at=NULL via
    the inverse cascade. This means: instances come back, embed
    keys stay revoked (Vision \u00a76.4: "embed keys re-minted (new
    keys, old keys stay revoked)"), team members are re-invited
    rather than restored (Vision \u00a76.4: "Team member: Re-invite
    (treated as new user; old data not auto-restored)").

What reactivation does NOT restore:
  * Stripe subscription is NOT auto-resurrected. Vision \u00a76.4
    explicitly says "resubscribe" \u2014 i.e. the admin must complete
    a fresh Stripe checkout. The reactivation route returns a
    checkout-session URL and only finalizes restoration on the
    successful webhook callback. This service supports that
    two-phase flow: ``stage_reactivation()`` validates eligibility
    and prepares state; ``complete_reactivation()`` runs after
    Stripe confirms the new subscription.
  * Embed keys are NOT un-revoked. Once revoked, always revoked
    (Vision \u00a76.4). The admin mints new keys after reactivation.
  * Sessions are NOT restored. Whatever browser tokens existed
    before closure are gone; the admin must log in fresh.
  * Downgrade-archived rows (those with
    ``pending_downgrade_archived_at`` set) are NOT rehydrated by
    closure-reactivation. Per the founder lock A6 from the Arc 10
    opening thread: closure-reactivation restores cascade-soft-
    deleted rows but leaves pending_downgrade_archived_at rows
    alone. Only a fresh re-upgrade rehydrates those.

What reactivation cannot do post day-30:
  * If now > closure_initiated_at + 30d, raises
    ReactivationWindowExpiredError. Route maps to HTTP 410 Gone
    because the admin's data has either been or will be hard-
    deleted by the retention worker; there is nothing to come
    back to.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.models.admin import Admin
from app.models.admin_audit_log import (
    ACTION_ACCOUNT_REACTIVATED,
    RESOURCE_TENANT,
)
from app.services.closure_service import GRACE_WINDOW_DAYS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------

class ReactivationError(Exception):
    """Base for reactivation-flow errors."""


class AccountNotInGraceError(ReactivationError):
    """Admin row has no closure_initiated_at \u2014 not in a grace state."""


class ReactivationWindowExpiredError(ReactivationError):
    """closure_initiated_at + 30d < now. Hard-delete is imminent or done."""


class AccountAlreadyTombstoneError(ReactivationError):
    """Admin row carries hard_deleted_at. Reactivation impossible."""


class StripeReactivationCheckoutFailedError(ReactivationError):
    """Stripe rejected the new checkout session at stage_reactivation."""


class StripeSubscriptionMismatchError(ReactivationError):
    """The Stripe session id presented at complete_reactivation does

    not belong to this admin, or its checkout has not completed.
    """


# ---------------------------------------------------------------------
# Result shapes.
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class ReactivationStaged:
    """Returned from stage_reactivation()."""
    admin_id: str
    closure_initiated_at: datetime
    grace_window_expires_at: datetime
    stripe_checkout_url: str
    stripe_checkout_session_id: str


@dataclass(frozen=True)
class ReactivationCompleted:
    """Returned from complete_reactivation() after Stripe confirms."""
    admin_id: str
    reactivated_at: datetime
    new_subscription_id: str
    instances_restored: int
    api_keys_revoked_count: int   # always 0; old keys stay revoked
    team_members_restored: int    # always 0; team members re-invite


# ---------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------

class ReactivationService:
    """Orchestrates account reactivation within the 30-day grace.

    Two-phase shape so the Stripe checkout happens between calls:

      stage_reactivation()    \u2192 returns checkout URL + session id
      \u2026 admin completes Stripe checkout \u2026
      complete_reactivation() \u2192 runs the inverse cascade, returns
                                 ReactivationCompleted

    The state between the two calls is not persisted as a separate
    "reactivation_in_flight" row; the admin's closure_initiated_at
    being non-NULL is the only state. If the admin abandons checkout,
    nothing rolls forward and the grace clock keeps ticking.
    """

    def __init__(
        self,
        db: Session,
        *,
        billing_service,
        audit_repository,
    ) -> None:
        self.db = db
        self.billing_service = billing_service
        self.audit_repository = audit_repository

    # -----------------------------------------------------------------
    # Phase 1 \u2014 stage. Verify eligibility, issue checkout URL.
    # -----------------------------------------------------------------

    def stage_reactivation(
        self,
        *,
        admin_id: str,
        target_tier: str,
        success_url: str,
        cancel_url: str,
    ) -> ReactivationStaged:
        """Verify reactivation eligibility and start a Stripe checkout.

        No DB mutation in this phase. Pure read + Stripe call. If the
        admin abandons the checkout, nothing has been changed.
        """
        admin = self._load_admin_for_reactivation(admin_id)

        # Defensive recompute of grace window even though the route
        # layer should have already gated. Belt-and-suspenders.
        now = datetime.now(timezone.utc)
        grace_expires = admin.closure_initiated_at + timedelta(
            days=GRACE_WINDOW_DAYS
        )
        if now >= grace_expires:
            raise ReactivationWindowExpiredError(
                f"Admin {admin_id!r} grace window expired at "
                f"{grace_expires.isoformat()}; reactivation impossible."
            )

        # Stripe call. The new checkout session creates a fresh
        # subscription on completion; we do NOT resurrect the old
        # cancelled subscription. The previous subscription row
        # stays in the DB as historical record.
        try:
            session = self.billing_service.create_reactivation_checkout(
                admin_id=admin_id,
                target_tier=target_tier,
                success_url=success_url,
                cancel_url=cancel_url,
            )
        except Exception as exc:
            raise StripeReactivationCheckoutFailedError(
                f"Stripe checkout failed during reactivation stage for "
                f"admin {admin_id!r}: {exc}"
            ) from exc

        return ReactivationStaged(
            admin_id=admin_id,
            closure_initiated_at=admin.closure_initiated_at,
            grace_window_expires_at=grace_expires,
            stripe_checkout_url=session.url,
            stripe_checkout_session_id=session.id,
        )

    # -----------------------------------------------------------------
    # Phase 2 \u2014 complete. Run inverse cascade after Stripe confirms.
    # -----------------------------------------------------------------

    def complete_reactivation(
        self,
        *,
        admin_id: str,
        stripe_checkout_session_id: str,
        audit_ctx,
    ) -> ReactivationCompleted:
        """Finalize reactivation after Stripe checkout completes.

        Steps (all in one transaction):
          1. Re-verify the admin is still in grace.
          2. Verify the Stripe session belongs to this admin and is
             in 'complete' status (Stripe call).
          3. Clear closure_initiated_at, closure_cancel_mode,
             deactivated_at; set active=true.
          4. Run the inverse cascade \u2014 every child row the closure
             cascade soft-deleted gets active=true + deactivated_at=
             NULL. Embed keys stay revoked. Downgrade-archived rows
             stay archived.
          5. Audit row.

        The new subscription_id is recorded in the audit-row after_json
        so forensics can correlate the reactivation event with the
        Stripe webhook that arrives after this method returns.
        """
        # Re-verify state. The grace clock could have expired between
        # stage and complete if Stripe checkout took >30 days, or
        # the admin could have been hard-deleted in a race against
        # the retention worker.
        admin = self._load_admin_for_reactivation(admin_id)

        # Stripe-side verification.
        try:
            session = self.billing_service.retrieve_checkout_session(
                session_id=stripe_checkout_session_id,
            )
        except Exception as exc:
            raise StripeSubscriptionMismatchError(
                f"Could not retrieve Stripe session "
                f"{stripe_checkout_session_id!r}: {exc}"
            ) from exc

        # The session's metadata should carry admin_id (set in the
        # stage_reactivation create_checkout_session call). Verify
        # the admin claiming reactivation matches.
        session_admin_id = (
            getattr(session, "metadata", None) or {}
        ).get("admin_id")
        if session_admin_id != admin_id:
            raise StripeSubscriptionMismatchError(
                f"Stripe session {stripe_checkout_session_id!r} does "
                f"not belong to admin {admin_id!r}."
            )
        if getattr(session, "status", None) != "complete":
            raise StripeSubscriptionMismatchError(
                f"Stripe session {stripe_checkout_session_id!r} is "
                f"not in 'complete' status."
            )

        new_subscription_id = (
            getattr(session, "subscription", None)
            or session.get("subscription", "")
        )
        if hasattr(new_subscription_id, "id"):
            new_subscription_id = new_subscription_id.id

        # --- Clear closure stamps on admins. ---
        now = datetime.now(timezone.utc)
        previous_closure_at = admin.closure_initiated_at
        admin.active = True
        admin.closure_initiated_at = None
        admin.closure_cancel_mode = None
        admin.deactivated_at = None
        self.db.flush()

        # --- Inverse cascade. ---
        # Restore every layer the closure cascade soft-deleted.
        # Layer ordering does not matter on the way back \u2014 these are
        # all idempotent UPDATEs scoped by admin_id.
        # Embed keys explicitly EXCLUDED: Vision \u00a76.4 says old keys
        # stay revoked. The reactivation route returns 0 here so the
        # frontend knows to prompt the admin to mint new keys.
        instances_restored = self._inverse_restore_table(
            table="instances",
            admin_id=admin_id,
        )
        # The other cascade layers \u2014 conversations, identity_claims,
        # memory_items \u2014 are restored similarly. We loop them rather
        # than enumerate each.
        for table in (
            "conversations",
            "identity_claims",
            "memory_items",
        ):
            self._inverse_restore_table(table=table, admin_id=admin_id)

        # --- Audit row. ---
        self.audit_repository.record(
            ctx=audit_ctx,
            admin_id=admin_id,
            action=ACTION_ACCOUNT_REACTIVATED,
            resource_type=RESOURCE_TENANT,
            resource_natural_id=admin_id,
            before={
                "active": False,
                "closure_initiated_at": previous_closure_at.isoformat()
                if previous_closure_at else None,
            },
            after={
                "active": True,
                "closure_initiated_at": None,
                "reactivated_at": now.isoformat(),
                "new_subscription_id": new_subscription_id,
                "instances_restored": instances_restored,
                "api_keys_revoked_count": 0,
                "team_members_restored": 0,
            },
            note=(
                f"Account reactivated for {admin_id} within grace "
                f"window; new subscription {new_subscription_id}."
            ),
            autocommit=False,
        )

        return ReactivationCompleted(
            admin_id=admin_id,
            reactivated_at=now,
            new_subscription_id=new_subscription_id,
            instances_restored=instances_restored,
            api_keys_revoked_count=0,
            team_members_restored=0,
        )

    # -----------------------------------------------------------------
    # Internals.
    # -----------------------------------------------------------------

    def _load_admin_for_reactivation(self, admin_id: str) -> Admin:
        """Load admin + verify it is in a reactivatable state.

        Raises:
          - AccountAlreadyTombstoneError if hard_deleted_at is set.
          - AccountNotInGraceError if closure_initiated_at is NULL.
          - ReactivationWindowExpiredError if > 30d since closure.
        """
        admin: Admin | None = self.db.get(Admin, admin_id)
        if admin is None:
            raise ReactivationError(f"Admin {admin_id!r} not found.")
        if admin.hard_deleted_at is not None:
            raise AccountAlreadyTombstoneError(
                f"Admin {admin_id!r} is already hard-deleted; "
                f"reactivation impossible."
            )
        if admin.closure_initiated_at is None:
            raise AccountNotInGraceError(
                f"Admin {admin_id!r} is not in a closure grace state."
            )
        now = datetime.now(timezone.utc)
        grace_expires = admin.closure_initiated_at + timedelta(
            days=GRACE_WINDOW_DAYS
        )
        if now >= grace_expires:
            raise ReactivationWindowExpiredError(
                f"Admin {admin_id!r} grace window expired at "
                f"{grace_expires.isoformat()}."
            )
        return admin

    def _inverse_restore_table(self, *, table: str, admin_id: str) -> int:
        """Flip active=true + deactivated_at=NULL for cascade-soft-

        deleted rows of one table belonging to this admin. Returns the
        rowcount of rows touched.

        Idempotent: rows already active=true (rare \u2014 maybe a partial
        cascade) are touched without effect since the UPDATE sets to
        their current state. Rows with pending_downgrade_archived_at
        set are NOT touched, even if their active=false \u2014 those
        belong to the downgrade-archive lifecycle, not the closure
        lifecycle, and re-upgrade is the only restoration path for
        them (founder lock A6).

        We do an indirect check on pending_downgrade_archived_at via a
        WHERE clause that is column-aware: tables that carry the
        column get the extra predicate; tables that don't get the
        plain restore. We discover column presence at runtime via the
        information_schema check below \u2014 cheaper than maintaining a
        manual list that drifts.
        """
        has_pending_col = self.db.execute(
            sql_text(
                """
                SELECT 1 FROM information_schema.columns
                 WHERE table_name = :tbl
                   AND column_name = 'pending_downgrade_archived_at'
                """
            ),
            {"tbl": table},
        ).first() is not None

        if has_pending_col:
            sql = sql_text(
                f"""
                UPDATE {table}
                   SET active = true,
                       deactivated_at = NULL
                 WHERE admin_id = :aid
                   AND active = false
                   AND pending_downgrade_archived_at IS NULL
                """
            )
        else:
            sql = sql_text(
                f"""
                UPDATE {table}
                   SET active = true,
                       deactivated_at = NULL
                 WHERE admin_id = :aid
                   AND active = false
                """
            )
        res = self.db.execute(sql, {"aid": admin_id})
        return int(res.rowcount or 0)
