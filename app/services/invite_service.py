"""InviteService -- chokepoint for the four-event UserInvite lifecycle.

Step 30a.4. Module-level functions matching the AuthService convention
(`auth_service.verify_password`, `auth_service.set_password`, ...) so
the call sites in `app/api/v1/admin.py` stay symmetric with the rest of
the auth surface.

The four events
---------------

* ``create_invite``   -- mint a UserInvite row + 24h set_password JWT,
                         send the invite email, emit ACTION_USER_INVITED.
                         Surface: ``POST /api/v1/admin/invites``.
* ``redeem_invite``   -- consume a set_password token of
                         ``purpose=\"invite\"``: provision User + Agent +
                         ScopeAssignment, set password, mark accepted,
                         emit ACTION_INVITE_REDEEMED. Surface: the
                         existing ``POST /api/v1/auth/set-password``
                         route detects ``purpose=invite`` from the JWT
                         and routes here.
* ``resend_invite``   -- rotate token_jti on a still-pending row,
                         re-mint a fresh 24h JWT, resend the email,
                         emit ACTION_INVITE_RESENT. Surface: ``POST
                         /api/v1/admin/invites/{id}/resend``.
* ``revoke_invite``   -- terminal flip pending -> revoked, emit
                         ACTION_INVITE_REVOKED. Surface: ``DELETE
                         /api/v1/admin/invites/{id}``.

Audit doctrine
--------------

Every mutation writes an admin_audit_logs row in the SAME DB
transaction as the row write. This is invariant 4 (audit-in-same-txn)
and the same pattern UserRepository / ScopeAssignmentRepository follow.
``resource_type=RESOURCE_USER_INVITE``, ``resource_pk=None`` (the PK is
a UUID, not an int -- same convention as RESOURCE_USER),
``resource_natural_id=invited_email`` so the audit chain is filterable
by invitee email regardless of which UserInvite row is involved.

Closure-shape (alpha)
---------------------

* Invite row TTL = 7 days (``INVITE_ROW_TTL`` in
  ``user_invites`` repository). Source of truth on expiry.
* JWT TTL = 24h (``settings.magic_link_ttl_hours``, applied by
  ``mint_set_password_token``).
* Resend rotates ``token_jti`` and re-mints a fresh JWT against the
  same invite row. The 7-day window stays anchored to first-issue.

Cross-refs
----------

* ``app.models.user_invite``               -- the row model + closure-shape docstring.
* ``app.repositories.user_invites``        -- the CRUD layer.
* ``app.services.magic_link_service``      -- mint/consume set_password tokens.
* ``app.services.email_service``           -- send welcome / invite emails.
* ``app.api.v1.admin``                     -- the route layer.
* ``app.api.v1.auth``                      -- the set-password redemption route.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.admin_audit_log import (
    ACTION_INVITE_REDEEMED,
    ACTION_INVITE_RESENT,
    ACTION_INVITE_REVOKED,
    ACTION_USER_INVITED,
    RESOURCE_USER_INVITE,
)
from app.models.agent import Agent
from app.models.scope_assignment import ScopeAssignment
from app.models.user import User
from app.models.user_invite import InviteStatus, UserInvite
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)
from app.repositories.user_invites import INVITE_ROW_TTL, UserInviteRepository
from app.services import auth_service
from app.services.email_service import (
    WelcomeEmailError,
    send_welcome_set_password_email,
)
from app.services.magic_link_service import (
    JWT_ALGORITHM,
    build_set_password_url,
    mint_set_password_token,
)
from app.services.tier_provisioning_service import _slugify_agent_id_from_email

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Error classes
# ---------------------------------------------------------------------
#
# Plain Python exceptions, no HTTP coupling. The route layer maps these
# to status codes. Same convention as AuthService.

class InviteError(Exception):
    """Base class for InviteService failures."""


class DuplicatePendingInviteError(InviteError):
    """Surfaces as 409 -- a pending invite for (tenant, email) already exists."""


class InviteNotFoundError(InviteError):
    """Surfaces as 404 -- token jti or invite id has no matching row."""


class InviteNotPendingError(InviteError):
    """Surfaces as 409 -- invite has already been accepted, revoked, or expired."""


class InviteExpiredError(InviteError):
    """Surfaces as 410 Gone -- invite row TTL has elapsed."""


class InvitePendingCapExceededError(InviteError):
    """Surfaces as 409 -- too many pending invites already outstanding."""


# ---------------------------------------------------------------------
# Pending-invite cap
# ---------------------------------------------------------------------
#
# This is a soft anti-runaway guard, not a billing seat cap (§14 forbids
# per-seat metering). We cap pending invites at 2x the tier's instance
# cap so a tenant can comfortably stage future provisioning without a
# runaway script fanning out 1000 invites. The cap is recomputed lazily
# on each create_invite call and uses TIER_INSTANCE_CAPS as the bound.

_DEFAULT_MAX_PENDING_INVITES = 100


def _resolve_max_pending_invites(*, tenant_id: str, db: Session) -> int:
    """Resolve the pending-invite cap for a tenant from its active sub.

    Returns 2x the subscription's instance_count_cap when there is an
    active subscription; falls back to _DEFAULT_MAX_PENDING_INVITES
    otherwise (e.g. during onboarding before the first webhook lands).
    """
    from app.models.subscription import STATUS_ACTIVE, STATUS_TRIALING, Subscription

    sub = (
        db.query(Subscription)
        .filter(
            Subscription.tenant_id == tenant_id,
            Subscription.status.in_((STATUS_ACTIVE, STATUS_TRIALING)),
            Subscription.active.is_(True),
        )
        .order_by(Subscription.created_at.desc())
        .first()
    )
    if sub is None or sub.instance_count_cap is None:
        return _DEFAULT_MAX_PENDING_INVITES
    return max(_DEFAULT_MAX_PENDING_INVITES, sub.instance_count_cap * 2)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _extract_jti(token: str) -> str:
    """Decode a JWT just enough to read its jti without validating signature.

    The redemption path validates the signature + expiry via
    ``consume_set_password_token`` BEFORE calling redeem_invite -- this
    helper is only used to peel jti so the repo lookup can ride the
    unique index. Doing two decodes is acceptable; the alternative
    (passing the parsed payload through the call chain) would couple the
    service to the JWT representation more tightly than is justified for
    a single field read.
    """
    import jwt as _jwt

    unverified = _jwt.decode(
        token,
        options={"verify_signature": False, "verify_exp": False},
        algorithms=[JWT_ALGORITHM],
    )
    jti = unverified.get("jti")
    if not isinstance(jti, str) or not jti:
        raise InviteError("token has no jti claim")
    return jti


# ---------------------------------------------------------------------
# create_invite -- ACTION_USER_INVITED
# ---------------------------------------------------------------------


def create_invite(
    *,
    db: Session,
    tenant_id: str,
    domain_id: str,
    inviter_user_id: uuid.UUID,
    inviter_email: str,
    invited_email: str,
    role: str = "teammate",
    audit_ctx: AuditContext,
) -> tuple[UserInvite, str]:
    """Mint a UserInvite + 24h set_password JWT, send the invite email.

    Returns ``(invite_row, raw_jwt)``. The route layer surfaces a 201
    with the invite_row shape; the raw JWT is consumed only by the
    email send and is NOT exposed in the API response (the invitee
    receives it via email, not via the admin's response body).

    Audit row: ACTION_USER_INVITED / RESOURCE_USER_INVITE /
    resource_natural_id=invited_email.

    Raises:
      * DuplicatePendingInviteError -- a still-pending invite for
        (tenant, email) already exists. Use ``resend_invite`` instead.
      * InvitePendingCapExceededError -- too many outstanding invites
        under this tenant; the anti-runaway guard. Cap is 2x the active
        subscription's instance_count_cap (or 100 if no active sub).
    """
    repo = UserInviteRepository(db)
    email_norm = invited_email.strip()
    email_lc = email_norm.lower()

    # 1. Duplicate-pending pre-flight. The partial unique index would
    #    raise IntegrityError on race, but a clean 409 is friendlier.
    existing = repo.get_pending_for_email(
        tenant_id=tenant_id, invited_email=email_norm
    )
    if existing is not None:
        raise DuplicatePendingInviteError(
            f"A pending invite for {email_lc} already exists under "
            f"tenant {tenant_id}; use resend instead of create."
        )

    # 2. Pending-invite cap.
    pending_count = repo.count_pending_for_tenant(tenant_id=tenant_id)
    cap = _resolve_max_pending_invites(tenant_id=tenant_id, db=db)
    if pending_count >= cap:
        raise InvitePendingCapExceededError(
            f"Tenant {tenant_id} already has {pending_count} pending "
            f"invites (cap={cap}). Resolve some before issuing more."
        )

    # 3. Mint set_password token first so we can write the jti onto the
    #    row in the same INSERT (the unique index on token_jti requires
    #    it be present at INSERT time). Use a placeholder user_id (the
    #    inviter) since the JWT's ``sub`` is unused for invite-purpose
    #    redemption -- redeem_invite looks up by jti, not sub.
    token = mint_set_password_token(
        user_id=inviter_user_id,
        email=email_norm,
        tenant_id=tenant_id,
        purpose="invite",
    )
    token_jti = _extract_jti(token)

    try:
        invite = repo.create(
            tenant_id=tenant_id,
            domain_id=domain_id,
            inviter_user_id=inviter_user_id,
            invited_email=email_norm,
            role=role,
            token_jti=token_jti,
            autocommit=False,
        )

        AdminAuditRepository(db).record(
            ctx=audit_ctx,
            tenant_id=tenant_id,
            domain_id=domain_id,
            action=ACTION_USER_INVITED,
            resource_type=RESOURCE_USER_INVITE,
            resource_pk=None,  # UUID PK, use natural id
            resource_natural_id=email_lc,
            after={
                "invite_id": str(invite.id),
                "tenant_id": tenant_id,
                "domain_id": domain_id,
                "invited_email": email_norm,
                "role": role,
                "inviter_user_id": str(inviter_user_id),
                "inviter_email": inviter_email,
                "expires_at": invite.expires_at.isoformat(),
            },
            note=f"team_invite: minted UserInvite for {email_lc}",
            autocommit=False,
        )

        db.commit()
        db.refresh(invite)
    except Exception:
        db.rollback()
        raise

    # 4. Best-effort email send. SES failures do NOT roll back the
    #    invite row (the admin can resend from the dashboard). Same
    #    fail-open contract as _invite_teammate.
    try:
        send_welcome_set_password_email(
            to_email=email_norm,
            set_password_url=build_set_password_url(token),
            display_name=None,
            purpose="invite",
        )
    except WelcomeEmailError:
        logger.exception(
            "create_invite: invite email send failed tenant=%s invite=%s "
            "email=%s -- row committed, admin can resend",
            tenant_id,
            invite.id,
            email_lc,
        )

    logger.info(
        "Invite created tenant=%s invite_id=%s email=%s role=%s "
        "inviter=%s expires_at=%s",
        tenant_id,
        invite.id,
        email_lc,
        role,
        inviter_email,
        invite.expires_at.isoformat(),
    )
    return invite, token


# ---------------------------------------------------------------------
# redeem_invite -- ACTION_INVITE_REDEEMED
# ---------------------------------------------------------------------


def redeem_invite(
    *,
    db: Session,
    token: str,
    payload: dict,
    password: str,
    audit_ctx: AuditContext,
) -> tuple[UserInvite, User]:
    """Consume an invite-purpose set_password token end-to-end.

    Called from ``POST /api/v1/auth/set-password`` when the JWT payload
    carries ``purpose=\"invite\"``. The route layer:
      1. Validates the JWT via ``consume_set_password_token`` (signature
         + exp + typ).
      2. Calls this with the parsed payload + the new password.

    This function:
      a. Looks up the invite by ``payload['jti']``.
      b. Gates on status == PENDING and expires_at > now() (lazy expiry).
      c. Provisions User + Agent + ScopeAssignment (or fetches the
         existing ones, mirroring ``_invite_teammate``).
      d. Sets the password via ``auth_service.set_password``.
      e. Flips the invite row to ACCEPTED.
      f. Emits ACTION_INVITE_REDEEMED.

    All six steps run under one DB transaction. Returns
    ``(invite, user)`` so the caller can mint the session cookie
    against user.id.

    Raises:
      * InviteNotFoundError    -- jti has no matching row (404).
      * InviteNotPendingError  -- already accepted / revoked (409).
      * InviteExpiredError     -- row TTL elapsed (410); row is flipped
                                  to EXPIRED in the same txn so the next
                                  redemption attempt fails with the same
                                  gate.
    """
    repo = UserInviteRepository(db)
    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti:
        raise InviteNotFoundError("token has no jti claim")

    invite = repo.get_by_jti(jti)
    if invite is None:
        raise InviteNotFoundError(f"no UserInvite found for jti={jti}")

    if invite.status != InviteStatus.PENDING:
        raise InviteNotPendingError(
            f"invite {invite.id} is {invite.status.value}, not pending"
        )

    now = datetime.now(timezone.utc)
    if invite.expires_at <= now:
        # Lazy expiry: flip the row + audit it before raising so the
        # next attempt sees the same terminal state.
        try:
            repo.mark_expired(invite, autocommit=False)
            AdminAuditRepository(db).record(
                ctx=audit_ctx,
                tenant_id=invite.tenant_id,
                domain_id=invite.domain_id,
                action=ACTION_INVITE_REVOKED,  # closest existing verb for system-initiated expiry
                resource_type=RESOURCE_USER_INVITE,
                resource_pk=None,
                resource_natural_id=invite.invited_email.lower(),
                before={"status": "pending"},
                after={"status": "expired"},
                note=(
                    f"team_invite: invite {invite.id} expired at redemption "
                    f"time (expires_at={invite.expires_at.isoformat()})"
                ),
                autocommit=False,
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        raise InviteExpiredError(
            f"invite {invite.id} expired at "
            f"{invite.expires_at.isoformat()}; ask the admin to resend"
        )

    tenant_id = invite.tenant_id
    domain_id = invite.domain_id
    email_norm = invite.invited_email.strip()
    email_lc = email_norm.lower()

    try:
        # --- Provision User + Agent + ScopeAssignment (mirror _invite_teammate) ---
        user = db.execute(
            select(User).where(func.lower(User.email) == email_lc)
        ).scalars().first()
        if user is None:
            user = User(
                email=email_norm,
                display_name=email_norm,
                synthetic=False,
                active=True,
            )
            db.add(user)
            db.flush()

        agent_slug = _slugify_agent_id_from_email(email_norm)
        existing_agent = db.execute(
            select(Agent).where(
                Agent.tenant_id == tenant_id,
                Agent.domain_id == domain_id,
                Agent.agent_id == agent_slug,
            )
        ).scalars().first()
        if existing_agent is not None and existing_agent.user_id == user.id:
            agent = existing_agent
        else:
            if existing_agent is not None:
                # Slug collision on a different user -- suffix.
                agent_slug = f"{agent_slug}-{user.id.hex[:8]}"[:100]
            agent = Agent(
                tenant_id=tenant_id,
                domain_id=domain_id,
                agent_id=agent_slug,
                display_name=email_norm,
                description="Teammate provisioned via Step 30a.4 invite redemption.",
                contact_email=email_norm,
                user_id=user.id,
                active=True,
                created_by="team_invite",
            )
            db.add(agent)
            db.flush()

        existing_assignment = db.execute(
            select(ScopeAssignment).where(
                ScopeAssignment.user_id == user.id,
                ScopeAssignment.tenant_id == tenant_id,
                ScopeAssignment.domain_id == domain_id,
                ScopeAssignment.ended_at.is_(None),
                ScopeAssignment.active.is_(True),
            )
        ).scalars().first()
        if existing_assignment is None:
            assignment = ScopeAssignment(
                user_id=user.id,
                tenant_id=tenant_id,
                domain_id=domain_id,
                role=invite.role,
                active=True,
            )
            db.add(assignment)
            db.flush()

        # --- Set password. auth_service.set_password commits internally
        #     (it manages its own txn boundary) -- so we commit our
        #     User/Agent/ScopeAssignment writes first via flush, then
        #     call set_password, then write the audit row and accept
        #     row in a second commit. ---
        auth_service.set_password(db=db, user_id=user.id, password=password)

        repo.mark_accepted(invite, accepted_user_id=user.id, autocommit=False)

        AdminAuditRepository(db).record(
            ctx=audit_ctx,
            tenant_id=tenant_id,
            domain_id=domain_id,
            agent_id=agent_slug,
            action=ACTION_INVITE_REDEEMED,
            resource_type=RESOURCE_USER_INVITE,
            resource_pk=None,
            resource_natural_id=email_lc,
            before={"status": "pending"},
            after={
                "status": "accepted",
                "invite_id": str(invite.id),
                "accepted_user_id": str(user.id),
                "agent_id": agent_slug,
                "tenant_id": tenant_id,
                "domain_id": domain_id,
                "role": invite.role,
            },
            note=(
                f"team_invite: redeemed invite {invite.id} -> "
                f"provisioned user {user.id} agent {agent_slug}"
            ),
            autocommit=False,
        )

        db.commit()
        db.refresh(invite)
        db.refresh(user)
    except Exception:
        db.rollback()
        raise

    logger.info(
        "Invite redeemed tenant=%s invite_id=%s user_id=%s agent=%s email=%s",
        tenant_id,
        invite.id,
        user.id,
        agent_slug,
        email_lc,
    )
    return invite, user


# ---------------------------------------------------------------------
# resend_invite -- ACTION_INVITE_RESENT
# ---------------------------------------------------------------------


def resend_invite(
    *,
    db: Session,
    invite_id: uuid.UUID,
    inviter_user_id: uuid.UUID,
    audit_ctx: AuditContext,
    bump_expires_at: bool = False,
) -> tuple[UserInvite, str]:
    """Rotate token_jti on a still-pending invite, re-mint a fresh JWT.

    The invite row stays put (preserving created_at, role, inviter) --
    only the JWT capability handle rotates. The old JWT becomes
    unredeemable the instant the new token_jti lands (the unique index
    guarantees redeem_invite cannot find the old jti).

    bump_expires_at=False keeps the 7-day window anchored to first-issue.
    A future "renew invite" affordance can pass True to restart the
    window, but the v1 admin UI does NOT expose that flag (so a stale
    invite eventually times out instead of being renewable forever).

    Audit: ACTION_INVITE_RESENT with before={token_jti=<old>},
    after={token_jti=<new>}.
    """
    repo = UserInviteRepository(db)
    invite = repo.get_by_pk(invite_id)
    if invite is None:
        raise InviteNotFoundError(f"no UserInvite found for id={invite_id}")
    if invite.status != InviteStatus.PENDING:
        raise InviteNotPendingError(
            f"invite {invite.id} is {invite.status.value}, not pending"
        )

    # If the 7-day row TTL has already elapsed, refuse and flip to expired.
    now = datetime.now(timezone.utc)
    if invite.expires_at <= now and not bump_expires_at:
        try:
            repo.mark_expired(invite, autocommit=False)
            db.commit()
        except Exception:
            db.rollback()
            raise
        raise InviteExpiredError(
            f"invite {invite.id} expired at "
            f"{invite.expires_at.isoformat()}; create a fresh invite instead"
        )

    old_jti = invite.token_jti
    new_token = mint_set_password_token(
        user_id=inviter_user_id,
        email=invite.invited_email,
        tenant_id=invite.tenant_id,
        purpose="invite",
    )
    new_jti = _extract_jti(new_token)
    new_expires_at: datetime | None = None
    if bump_expires_at:
        new_expires_at = now + INVITE_ROW_TTL

    try:
        repo.rotate_token_jti(
            invite,
            new_token_jti=new_jti,
            new_expires_at=new_expires_at,
            autocommit=False,
        )
        AdminAuditRepository(db).record(
            ctx=audit_ctx,
            tenant_id=invite.tenant_id,
            domain_id=invite.domain_id,
            action=ACTION_INVITE_RESENT,
            resource_type=RESOURCE_USER_INVITE,
            resource_pk=None,
            resource_natural_id=invite.invited_email.lower(),
            before={"token_jti": old_jti},
            after={
                "token_jti": new_jti,
                "invite_id": str(invite.id),
                "expires_at": invite.expires_at.isoformat(),
                "bumped_expires_at": bool(bump_expires_at),
            },
            note=f"team_invite: resent invite {invite.id} (rotated jti)",
            autocommit=False,
        )
        db.commit()
        db.refresh(invite)
    except Exception:
        db.rollback()
        raise

    # Best-effort email re-send.
    try:
        send_welcome_set_password_email(
            to_email=invite.invited_email,
            set_password_url=build_set_password_url(new_token),
            display_name=None,
            purpose="invite",
        )
    except WelcomeEmailError:
        logger.exception(
            "resend_invite: email send failed tenant=%s invite=%s email=%s "
            "-- row committed, admin can resend again",
            invite.tenant_id,
            invite.id,
            invite.invited_email,
        )

    logger.info(
        "Invite resent tenant=%s invite_id=%s email=%s old_jti=%s new_jti=%s",
        invite.tenant_id,
        invite.id,
        invite.invited_email,
        old_jti,
        new_jti,
    )
    return invite, new_token


# ---------------------------------------------------------------------
# revoke_invite -- ACTION_INVITE_REVOKED
# ---------------------------------------------------------------------


def revoke_invite(
    *,
    db: Session,
    invite_id: uuid.UUID,
    audit_ctx: AuditContext,
    revoked_by_api_key_id: int | None = None,
    reason: str | None = None,
) -> UserInvite:
    """Flip a still-pending invite to REVOKED.

    Idempotency: revoking an already-terminal invite raises
    InviteNotPendingError rather than silently no-opping; this mirrors
    the UserInviteRepository.mark_* contract and keeps audit-row
    semantics honest (a no-op should not write a row that says
    "revoked").
    """
    repo = UserInviteRepository(db)
    invite = repo.get_by_pk(invite_id)
    if invite is None:
        raise InviteNotFoundError(f"no UserInvite found for id={invite_id}")
    if invite.status != InviteStatus.PENDING:
        raise InviteNotPendingError(
            f"invite {invite.id} is {invite.status.value}, not pending"
        )

    try:
        repo.mark_revoked(
            invite,
            revoked_by_api_key_id=revoked_by_api_key_id,
            autocommit=False,
        )
        AdminAuditRepository(db).record(
            ctx=audit_ctx,
            tenant_id=invite.tenant_id,
            domain_id=invite.domain_id,
            action=ACTION_INVITE_REVOKED,
            resource_type=RESOURCE_USER_INVITE,
            resource_pk=None,
            resource_natural_id=invite.invited_email.lower(),
            before={"status": "pending"},
            after={
                "status": "revoked",
                "invite_id": str(invite.id),
                "revoked_at": (
                    invite.revoked_at.isoformat() if invite.revoked_at else None
                ),
                "revoked_by_api_key_id": revoked_by_api_key_id,
            },
            note=reason or f"team_invite: revoked invite {invite.id}",
            autocommit=False,
        )
        db.commit()
        db.refresh(invite)
    except Exception:
        db.rollback()
        raise

    logger.info(
        "Invite revoked tenant=%s invite_id=%s email=%s",
        invite.tenant_id,
        invite.id,
        invite.invited_email,
    )
    return invite
