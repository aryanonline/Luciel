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

        Three gates in order:

          1. ``platform_admin`` bypasses everything (operator role).
          2. ``instance_operator`` is scoped: the operator's assigned
             instance_id (set by auth middleware as
             ``request.state.luciel_instance_id``) must match the
             target instance's id. A manager / owner is not so
             constrained — they hold scope at the Admin level.
          3. The caller's resolved role must be in ``allowed_roles``.

        Raises 403 with a stable detail message on any failure.
        """
        # 1. Platform-admin bypass.
        if cls.is_platform_admin(request):
            return

        # 2. Cross-Admin guard first — must own the Instance before
        #    role-gating means anything. Belt-and-suspenders to the
        #    existing ``enforce_admin_owns_instance`` callers, but
        #    safe to run twice.
        cls.enforce_admin_owns_instance(request, instance)

        # 3. Look up the role.
        role = cls._resolve_role_on_instance(request, instance)
        if role is None:
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

        # 4. Operator-instance scoping. instance_operator is the only
        #    role bound at the Instance level; the other three roles
        #    hold scope at the Admin level and see every Instance
        #    under it.
        if role == ROLE_INSTANCE_OPERATOR:
            caller_instance_id = getattr(
                request.state, "luciel_instance_id", None,
            )
            target_instance_id = getattr(instance, "id", None)
            if (
                caller_instance_id is None
                or caller_instance_id != target_instance_id
            ):
                logger.warning(
                    "Role denial: instance_operator caller bound to "
                    "instance=%s tried instance=%s",
                    caller_instance_id, target_instance_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        "instance_operator scope does not include this Instance"
                    ),
                )

        # 5. Final role check.
        if role not in allowed_roles:
            logger.warning(
                "Role denial: caller role=%s not in allowed=%s",
                role, sorted(allowed_roles),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Role {role!r} is not permitted for this action"
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
    ) -> None:
        """Verify the caller holds ``required_permission`` for action
        ``action_label``. Raises HTTPException(403) on miss.

        ``platform_admin`` satisfies any required permission by design.
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

        _, perms = cls._caller(request)
        if PLATFORM_ADMIN in perms:
            return
        if required_permission in perms:
            return

        logger.warning(
            "Action denied: action=%s required_permission=%s caller_perms=%s",
            action_label,
            required_permission,
            sorted(perms),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"This API key does not have permission "
                f"{required_permission!r} required for action {action_label!r}."
            ),
        )
