"""Scope enforcement policy — V2 Admin → Instance surface.

Arc 5 Path A (Commit A4). The V2 doctrine has only two scope levels:
``platform_admin`` (cross-Admin operator) and Admin (one boundary per
billing tenant). There is no Domain layer and no Agent layer. The
legacy three-level (tenant / domain / agent) ScopePolicy is collapsed
to:

* :func:`ScopePolicy.is_platform_admin` — unchanged.
* :func:`ScopePolicy.enforce_tenant_scope` — verifies the caller's
  Admin matches the target Admin (``request.state.admin_id`` is the
  Admin slug post-Revision-B backfill). Cross-Admin access requires
  platform_admin.
* :func:`ScopePolicy.enforce_admin_owns_instance` — verifies the
  caller's Admin owns the target Instance row.
* :func:`ScopePolicy.enforce_luciel_instance_scope` — V2-collapsed
  alias that delegates to :func:`enforce_admin_owns_instance`. Kept
  because the Instance-lifecycle routes in ``app/api/v1/admin.py``
  call it by this legacy name.
* :func:`ScopePolicy.enforce_no_privilege_escalation` — unchanged.
* :func:`ScopePolicy.enforce_action` — unchanged.

Arc 12 EX1d (founder-directed agent_id/domain_id excision) removed
the V1-named delegations that still referenced ``domain_id`` /
``agent_id`` in their signatures (``enforce_domain_scope``,
``enforce_agent_scope``, ``enforce_luciel_creation_scope``). Those
delegations had zero in-tree callers post-EX1a/b/c and were dead
weight in the V2 surface.

Cross-refs: D-arc5-aggressive-cleanup-doctrine-amendment-2026-05-23,
D-arc5-b2-incomplete-instance-service-not-collapsed-2026-05-23 row #4.
"""

from __future__ import annotations

import logging

from typing import Literal

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)

PLATFORM_ADMIN = "platform_admin"
ADMIN = "admin"

# ---------------------------------------------------------------------
# Arc 11 — canonical role names for the Knowledge subsystem role
# matrix per Vision §5.2 + Architecture §3.2.2.
#
# Cleanup C promoted ``ScopeAssignment.role`` from a free-form
# String(100) to a Postgres ENUM (``scope_role``). The four canonical
# names below reference the ``ScopeRole`` Python enum directly; the
# matrix sets use enum members so a stray string can't silently
# match.
# ---------------------------------------------------------------------
from app.models.scope_assignment import ScopeRole

ROLE_ADMIN_OWNER = ScopeRole.ADMIN_OWNER
ROLE_ADMIN_MANAGER = ScopeRole.ADMIN_MANAGER
ROLE_INSTANCE_OPERATOR = ScopeRole.INSTANCE_OPERATOR
ROLE_READ_ONLY_VIEWER = ScopeRole.READ_ONLY_VIEWER

ALL_KNOWLEDGE_ROLES: frozenset[ScopeRole] = frozenset({
    ROLE_ADMIN_OWNER,
    ROLE_ADMIN_MANAGER,
    ROLE_INSTANCE_OPERATOR,
    ROLE_READ_ONLY_VIEWER,
})

# Role-action matrix per Architecture §3.2.2 / ARC11_PLAN.md §0.6.
# list/view → owner + manager + operator (operator scoped)
# edit/delete → owner + manager only
_KNOWLEDGE_ACTION_ROLES: dict[str, frozenset[ScopeRole]] = {
    "list":   frozenset({ROLE_ADMIN_OWNER, ROLE_ADMIN_MANAGER, ROLE_INSTANCE_OPERATOR}),
    "view":   frozenset({ROLE_ADMIN_OWNER, ROLE_ADMIN_MANAGER, ROLE_INSTANCE_OPERATOR}),
    "edit":   frozenset({ROLE_ADMIN_OWNER, ROLE_ADMIN_MANAGER}),
    "delete": frozenset({ROLE_ADMIN_OWNER, ROLE_ADMIN_MANAGER}),
}


