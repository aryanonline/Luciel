"""UserInvite repository -- data-access layer for the user_invites table.

Step 30a.4. Wraps app.models.user_invite.UserInvite. Mirrors the
project-wide repository doctrine established by UserRepository and
AgentRepository:

* Pure CRUD. No ScopePolicy calls, no business-rule checks, no HTTP
  exceptions. Callers (InviteService / route handlers) handle those.
* Audit-row emission is the CALLER's responsibility for this resource,
  because the four-event invite arc (issue, redeem, resend, revoke)
  emits semantically distinct verbs with different actor + payload
  shapes -- pushing that decision into the repo would either bloat
  every method with an `action` parameter or split into four near-
  duplicate methods. InviteService is the chokepoint instead.
  This is the same pattern as ScopeAssignmentRepository.
* All mutations run under `autocommit=False` by default so the service
  layer can compose them into a single txn that includes the audit row.
* Case-insensitive email lookup goes through `func.lower()` to ride
  the partial-unique index on (tenant_id, LOWER(invited_email))
  WHERE status='pending'.

Domain-agnostic: no imports from app/domain/, no vertical branching.

Cross-refs
----------
app.models.user_invite      -- the model (closure-shape alpha docstring).
app.services.invite_service -- the chokepoint that wires audit + email.
app.api.v1.admin            -- the route layer (POST/GET/resend/revoke).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.user_invite import InviteStatus, UserInvite

logger = logging.getLogger(__name__)

# 7-day invite-row TTL. Closure-shape alpha: the row is the source of
# truth on expiry, independent of the 24h JWT TTL. Resend rotates the
# token_jti and re-mints a fresh 24h JWT against the same row, so the
# 7-day window is what the invitee experiences end-to-end.
INVITE_ROW_TTL = timedelta(days=7)


class UserInviteRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        tenant_id: str,
        domain_id: str,
        inviter_user_id: uuid.UUID,
        invited_email: str,
        role: str,
        token_jti: str,
        expires_at: datetime | None = None,
        autocommit: bool = False,
    ) -> UserInvite:
        """Insert a new pending UserInvite row.

        The partial unique index on (tenant_id, LOWER(invited_email))
        WHERE status='pending' enforces the no-duplicate-pending invariant
        at the schema layer. Caller (InviteService) is expected to
        translate IntegrityError into a 409 at the route layer.

        autocommit=False is the default so InviteService can wrap this in
        a transaction that also writes the audit row. expires_at defaults
        to now + INVITE_ROW_TTL when not supplied.
        """
        if expires_at is None:
            expires_at = datetime.now(timezone.utc) + INVITE_ROW_TTL

        invite = UserInvite(
            tenant_id=tenant_id,
            domain_id=domain_id,
            inviter_user_id=inviter_user_id,
            invited_email=invited_email.strip(),
            role=role,
            token_jti=token_jti,
            status=InviteStatus.PENDING,
            expires_at=expires_at,
        )
        self.db.add(invite)
        self.db.flush()  # assigns invite.id, enables audit write before commit

        if autocommit:
            self.db.commit()
            self.db.refresh(invite)

        logger.info(
            "UserInvite created id=%s tenant=%s email=%s role=%s expires_at=%s",
            invite.id,
            tenant_id,
            invited_email,
            role,
            expires_at.isoformat(),
        )
        return invite

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_by_pk(self, pk: uuid.UUID) -> UserInvite | None:
        return self.db.query(UserInvite).filter(UserInvite.id == pk).first()

    def get_by_jti(self, token_jti: str) -> UserInvite | None:
        """Lookup invite by JWT jti -- the redemption hot path.

        Rides the unique index on user_invites.token_jti so this is O(1).
        Returns the row regardless of status; the gate (status='pending'
        AND expires_at > now()) is enforced at the InviteService layer
        so the row mutation and audit row can ride one transaction.
        """
        if not token_jti:
            return None
        return (
            self.db.query(UserInvite)
            .filter(UserInvite.token_jti == token_jti)
            .first()
        )

    def get_pending_for_email(
        self,
        *,
        tenant_id: str,
        invited_email: str,
    ) -> UserInvite | None:
        """Case-insensitive lookup of the pending invite for (tenant, email).

        Rides the partial unique index on (tenant_id, LOWER(invited_email))
        WHERE status='pending'. Returns at most one row by construction.
        Used by InviteService.create_invite as the pre-flight duplicate
        guard so we surface a clean 409 before the INSERT race.
        """
        if not invited_email:
            return None
        return (
            self.db.query(UserInvite)
            .filter(
                UserInvite.tenant_id == tenant_id,
                func.lower(UserInvite.invited_email)
                == invited_email.strip().lower(),
                UserInvite.status == InviteStatus.PENDING,
            )
            .first()
        )

    def list_for_tenant(
        self,
        *,
        tenant_id: str,
        statuses: tuple[InviteStatus, ...] | None = None,
    ) -> list[UserInvite]:
        """List invites under a tenant, optionally filtered by status.

        statuses=None returns every invite under the tenant -- used by
        the admin UI to render both the pending and the accepted lists
        in one round-trip. Caller filters / groups in Python.

        Default ordering is descending created_at so the freshest
        invites appear first in the admin list (matches the
        ScopeAssignment + Agent list endpoints' conventions).
        """
        query = select(UserInvite).where(UserInvite.tenant_id == tenant_id)
        if statuses:
            query = query.where(UserInvite.status.in_(statuses))
        query = query.order_by(UserInvite.created_at.desc())
        return list(self.db.execute(query).scalars().all())

    def count_pending_for_tenant(self, *, tenant_id: str) -> int:
        """Count still-pending invites under a tenant.

        Used by InviteService.create_invite to enforce the per-tier
        instance cap at the service layer (pending invites + active
        teammates must not exceed the Team / Company tier seat cap).
        """
        return (
            self.db.query(func.count(UserInvite.id))
            .filter(
                UserInvite.tenant_id == tenant_id,
                UserInvite.status == InviteStatus.PENDING,
            )
            .scalar()
        ) or 0

    # ------------------------------------------------------------------
    # State transitions (pending -> terminal)
    # ------------------------------------------------------------------
    #
    # All transition methods take an already-fetched invite row (rather
    # than re-fetching by pk) so the caller controls the lock context.
    # InviteService.redeem_invite fetches via get_by_jti, validates the
    # status + expires_at gate, then mutates -- all in one txn.

    def mark_accepted(
        self,
        invite: UserInvite,
        *,
        accepted_user_id: uuid.UUID,
        autocommit: bool = False,
    ) -> UserInvite:
        """Flip pending -> accepted and stamp accepted_at + accepted_user_id.

        Idempotency note: callers MUST gate on status == PENDING before
        calling this. Re-calling on an already-accepted row would be a
        bug; we do not silently no-op because that would mask logic
        errors in the redemption flow.
        """
        invite.status = InviteStatus.ACCEPTED
        invite.accepted_at = datetime.now(timezone.utc)
        invite.accepted_user_id = accepted_user_id
        self.db.flush()
        if autocommit:
            self.db.commit()
            self.db.refresh(invite)
        return invite

    def mark_revoked(
        self,
        invite: UserInvite,
        *,
        revoked_by_api_key_id: int | None = None,
        autocommit: bool = False,
    ) -> UserInvite:
        """Flip pending -> revoked and stamp revoked_at + revoked_by_api_key_id.

        Same idempotency note as mark_accepted: caller gates on
        status == PENDING.
        """
        invite.status = InviteStatus.REVOKED
        invite.revoked_at = datetime.now(timezone.utc)
        invite.revoked_by_api_key_id = revoked_by_api_key_id
        self.db.flush()
        if autocommit:
            self.db.commit()
            self.db.refresh(invite)
        return invite

    def mark_expired(
        self,
        invite: UserInvite,
        *,
        autocommit: bool = False,
    ) -> UserInvite:
        """Flip pending -> expired.

        Called from the redemption-gate path when a still-pending row's
        expires_at has elapsed (lazy expiry) and from a future retention
        worker sweep (eager expiry). Distinct verb from mark_revoked
        because revoke is admin-initiated and expired is system-initiated;
        the audit row distinguishes the two cleanly.
        """
        invite.status = InviteStatus.EXPIRED
        self.db.flush()
        if autocommit:
            self.db.commit()
            self.db.refresh(invite)
        return invite

    def rotate_token_jti(
        self,
        invite: UserInvite,
        *,
        new_token_jti: str,
        new_expires_at: datetime | None = None,
        autocommit: bool = False,
    ) -> UserInvite:
        """Rotate token_jti on a still-pending invite (resend path).

        Closure-shape alpha resend mechanic: the invite ROW stays the
        same (preserving created_at, inviter, role); only the JWT
        capability handle rotates. The unique index on token_jti makes
        the old jti unredeemable; the new jti is the only valid handle
        going forward.

        Optionally also bumps expires_at -- the route layer is expected
        to NOT bump it on a vanilla resend (so the 7-day window stays
        anchored to first-issue), but a future "renew invite" affordance
        could pass new_expires_at to restart the window.

        Caller MUST gate on status == PENDING before calling.
        """
        invite.token_jti = new_token_jti
        if new_expires_at is not None:
            invite.expires_at = new_expires_at
        self.db.flush()
        if autocommit:
            self.db.commit()
            self.db.refresh(invite)
        return invite
