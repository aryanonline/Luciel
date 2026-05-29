"""SiblingCallGrant repository — Arc 12 WU4.

Pure CRUD against the ``sibling_call_grants`` table. The grant-
authoring API, the deactivation-cascade hook, and (Arc 12 WU5) the
runtime dispatch lookup all read through this repo.

Scope of responsibility
-----------------------
* Author / list / get / approve / reject / revoke rows scoped by
  ``admin_id``.
* The Wall-2 cross-Instance role check is enforced at the API
  layer, not here. This repo trusts its caller has already gated.
* No HTTP exceptions — callers raise them.

State machine
-------------

::

    (author, Pro)
       │
       ▼
     live ──── revoke ─────▶ revoked  (terminal)
                                  ▲
    (author, Enterprise)          │
       │                          │
       ▼                          │
   pending_approval ── approve ──▶ live (→ revoke as above)
       │                          │
       └────── reject ────────────┘
       │
       └────── revoke ──────▶ revoked
                                  ▲
                                  │
                  deactivation cascade
                  (any non-revoked grant touching the
                   deactivated instance, caller or callee)

The partial unique index on
``(admin_id, caller_instance_id, callee_instance_id) WHERE
approval_state <> 'revoked'`` is the integrity backstop — at most
one non-revoked row per triple at any moment. Revoke + re-author
works because the revoked row is excluded from the index.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from app.models.sibling_call_grant import (
    APPROVAL_STATE_LIVE,
    APPROVAL_STATE_PENDING,
    APPROVAL_STATE_REVOKED,
    SiblingCallGrant,
)

logger = logging.getLogger(__name__)


class SiblingCallGrantRepository:
    """Data-access layer for sibling-Luciel composition grants.

    All read methods filter on ``admin_id`` — Wall-1. RLS at the DB
    layer enforces it a second time; the explicit filter at the
    application layer keeps service-layer callers honest even in
    test environments that lack the RLS GUC.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_by_id(
        self, *, admin_id: str, grant_id: int
    ) -> Optional[SiblingCallGrant]:
        """Look up a grant by PK, scoped to admin. None if not found."""
        stmt = (
            select(SiblingCallGrant)
            .where(
                and_(
                    SiblingCallGrant.id == grant_id,
                    SiblingCallGrant.admin_id == admin_id,
                )
            )
            .limit(1)
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def get_active(
        self,
        *,
        admin_id: str,
        caller_instance_id: int,
        callee_instance_id: int,
    ) -> Optional[SiblingCallGrant]:
        """Return the non-revoked grant for the triple if one exists.

        "Active" here means ``approval_state != 'revoked'`` — either
        ``live`` or ``pending_approval``. The partial unique index
        guarantees at most one such row.
        """
        stmt = (
            select(SiblingCallGrant)
            .where(
                and_(
                    SiblingCallGrant.admin_id == admin_id,
                    SiblingCallGrant.caller_instance_id == caller_instance_id,
                    SiblingCallGrant.callee_instance_id == callee_instance_id,
                    SiblingCallGrant.approval_state != APPROVAL_STATE_REVOKED,
                )
            )
            .limit(1)
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def get_live(
        self,
        *,
        admin_id: str,
        caller_instance_id: int,
        callee_instance_id: int,
    ) -> Optional[SiblingCallGrant]:
        """WU5 runtime-dispatch lookup. Returns the row iff it is
        ``approval_state='live'`` for the triple."""
        stmt = (
            select(SiblingCallGrant)
            .where(
                and_(
                    SiblingCallGrant.admin_id == admin_id,
                    SiblingCallGrant.caller_instance_id == caller_instance_id,
                    SiblingCallGrant.callee_instance_id == callee_instance_id,
                    SiblingCallGrant.approval_state == APPROVAL_STATE_LIVE,
                )
            )
            .limit(1)
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def list_for_admin(
        self,
        *,
        admin_id: str,
        approval_states: Sequence[str] | None = None,
        caller_instance_id: int | None = None,
        callee_instance_id: int | None = None,
    ) -> list[SiblingCallGrant]:
        """List grants for an Admin, optionally filtered.

        Default: all grants (any state). Pass ``approval_states`` to
        narrow (e.g. only live, or only pending_approval). Pass
        ``caller_instance_id`` / ``callee_instance_id`` to scope to
        one side of an edge — useful for the UI "what grants does
        this Instance have authored" view.
        """
        conditions = [SiblingCallGrant.admin_id == admin_id]
        if approval_states:
            conditions.append(
                SiblingCallGrant.approval_state.in_(tuple(approval_states))
            )
        if caller_instance_id is not None:
            conditions.append(
                SiblingCallGrant.caller_instance_id == caller_instance_id
            )
        if callee_instance_id is not None:
            conditions.append(
                SiblingCallGrant.callee_instance_id == callee_instance_id
            )
        stmt = (
            select(SiblingCallGrant)
            .where(and_(*conditions))
            .order_by(SiblingCallGrant.created_at.desc())
        )
        return list(self.db.execute(stmt).scalars())

    # ------------------------------------------------------------------
    # Write: author
    # ------------------------------------------------------------------

    def author(
        self,
        *,
        admin_id: str,
        caller_instance_id: int,
        callee_instance_id: int,
        granted_by_user_id: uuid.UUID,
        initial_state: str,
        autocommit: bool = False,
    ) -> SiblingCallGrant:
        """Insert a new grant row.

        ``initial_state`` is ``'live'`` for Pro-tier authors and
        ``'pending_approval'`` for Enterprise — the service layer
        resolves the tier and passes the right value. Caller is
        expected to have already verified no active row exists for
        the triple (the partial unique index will raise
        IntegrityError otherwise — caller may catch and translate).
        """
        if initial_state not in (APPROVAL_STATE_LIVE, APPROVAL_STATE_PENDING):
            raise ValueError(
                f"initial_state must be 'live' or 'pending_approval'; "
                f"got {initial_state!r}"
            )
        row = SiblingCallGrant(
            admin_id=admin_id,
            caller_instance_id=caller_instance_id,
            callee_instance_id=callee_instance_id,
            granted_by_user_id=granted_by_user_id,
            approval_state=initial_state,
        )
        self.db.add(row)
        if autocommit:
            self.db.commit()
            self.db.refresh(row)
        else:
            self.db.flush()
        return row

    # ------------------------------------------------------------------
    # Write: state transitions
    # ------------------------------------------------------------------

    def approve(
        self,
        *,
        grant: SiblingCallGrant,
        approved_by_user_id: uuid.UUID,
        autocommit: bool = False,
    ) -> SiblingCallGrant:
        """Flip pending → live. Caller MUST have verified the row is
        in ``pending_approval`` state first; this method does not
        re-check (the service layer is the policy chokepoint)."""
        now = datetime.now(timezone.utc)
        grant.approval_state = APPROVAL_STATE_LIVE
        grant.approved_by_user_id = approved_by_user_id
        grant.approved_at = now
        grant.updated_at = now
        if autocommit:
            self.db.commit()
            self.db.refresh(grant)
        else:
            self.db.flush()
        return grant

    def reject(
        self,
        *,
        grant: SiblingCallGrant,
        rejected_by_user_id: uuid.UUID,
        autocommit: bool = False,
    ) -> SiblingCallGrant:
        """Flip pending → revoked (rejected). Distinct from revoke
        only in the verb-level audit row the service writes; the
        DB-level mutation is the same (approval_state='revoked',
        revoked_at=now). ``rejected_by_user_id`` is recorded as
        ``approved_by_user_id`` so the column carries the
        adjudicator's identity for both approve and reject — the
        verb in the audit row disambiguates the direction."""
        now = datetime.now(timezone.utc)
        grant.approval_state = APPROVAL_STATE_REVOKED
        grant.approved_by_user_id = rejected_by_user_id
        grant.approved_at = now
        grant.revoked_at = now
        grant.updated_at = now
        if autocommit:
            self.db.commit()
            self.db.refresh(grant)
        else:
            self.db.flush()
        return grant

    def revoke(
        self,
        *,
        grant: SiblingCallGrant,
        autocommit: bool = False,
    ) -> SiblingCallGrant:
        """Flip live (or pending) → revoked. Terminal. Caller has
        already verified the row is not already revoked."""
        now = datetime.now(timezone.utc)
        grant.approval_state = APPROVAL_STATE_REVOKED
        grant.revoked_at = now
        grant.updated_at = now
        if autocommit:
            self.db.commit()
            self.db.refresh(grant)
        else:
            self.db.flush()
        return grant

    # ------------------------------------------------------------------
    # Write: deactivation cascade
    # ------------------------------------------------------------------

    def list_active_touching_instance(
        self,
        *,
        admin_id: str,
        instance_id: int,
    ) -> list[SiblingCallGrant]:
        """Return all non-revoked grants where ``instance_id`` appears
        as caller OR callee. Used by the instance-deactivation cascade
        (Architecture §3.6.1 step 3) to enumerate the rows the worker
        needs to revoke. Scoped to ``admin_id`` because instance ids
        are globally unique but the Wall-1 + RLS posture insists on
        explicit scoping at the application layer.
        """
        stmt = (
            select(SiblingCallGrant)
            .where(
                and_(
                    SiblingCallGrant.admin_id == admin_id,
                    SiblingCallGrant.approval_state != APPROVAL_STATE_REVOKED,
                    or_(
                        SiblingCallGrant.caller_instance_id == instance_id,
                        SiblingCallGrant.callee_instance_id == instance_id,
                    ),
                )
            )
            .order_by(SiblingCallGrant.id.asc())
        )
        return list(self.db.execute(stmt).scalars())
