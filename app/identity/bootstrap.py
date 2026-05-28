"""Arc 9 C22 -- Consolidated identity bootstrap.

Why this module exists
======================

Every authenticated cookied request hits the same chicken-and-egg
with Wall-1 (Account) FORCE RLS:

  1. We know the User from the session cookie's user_id claim.
  2. We need to know the canonical admin_id, the user's tier, and
     the full list of active scope assignments to (a) set
     ``app.admin_id`` GUC for the rest of the request, (b) enforce
     tier-aware authorisation, and (c) populate billing/admin
     response bodies.
  3. ``scope_assignments`` is FORCE RLS-gated on ``app.admin_id``,
     which is precisely the value we're trying to discover -- so
     direct ORM reads silently return ``[]`` here.

Before C22, two separate SECURITY DEFINER escape hatches (C20 and
C21) papered over this in two specific spots: the auth resolver
and the ScopeAssignmentRepository.list_for_user path. That worked
but created a "find the next callsite, add another SECDEF" pattern
that surfaced as 5 separate symptoms during the first Free signup
demo (401/405/422/500/402).

C22 consolidates that into ONE bootstrap call. The
``IdentityBootstrap`` service runs at most ONCE per HTTP request,
delegates to the SQL function ``public.arc9_c22_bootstrap_identity``
(SECURITY DEFINER, owned by luciel_ops/BYPASSRLS) and returns a
single immutable ``IdentitySnapshot`` carrying:

  * ``canonical_tenant_id`` -- the tenant the request will run under
    (owner-first, then most-recent active scope; same priority as
    every prior auth resolver).
  * ``canonical_tier``      -- the Admin row's tier for the canonical
    tenant. Empty string when the user has no scope -- callers MUST
    treat empty-tier as "no entitlement", not "Free by default".
  * ``active_scopes``       -- the full ordered list of currently-
    active scope assignments (for /billing/me and friends).

Doctrine
========

* This is a DISCOVERY read only. No writes, no side effects, no
  audit. The caller writes audit rows under the resolved admin_id.

* Once the snapshot is taken, the caller is responsible for binding
  ``current_admin_id`` (and ``current_instance_id`` where applicable)
  via the existing ContextVar mechanism in
  ``app.db.tenant_context``. After that the engine's
  ``after_begin`` listener emits ``SET LOCAL app.admin_id = '...'``
  and EVERY subsequent query on the request's session runs under
  normal FORCE RLS. C22 does not bypass RLS for the request as a
  whole -- only for this one discovery read.

* Pure DI: takes a Session, returns a dataclass. No global state.
  Safe to call from middleware, route handlers, or tests.

* The function is invoked positionally to keep psycopg from
  inferring NULL vs '' confusion on the empty-string columns.

Empty / zero-state semantics
============================

* If the user has no active scope assignment at all, the function
  returns 0 rows. ``IdentityBootstrap.resolve`` returns an
  ``IdentitySnapshot`` with ``canonical_tenant_id=""``,
  ``canonical_tier=""``, ``active_scopes=[]``. Callers MUST treat
  this as "no entitlement" -- typically a 401 or 402, never silently
  proceed.

* If the user has scopes but the Admin row backing the canonical
  tenant has been soft-deleted, ``canonical_tier=""`` while
  ``canonical_tenant_id`` is still set. Callers SHOULD still gate
  on tier (the entitlement layer treats empty tier as no entitlement).

Failure semantics
=================

Any DB exception from the SECDEF call propagates up. Callers in the
HTTP path are expected to map it to a 500 with structured logging;
this module does not catch / swallow / log per the repository
discipline (no HTTP exceptions, no broad excepts).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import text as _sa_text
from sqlalchemy.orm import Session

from app.models.scope_assignment import EndReason, ScopeAssignment

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Snapshot dataclass
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class IdentitySnapshot:
    """Immutable result of one bootstrap discovery read.

    Returned by ``IdentityBootstrap.resolve``. Treat as read-only;
    callers must not mutate the embedded ScopeAssignment instances
    (they are transient, not session-attached -- mutations would
    silently no-op and confuse downstream readers).
    """

    user_id: uuid.UUID
    canonical_tenant_id: str  # '' when user has no active scope
    canonical_tier: str       # '' when no scope OR Admin missing
    active_scopes: list[ScopeAssignment] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Convenience accessors -- callers should use these instead of
    # poking at the fields directly so future shape changes are local.
    # ------------------------------------------------------------------

    @property
    def has_scope(self) -> bool:
        """True iff the user has at least one active scope assignment."""
        return bool(self.canonical_tenant_id) and bool(self.active_scopes)

    @property
    def canonical_role(self) -> str:
        """Role on the canonical tenant ('' if no scope).

        Picks the scope whose admin_id matches canonical_tenant_id;
        if multiple match (shouldn't happen in steady state), prefers
        the ``admin_owner`` role. Returns the role's ``.value``
        string (Cleanup C promoted the column to the ``scope_role``
        PG enum).
        """
        if not self.canonical_tenant_id:
            return ""
        on_canonical = [
            s for s in self.active_scopes
            if s.admin_id == self.canonical_tenant_id
        ]
        if not on_canonical:
            return ""
        owners = [
            s for s in on_canonical
            if getattr(s.role, "value", s.role) == "admin_owner"
            or s.role == "admin_owner"
        ]
        chosen = (owners[0] if owners else on_canonical[0]).role
        return getattr(chosen, "value", chosen) or ""


# ---------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------


class IdentityBootstrap:
    """Single source of truth for pre-tenant-GUC identity discovery.

    Wraps the C22 SECDEF function. Construct with a Session, call
    ``resolve(user_id)``.
    """

    # Columns returned by arc9_c22_bootstrap_identity, in order. Kept
    # as a class constant so the parsing code is co-located with the
    # SQL select-list shape -- a desync between them is the kind of
    # silent bug we want a code review to catch.
    _COLUMNS = (
        "canonical_tenant_id",
        "canonical_tier",
        "scope_assignment_id",
        "admin_id",
        "domain_id",
        "role",
        "started_at",
        "ended_at",
        "ended_reason",
        "ended_note",
        "ended_by_api_key_id",
        "active",
    )

    def __init__(self, db: Session) -> None:
        self.db = db

    def resolve(self, user_id: uuid.UUID) -> IdentitySnapshot:
        """Run the bootstrap discovery read and assemble a snapshot."""
        rows = self.db.execute(
            _sa_text(
                "SELECT canonical_tenant_id, canonical_tier, "
                "scope_assignment_id, admin_id, domain_id, role, "
                "started_at, ended_at, ended_reason, ended_note, "
                "ended_by_api_key_id, active "
                "FROM public.arc9_c22_bootstrap_identity(:uid)"
            ),
            {"uid": str(user_id)},
        ).all()

        if not rows:
            # Genuine zero-state: user has no active scope.
            return IdentitySnapshot(
                user_id=user_id,
                canonical_tenant_id="",
                canonical_tier="",
                active_scopes=[],
            )

        # The two header columns repeat on every row by SQL design
        # (no PG support for "one scalar + a set" return shapes).
        # Pluck them from row 0 and treat the rest of the columns
        # as the scope-assignment list.
        canonical_tenant_id = rows[0].canonical_tenant_id or ""
        canonical_tier = rows[0].canonical_tier or ""

        scopes: list[ScopeAssignment] = []
        for r in rows:
            scopes.append(
                _hydrate_scope_assignment(
                    scope_id=r.scope_assignment_id,
                    user_id=user_id,
                    admin_id=r.admin_id,
                    domain_id=r.domain_id,
                    role=r.role,
                    started_at=r.started_at,
                    ended_at=r.ended_at,
                    ended_reason=r.ended_reason,
                    ended_note=r.ended_note,
                    ended_by_api_key_id=r.ended_by_api_key_id,
                    active=r.active,
                )
            )

        return IdentitySnapshot(
            user_id=user_id,
            canonical_tenant_id=canonical_tenant_id,
            canonical_tier=canonical_tier,
            active_scopes=scopes,
        )


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _hydrate_scope_assignment(
    *,
    scope_id: uuid.UUID,
    user_id: uuid.UUID,
    admin_id: str,
    domain_id: str,
    role: str,
    started_at: datetime,
    ended_at: Optional[datetime],
    ended_reason: Optional[str],
    ended_note: Optional[str],
    ended_by_api_key_id: Optional[int],
    active: bool,
) -> ScopeAssignment:
    """Build a transient (non-session-attached) ScopeAssignment row.

    The instance carries the same shape callers expect from the ORM
    path, but is intentionally NOT added to the session: a SECDEF
    discovery read must not enrol rows into a tracking pool where
    they could be flushed back under the eventual tenant GUC.
    """
    return ScopeAssignment(
        id=scope_id,
        user_id=user_id,
        admin_id=admin_id,
        domain_id=domain_id,
        role=role,
        started_at=started_at,
        ended_at=ended_at,
        ended_reason=(
            EndReason(ended_reason) if ended_reason is not None else None
        ),
        ended_note=ended_note,
        ended_by_api_key_id=ended_by_api_key_id,
        active=active,
    )


__all__ = ["IdentityBootstrap", "IdentitySnapshot"]