class ScopePolicy:
    @staticmethod
    def _caller(request: Request) -> tuple[str | None, list[str]]:
        """Return the caller's ``(admin_id, permissions)`` tuple.

        Arc 12 EX1d: V2 surfaces only ``admin_id`` via
        ``request.state.admin_id`` (the field name carries the legacy
        ``admin_id`` for one release window — Revision B backfilled
        the values to be equal to the V2 Admin slug). The legacy
        three-element ``(admin_id, domain_id, agent_id, permissions)``
        return shape that V1 callsites destructured is gone with the
        delegations that consumed it.
        """
        admin_id = getattr(request.state, "admin_id", None)
        permissions = getattr(request.state, "permissions", []) or []
        return admin_id, permissions

    @classmethod
    def is_platform_admin(cls, request: Request) -> bool:
        _, perms = cls._caller(request)
        return PLATFORM_ADMIN in perms

    # ------------------------------------------------------------------
    # V2 — cross-Admin guard (formerly enforce_tenant_scope)
    # ------------------------------------------------------------------

    @classmethod
    def enforce_tenant_scope(cls, request: Request, target_admin_id: str) -> None:
        """Reject the call if the caller's Admin does not match the
        target Admin (and the caller is not a platform_admin).

        The method keeps its legacy name ``enforce_tenant_scope`` so
        the 24+ callsites in app/api/v1/admin.py keep working through
        B1's route-body rewrite. New code MUST call
        :func:`enforce_admin_scope` (a synonym below).
        """
        caller_admin, perms = cls._caller(request)
        if PLATFORM_ADMIN in perms:
            return
        if caller_admin is None or caller_admin != target_admin_id:
            logger.warning(
                "Scope violation: caller admin=%s tried to access admin=%s",
                caller_admin,
                target_admin_id,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cross-Admin access is not permitted for this key",
            )

    # V2 synonym — new code uses this name.
    enforce_admin_scope = enforce_tenant_scope

    # ------------------------------------------------------------------
    # V2 — admin owns instance predicate
    # ------------------------------------------------------------------

    @classmethod
    def enforce_admin_owns_instance(
        cls,
        request: Request,
        instance,  # app.models.instance.Instance
    ) -> None:
        """Verify the caller's Admin owns this Instance row.

        V2 collapses the legacy three-level
        ``enforce_luciel_instance_scope`` to a flat ``admin_id`` ==
        ``instance.admin_id`` check, with the platform_admin bypass
        preserved.
        """
        if cls.is_platform_admin(request):
            return
        caller_admin, _ = cls._caller(request)
        target_admin = getattr(instance, "admin_id", None)
        if caller_admin is None or target_admin is None or caller_admin != target_admin:
            logger.warning(
                "Scope violation: caller admin=%s tried to access "
                "instance owned by admin=%s",
                caller_admin,
                target_admin,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This key does not own the target Instance",
            )

    # ------------------------------------------------------------------
    # V2 method names — surviving surface.
    # ------------------------------------------------------------------
    #
    # Arc 12 EX1d removed the V1 ``enforce_domain_scope`` /
    # ``enforce_agent_scope`` / ``enforce_luciel_creation_scope``
    # delegations. They had no remaining in-tree callers and existed
    # only to swallow ``domain_id`` / ``agent_id`` arguments from
    # pre-Arc-5 call sites. New code calls
    # :func:`enforce_tenant_scope` (or its
    # :func:`enforce_admin_scope` synonym).

    @classmethod
    def enforce_no_privilege_escalation(
        cls, request: Request, target_permissions: list[str],
    ) -> None:
        """Callers without ``platform_admin`` cannot mint ``platform_admin`` keys."""
        if PLATFORM_ADMIN in (target_permissions or []) and not cls.is_platform_admin(
            request
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only platform_admin may grant platform_admin permission",
            )

    @classmethod
    def enforce_luciel_instance_scope(
        cls,
        request: Request,
        instance,
    ) -> None:
        """V2-collapsed: delegate to :func:`enforce_admin_owns_instance`.

        V2 ``Instance`` has ``instance.admin_id`` — no legacy scope
        attributes. The pre-A4 method body dereferenced removed columns
        and would AttributeError; this V2 replacement reads
        ``instance.admin_id`` directly.
        """
        cls.enforce_admin_owns_instance(request, instance)

    # ------------------------------------------------------------------
    # Arc 11 Step 7 — Knowledge role matrix (Architecture §3.2.2).
    # ------------------------------------------------------------------

    @classmethod
    def _resolve_role_on_instance(
        cls,
        request: Request,
        instance,
    ) -> ScopeRole | None:
        """Look up the caller's role on a specific Instance.

        Resolution order:

          1. Platform admin: returns ``None`` (caller is unscoped; the
             outer ``enforce_role_on_instance`` short-circuits on
             platform_admin before this is called).
          2. ``request.state.scope_assignments`` — populated by the
             auth middleware (Cleanup C item #8). The middleware
             fetches the caller's active scope_assignments for the
             bound admin once per request; this method then picks the
             row whose ``admin_id`` matches the target instance.
          3. ``request.state.role`` — a pre-resolved single-role
             binding (e.g., ``admin_owner`` API keys whose role is
             known at key-mint time). Coerced to ``ScopeRole`` if a
             plain string was set.
          4. Fall back to a one-shot ``scope_assignments`` SELECT.
             Used by test contexts where middleware is mocked.

        Returns the canonical ``ScopeRole`` or ``None`` when no
        active assignment exists for this caller in this Admin scope.
        ``None`` always denies; the gate is fail-closed by
        construction.
        """
        if cls.is_platform_admin(request):
            return None  # bypass — caller handles platform_admin

        target_admin_id = getattr(instance, "admin_id", None)

        # 2. Prefer middleware-populated scope_assignments list.
        assignments = getattr(request.state, "scope_assignments", None)
        if assignments:
            for sa_row in assignments:
                if (
                    getattr(sa_row, "admin_id", None) == target_admin_id
                    and getattr(sa_row, "active", False)
                    and getattr(sa_row, "ended_at", None) is None
                ):
                    return cls._coerce_role(sa_row.role)

        # 3. Pre-resolved single role.
        explicit = getattr(request.state, "role", None)
        if explicit:
            return cls._coerce_role(explicit)

        # 4. Fall back to a per-request DB lookup (test contexts
        #    without middleware).
        actor_user_id = getattr(request.state, "actor_user_id", None)
        if actor_user_id is None or target_admin_id is None:
            return None  # no actor or no target → deny

        try:
            from sqlalchemy import select

            from app.db.session import SessionLocal
            from app.models.scope_assignment import ScopeAssignment
        except Exception:  # pragma: no cover — defensive
            logger.exception("Could not import scope_assignments lookup deps")
            return None

        db = SessionLocal()
        try:
            stmt = (
                select(ScopeAssignment.role)
                .where(
                    ScopeAssignment.user_id == actor_user_id,
                    ScopeAssignment.admin_id == target_admin_id,
                    ScopeAssignment.active.is_(True),
                    ScopeAssignment.ended_at.is_(None),
                )
                .limit(1)
            )
            row = db.execute(stmt).first()
            return cls._coerce_role(row[0]) if row else None
        finally:
            db.close()

    @staticmethod
    def _coerce_role(value) -> ScopeRole | None:
        """Best-effort coercion to ``ScopeRole``.

        Cleanup C made the DB column a Postgres enum, which
        SQLAlchemy materialises as a ``ScopeRole`` member already.
        But pre-Cleanup-C test fixtures, middleware that stamps the
        role as a string, and the ``request.state.role`` fast-path
        may still hand us a plain string — coerce defensively.
        Unknown strings deny by returning ``None``.
        """
        if isinstance(value, ScopeRole):
            return value
        if isinstance(value, str):
            try:
                return ScopeRole(value)
            except ValueError:
                return None
        return None

    @classmethod
    def enforce_role_on_instance(
        cls,
        request: Request,
        instance,
        *,
        allowed_roles: set[str] | frozenset[str],
    ) -> None:
        """Verify the caller holds one of ``allowed_roles`` for this
        Instance's Admin.

        Arc 12b: this is now a thin wrapper around the unified
        :class:`app.policy.permissions.PermissionResolver`. The
        semantics are PRESERVED — for every Free/Pro caller and every
        Enterprise caller holding a locked role, this method returns
        exactly the same decision as before Arc 12b.

        How: the resolver yields the caller's effective permission set
        (locked-role permissions ∪ custom-role permissions). This
        method then computes the union of the locked-role permission
        sets that ``allowed_roles`` describes ("what would a caller
        with any of those roles hold?"), and accepts iff the caller's
        resolved set is a superset of one of the allowed-role sets
        (i.e. there exists a role R ∈ allowed_roles such that the
        caller holds every permission R confers). Custom roles on
        Enterprise can satisfy the gate without literally being one of
        the locked roles — that's the whole point of Arc 12b.

        Three gates in order:

          1. ``platform_admin`` bypasses everything (operator role).
          2. Cross-Admin guard — caller's Admin must own the Instance.
          3. Resolver-based permission satisfaction (subsumes the old
             role lookup + operator-instance scoping; the resolver
             applies the operator-instance scoping internally).

        Raises 403 with a stable detail message on any failure.
        """
        # Lazy import to avoid a circular at module-load (permissions
        # module imports nothing from scope, but ScopePolicy is
        # imported by middlewares).
        from app.policy.permissions import (
            PermissionResolver,
            PLATFORM_ADMIN_ALL,
        )

        # 1. Platform-admin bypass.
        if cls.is_platform_admin(request):
            return

        # 2. Cross-Admin guard first.
        cls.enforce_admin_owns_instance(request, instance)

        # 3. Resolve the caller's effective permission set.
        resolved = PermissionResolver.resolve(request, instance=instance)
        if resolved is PLATFORM_ADMIN_ALL:
            return

        # 4. Build the union of permission sets implied by allowed_roles.
        #    For each role in allowed_roles, look up its permission set
        #    via the seeded locked-role rows. The gate accepts iff the
        #    caller holds the FULL permission set of at least one of the
        #    allowed roles (i.e. they are at least as capable as one of
        #    the listed roles for this action).
        allowed_role_strs: set[str] = set()
        for r in allowed_roles:
            coerced = _role_to_str(r)
            if coerced is not None:
                allowed_role_strs.add(coerced)

        if not allowed_role_strs:
            # Caller specified an empty / nonsense allowed_roles — fail-closed.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No roles configured to permit this action",
            )

        # Fast path: if the resolver yielded an empty set, the caller
        # has no Wall-2 standing under this Admin — 403 with the same
        # detail message the pre-Arc-12b code used.
        if not resolved:
            logger.warning(
                "Role denial: caller has no active scope assignment for "
                "admin=%s instance=%s",
                getattr(instance, "admin_id", None),
                getattr(instance, "id", None),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Caller has no active scope assignment for this Admin"
                ),
            )

        from app.policy.permissions import (
            LOCKED_ROLE_PERMISSIONS_FALLBACK,
        )

        # The locked-role → permission set is platform-managed seed
        # data and is mirrored in the Python constant
        # LOCKED_ROLE_PERMISSIONS_FALLBACK; using the constant avoids a
        # superfluous DB round-trip on every enforce call. The unit
        # tests assert the constant matches the DB seed row-for-row.
        cached = LOCKED_ROLE_PERMISSIONS_FALLBACK

        # Accept iff the caller holds the full permission set of at
        # least one of the allowed roles.
        for role_str in allowed_role_strs:
            required = cached.get(role_str, frozenset())
            if required and required.issubset(resolved):
                return

        # 5. None of the allowed roles' permission sets is satisfied — 403.
        logger.warning(
            "Role denial: caller resolved=%s does not satisfy any of "
            "allowed_roles=%s (instance=%s)",
            sorted(resolved),
            sorted(allowed_role_strs),
            getattr(instance, "id", None),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Caller does not hold the permissions required for this "
                f"action (allowed roles: {sorted(allowed_role_strs)})"
            ),
        )

    @classmethod
    def require_knowledge_role(
        cls,
        request: Request,
        instance,
        action: Literal["list", "view", "edit", "delete"],
    ) -> None:
        """Convenience wrapper for ``enforce_role_on_instance`` that
        maps a knowledge action to its allowed-role set per
        Architecture §3.2.2. See ``_KNOWLEDGE_ACTION_ROLES`` above for
        the canonical matrix."""
        if action not in _KNOWLEDGE_ACTION_ROLES:
            raise ValueError(
                f"Unknown knowledge action {action!r}; expected one of "
                f"{sorted(_KNOWLEDGE_ACTION_ROLES.keys())}"
            )
        cls.enforce_role_on_instance(
            request,
            instance,
            allowed_roles=_KNOWLEDGE_ACTION_ROLES[action],
        )

    # ------------------------------------------------------------------
    # Action-class enforcement (Step 29.y gap-fix C3) — unchanged.
    # ------------------------------------------------------------------

    @classmethod
    def enforce_action(
        cls,
        request: Request,
        *,
        required_permission: str,
        action_label: str,
        instance=None,
    ) -> None:
        """Verify the caller holds ``required_permission`` for action
        ``action_label``. Raises HTTPException(403) on miss.

        ``platform_admin`` satisfies any required permission by design.

        Arc 12b: unified with :class:`PermissionResolver`. The
        decision is made on the SAME resolved permission set that
        :func:`enforce_role_on_instance` consults. Two source-of-truth
        paths are gone — one set is queried; both gates compare
        against it.

        Backwards-compatible behavior: callers that historically
        passed transport-layer permission strings (``"admin"``,
        ``"chat"``, ``"sessions"``) still get a True result if those
        strings appear in ``request.state.permissions`` (the legacy
        API-key permission list). Wall-2 permission keys
        (``"can_..."``) are resolved via the resolver. No existing
        callsite passes a ``can_...`` string today; this widening is
        forward-compatible.
        """
        if not isinstance(required_permission, str) or not required_permission.strip():
            raise ValueError("required_permission must be a non-empty str")
        if any(c in required_permission for c in (",", '"', "\\", "\n", "\r", "\t")):
            raise ValueError(
                f"required_permission {required_permission!r} contains "
                "forbidden character; must be a plain identifier"
            )
        if not isinstance(action_label, str) or not action_label.strip():
            raise ValueError("action_label must be a non-empty str")

        # Platform-admin bypass — preserved. ``perms`` is the
        # transport-layer (API-key) permission tuple from
        # ``request.state.permissions`` (typically
        # ``["admin","chat","sessions"]``).
        _, perms = cls._caller(request)
        if PLATFORM_ADMIN in perms:
            return

        # Transport-layer permission check (legacy surface still
        # honoured for non-``can_*`` permission strings).
        if required_permission in perms:
            return

        # Wall-2 permission check via the unified resolver. After
        # Arc 12b this is the SAME effective permission set
        # ``enforce_role_on_instance`` consults — one authorization
        # source of truth.
        from app.policy.permissions import PermissionResolver

        resolved = PermissionResolver.resolve(request, instance=instance)
        if required_permission in resolved:
            return

        logger.warning(
            "Action denied: action=%s required_permission=%s "
            "transport_perms=%s resolved=%s",
            action_label,
            required_permission,
            sorted(perms),
            sorted(resolved) if not _is_admin_all(resolved) else "<PLATFORM_ADMIN_ALL>",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"This caller does not have permission "
                f"{required_permission!r} required for action {action_label!r}."
            ),
        )


def _role_to_str(value) -> str | None:
    """Coerce a ``ScopeRole`` member or a plain string to its canonical
    string value; return ``None`` on anything else (fail-closed).
    """
    if isinstance(value, ScopeRole):
        return value.value
    if isinstance(value, str):
        if value in (
            "admin_owner",
            "admin_manager",
            "instance_operator",
            "read_only_viewer",
        ):
            return value
    return None


def _is_admin_all(value) -> bool:
    """True when value is the PLATFORM_ADMIN_ALL sentinel."""
    try:
        from app.policy.permissions import PLATFORM_ADMIN_ALL

        return value is PLATFORM_ADMIN_ALL
    except Exception:
        return False
