"""SiblingCallGrant service — Arc 12 WU4.

Orchestration on top of ``SiblingCallGrantRepository``. Centralises:

* the tier-conditional approval workflow (Pro ⇒ author lands ``live``;
  Enterprise ⇒ author lands ``pending_approval``; Free ⇒ author
  rejected because ``call_sibling_luciel`` is unavailable on Free
  per §3.3.4 master switch);
* the four state transitions (author / approve / reject / revoke);
* audit-row emission inside the same transaction as the mutation
  (the chain hash + Wall-2 verb the regulator filters on);
* the instance-deactivation cascade (§3.6.1 step 3) — bulk revoke
  every active grant where the instance appears as caller OR callee.

Wall-2 enforcement (the caller/callee scope check) lives at the
route layer where the ``Request`` is in hand — the service receives
already-validated instance ids and trusts them. This split is
intentional: pure DB services do not know about FastAPI request
state, and the route is the only place ``ScopePolicy`` ought to be
called.
"""
from __future__ import annotations

import logging
import uuid
from typing import Sequence

from sqlalchemy.orm import Session

from app.models.admin_audit_log import (
    ACTION_SIBLING_GRANT_APPROVED,
    ACTION_SIBLING_GRANT_AUTHORED,
    ACTION_SIBLING_GRANT_REJECTED,
    ACTION_SIBLING_GRANT_REVOKED,
    RESOURCE_SIBLING_CALL_GRANT,
)
from app.models.sibling_call_grant import (
    APPROVAL_STATE_LIVE,
    APPROVAL_STATE_PENDING,
    APPROVAL_STATE_REVOKED,
    SiblingCallGrant,
)
from app.policy.entitlements import (
    TIER_ENTERPRISE,
    TIER_FREE,
    TIER_PRO,
)
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)
from app.repositories.sibling_call_grant_repository import (
    SiblingCallGrantRepository,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Service-level exceptions — the API layer translates these to HTTP.
# ---------------------------------------------------------------------


class SiblingGrantError(Exception):
    """Base for all service-layer sibling-grant exceptions."""


class TierNotEligibleForSiblingGrants(SiblingGrantError):
    """The Admin's tier cannot author sibling grants.

    Free has ``composition_enabled=False`` per §3.3.4 — the master
    switch is off, so authoring is structurally rejected before any
    grant row is written. Pro and Enterprise are eligible (Pro lands
    live immediately; Enterprise lands pending_approval).
    """


class GrantNotFound(SiblingGrantError):
    """The grant id does not exist under this admin scope."""


class GrantAlreadyExists(SiblingGrantError):
    """A live or pending grant already exists for the triple.

    The partial unique index enforces this at the DB layer; this
    exception is the service-side translation so the API can return
    a clean 409.
    """


class InvalidStateTransition(SiblingGrantError):
    """The grant is not in the state the operation requires.

    Examples: approve on a non-pending row, reject on a non-pending
    row, revoke on an already-revoked row.
    """


# ---------------------------------------------------------------------
# Tier resolution — matches admin_knowledge._resolve_tier_entitlement.
# ---------------------------------------------------------------------


def _resolve_admin_tier(db: Session, *, admin_id: str) -> str:
    """Look up the Admin's tier string. Fail-closed to Free if the
    Admin row is missing or the tier is not one of the three known
    V2 tiers. This mirrors ``_resolve_tier_entitlement`` in
    admin_knowledge.py — see the doc there for the rationale.
    """
    from sqlalchemy import select

    from app.models.admin import Admin
    from app.policy.entitlements import TIER_ENTITLEMENTS

    row = db.execute(
        select(Admin.tier).where(Admin.id == admin_id)
    ).scalar_one_or_none()
    return row if row in TIER_ENTITLEMENTS else TIER_FREE


# ---------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------


class SiblingCallGrantService:
    """Author / approve / reject / revoke sibling-call grants.

    The four mutating methods each emit an admin_audit_log row in
    the same SQLAlchemy session as the grant mutation, so the audit
    chain is atomic with the data change. Callers control the
    commit via ``autocommit`` (the API layer commits at the route
    boundary; tests can flush + assert without committing).
    """

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = SiblingCallGrantRepository(db)
        self.audit_repo = AdminAuditRepository(db)

    # ------------------------------------------------------------------
    # Read passthroughs
    # ------------------------------------------------------------------

    def get_by_id(
        self, *, admin_id: str, grant_id: int
    ) -> SiblingCallGrant:
        grant = self.repo.get_by_id(admin_id=admin_id, grant_id=grant_id)
        if grant is None:
            raise GrantNotFound(
                f"sibling_call_grant id={grant_id} not found for admin={admin_id}"
            )
        return grant

    def list_for_admin(
        self,
        *,
        admin_id: str,
        approval_states: Sequence[str] | None = None,
        caller_instance_id: int | None = None,
        callee_instance_id: int | None = None,
    ) -> list[SiblingCallGrant]:
        return self.repo.list_for_admin(
            admin_id=admin_id,
            approval_states=approval_states,
            caller_instance_id=caller_instance_id,
            callee_instance_id=callee_instance_id,
        )

    # ------------------------------------------------------------------
    # Author
    # ------------------------------------------------------------------

    def author(
        self,
        *,
        admin_id: str,
        caller_instance_id: int,
        callee_instance_id: int,
        granted_by_user_id: uuid.UUID,
        audit_ctx: AuditContext,
        autocommit: bool = False,
    ) -> SiblingCallGrant:
        """Author a new sibling-call grant.

        Tier matrix (§3.3.4):
          * Free        ⇒ raise TierNotEligibleForSiblingGrants.
          * Pro         ⇒ initial_state = 'live'.
          * Enterprise  ⇒ initial_state = 'pending_approval'.

        If an active grant already exists for the triple, raises
        ``GrantAlreadyExists`` — the partial unique index would have
        caught this anyway, but the explicit lookup gives a cleaner
        error than translating an IntegrityError.
        """
        if caller_instance_id == callee_instance_id:
            # Defence in depth — the route layer should have rejected
            # this, and the DB CHECK constraint would catch it, but
            # surfacing here gives a service-layer-friendly error.
            raise InvalidStateTransition(
                "caller and callee Instance IDs must differ"
            )

        tier = _resolve_admin_tier(self.db, admin_id=admin_id)
        if tier == TIER_FREE:
            raise TierNotEligibleForSiblingGrants(
                "Sibling-Luciel composition is not available on the Free tier. "
                "Upgrade to Pro or Enterprise to author sibling-call grants."
            )

        existing = self.repo.get_active(
            admin_id=admin_id,
            caller_instance_id=caller_instance_id,
            callee_instance_id=callee_instance_id,
        )
        if existing is not None:
            raise GrantAlreadyExists(
                f"An active sibling-call grant already exists for "
                f"caller={caller_instance_id} → callee={callee_instance_id} "
                f"(grant_id={existing.id}, state={existing.approval_state}). "
                f"Revoke it before authoring a new one."
            )

        initial_state = (
            APPROVAL_STATE_LIVE if tier == TIER_PRO
            else APPROVAL_STATE_PENDING  # enterprise
        )

        grant = self.repo.author(
            admin_id=admin_id,
            caller_instance_id=caller_instance_id,
            callee_instance_id=callee_instance_id,
            granted_by_user_id=granted_by_user_id,
            initial_state=initial_state,
            autocommit=False,
        )

        self.audit_repo.record(
            ctx=audit_ctx,
            admin_id=admin_id,
            action=ACTION_SIBLING_GRANT_AUTHORED,
            resource_type=RESOURCE_SIBLING_CALL_GRANT,
            resource_pk=grant.id,
            resource_natural_id=(
                f"{caller_instance_id}->{callee_instance_id}"
            ),
            luciel_instance_id=caller_instance_id,
            before=None,
            after={
                "caller_instance_id": caller_instance_id,
                "callee_instance_id": callee_instance_id,
                "approval_state": initial_state,
                "tier": tier,
                "granted_by_user_id": str(granted_by_user_id),
            },
            autocommit=False,
        )

        if autocommit:
            self.db.commit()
            self.db.refresh(grant)
        return grant

    # ------------------------------------------------------------------
    # Approve (Enterprise pending → live)
    # ------------------------------------------------------------------

    def approve(
        self,
        *,
        admin_id: str,
        grant_id: int,
        approved_by_user_id: uuid.UUID,
        audit_ctx: AuditContext,
        autocommit: bool = False,
    ) -> SiblingCallGrant:
        """Flip a pending grant to live. Only valid from
        ``pending_approval`` — approve on a live grant is a 409,
        approve on a revoked grant is a 409.

        Caller (the route layer) must have already verified the
        actor holds the ``admin_owner`` role for this Admin.
        """
        grant = self.get_by_id(admin_id=admin_id, grant_id=grant_id)
        if grant.approval_state != APPROVAL_STATE_PENDING:
            raise InvalidStateTransition(
                f"Cannot approve grant id={grant_id}: current state "
                f"is {grant.approval_state!r}, only pending_approval "
                f"is approvable."
            )

        before_state = grant.approval_state
        self.repo.approve(
            grant=grant,
            approved_by_user_id=approved_by_user_id,
            autocommit=False,
        )

        self.audit_repo.record(
            ctx=audit_ctx,
            admin_id=admin_id,
            action=ACTION_SIBLING_GRANT_APPROVED,
            resource_type=RESOURCE_SIBLING_CALL_GRANT,
            resource_pk=grant.id,
            resource_natural_id=(
                f"{grant.caller_instance_id}->{grant.callee_instance_id}"
            ),
            luciel_instance_id=grant.caller_instance_id,
            before={"approval_state": before_state},
            after={
                "approval_state": APPROVAL_STATE_LIVE,
                "approved_by_user_id": str(approved_by_user_id),
                "approved_at": (
                    grant.approved_at.isoformat()
                    if grant.approved_at is not None
                    else None
                ),
            },
            autocommit=False,
        )

        if autocommit:
            self.db.commit()
            self.db.refresh(grant)
        return grant

    # ------------------------------------------------------------------
    # Reject (Enterprise pending → revoked, never went live)
    # ------------------------------------------------------------------

    def reject(
        self,
        *,
        admin_id: str,
        grant_id: int,
        rejected_by_user_id: uuid.UUID,
        audit_ctx: AuditContext,
        autocommit: bool = False,
    ) -> SiblingCallGrant:
        """Reject a pending grant. Only valid from
        ``pending_approval``; reject on a live grant is a 409 (use
        revoke), reject on a revoked grant is a 409.

        Distinct from revoke in two ways:
          1. State precondition: reject requires pending_approval,
             revoke requires live OR pending.
          2. Audit verb: ACTION_SIBLING_GRANT_REJECTED vs
             ACTION_SIBLING_GRANT_REVOKED, so an auditor scanning
             the chain can distinguish "denied at approval" from
             "withdrawn after life".

        DB column writes are the same (approval_state='revoked',
        revoked_at=now).
        """
        grant = self.get_by_id(admin_id=admin_id, grant_id=grant_id)
        if grant.approval_state != APPROVAL_STATE_PENDING:
            raise InvalidStateTransition(
                f"Cannot reject grant id={grant_id}: current state "
                f"is {grant.approval_state!r}, only pending_approval "
                f"is rejectable. Use revoke for live grants."
            )

        before_state = grant.approval_state
        self.repo.reject(
            grant=grant,
            rejected_by_user_id=rejected_by_user_id,
            autocommit=False,
        )

        self.audit_repo.record(
            ctx=audit_ctx,
            admin_id=admin_id,
            action=ACTION_SIBLING_GRANT_REJECTED,
            resource_type=RESOURCE_SIBLING_CALL_GRANT,
            resource_pk=grant.id,
            resource_natural_id=(
                f"{grant.caller_instance_id}->{grant.callee_instance_id}"
            ),
            luciel_instance_id=grant.caller_instance_id,
            before={"approval_state": before_state},
            after={
                "approval_state": APPROVAL_STATE_REVOKED,
                "rejected_by_user_id": str(rejected_by_user_id),
                "revoked_at": (
                    grant.revoked_at.isoformat()
                    if grant.revoked_at is not None
                    else None
                ),
            },
            autocommit=False,
        )

        if autocommit:
            self.db.commit()
            self.db.refresh(grant)
        return grant

    # ------------------------------------------------------------------
    # Revoke (live or pending → revoked)
    # ------------------------------------------------------------------

    def revoke(
        self,
        *,
        admin_id: str,
        grant_id: int,
        audit_ctx: AuditContext,
        cascade_source_instance_id: int | None = None,
        autocommit: bool = False,
    ) -> SiblingCallGrant:
        """Revoke a live or pending grant. Idempotent against
        already-revoked rows is intentionally NOT honoured — calling
        revoke on a revoked grant raises ``InvalidStateTransition``
        so the API can return 409 and the operator notices the bug.

        ``cascade_source_instance_id`` is set by the deactivation
        cascade so the audit row's ``after_json`` records the
        triggering Instance. Operator revokes leave it None.
        """
        grant = self.get_by_id(admin_id=admin_id, grant_id=grant_id)
        if grant.approval_state == APPROVAL_STATE_REVOKED:
            raise InvalidStateTransition(
                f"Cannot revoke grant id={grant_id}: already revoked "
                f"at {grant.revoked_at}."
            )

        before_state = grant.approval_state
        self.repo.revoke(grant=grant, autocommit=False)

        after_payload: dict = {
            "approval_state": APPROVAL_STATE_REVOKED,
            "revoked_at": (
                grant.revoked_at.isoformat()
                if grant.revoked_at is not None
                else None
            ),
        }
        if cascade_source_instance_id is not None:
            after_payload["cascade_source_instance_id"] = (
                cascade_source_instance_id
            )

        self.audit_repo.record(
            ctx=audit_ctx,
            admin_id=admin_id,
            action=ACTION_SIBLING_GRANT_REVOKED,
            resource_type=RESOURCE_SIBLING_CALL_GRANT,
            resource_pk=grant.id,
            resource_natural_id=(
                f"{grant.caller_instance_id}->{grant.callee_instance_id}"
            ),
            luciel_instance_id=grant.caller_instance_id,
            before={"approval_state": before_state},
            after=after_payload,
            note=(
                f"Cascade revoke from instance deactivation "
                f"(instance_id={cascade_source_instance_id})"
                if cascade_source_instance_id is not None
                else None
            ),
            autocommit=False,
        )

        if autocommit:
            self.db.commit()
            self.db.refresh(grant)
        return grant

    # ------------------------------------------------------------------
    # Deactivation cascade (§3.6.1 step 3)
    # ------------------------------------------------------------------

    def revoke_all_touching_instance(
        self,
        *,
        admin_id: str,
        instance_id: int,
        audit_ctx: AuditContext,
        autocommit: bool = False,
    ) -> list[SiblingCallGrant]:
        """Cascade-revoke every active grant where ``instance_id``
        appears as caller OR callee. Used by the instance-deactivation
        flow (Architecture §3.6.1 step 3): once an Instance leaves
        active state, no further sibling traffic may dispatch through
        it in either direction, so all touching grants are revoked
        in the same transaction as the deactivation itself.

        Returns the list of grants that were revoked (possibly empty
        if the Instance had no grants). Each revocation emits an
        ACTION_SIBLING_GRANT_REVOKED audit row carrying the cascade
        source.
        """
        touching = self.repo.list_active_touching_instance(
            admin_id=admin_id, instance_id=instance_id,
        )
        revoked: list[SiblingCallGrant] = []
        for grant in touching:
            # Cannot use ``self.revoke`` because that re-fetches by id
            # and would emit identical audit rows; we already have the
            # row and want to embed the cascade source in the audit.
            before_state = grant.approval_state
            self.repo.revoke(grant=grant, autocommit=False)
            self.audit_repo.record(
                ctx=audit_ctx,
                admin_id=admin_id,
                action=ACTION_SIBLING_GRANT_REVOKED,
                resource_type=RESOURCE_SIBLING_CALL_GRANT,
                resource_pk=grant.id,
                resource_natural_id=(
                    f"{grant.caller_instance_id}->{grant.callee_instance_id}"
                ),
                luciel_instance_id=grant.caller_instance_id,
                before={"approval_state": before_state},
                after={
                    "approval_state": APPROVAL_STATE_REVOKED,
                    "revoked_at": (
                        grant.revoked_at.isoformat()
                        if grant.revoked_at is not None
                        else None
                    ),
                    "cascade_source_instance_id": instance_id,
                },
                note=(
                    f"Cascade revoke from instance deactivation "
                    f"(instance_id={instance_id})"
                ),
                autocommit=False,
            )
            revoked.append(grant)

        if autocommit and revoked:
            self.db.commit()
        return revoked
