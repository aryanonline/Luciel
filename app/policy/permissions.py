"""Arc 12b — unified permission resolver.

Architecture §3.7.2: "Permission-based custom roles (Enterprise, Arc 12b)".

ONE authorization source of truth. Both :func:`ScopePolicy
.enforce_role_on_instance` and :func:`ScopePolicy.enforce_action` consult
this module — :class:`PermissionResolver`.

Inputs:
  * the FastAPI ``Request`` (carries the authenticated context — admin_id,
    actor_user_id, scope_assignments, permissions, etc.)
  * an optional target ``Instance`` (when the action is Instance-scoped;
    knowledge / tool / sibling-grant routes pass it; pure admin-scoped
    routes do not).

Output:
  * a ``frozenset[str]`` of permission keys the caller effectively holds
    in that context.

Resolution algorithm
--------------------

  1. ``platform_admin`` short-circuit: a caller whose transport-layer
     permissions list (``request.state.permissions``) contains
     ``"platform_admin"`` is the cross-Admin operator. Resolver returns
     the special sentinel :data:`PLATFORM_ADMIN_ALL` which compares
     equal to any required permission via :func:`permission_set_satisfies`.

  2. Locked-role binding: read ``request.state.scope_assignments``
     (populated by the auth middleware) — the user's active
     ``scope_assignments`` rows under the bound Admin. For every row
     whose ``admin_id`` matches the target Instance's admin (or the
     bound admin_id when no Instance is given), apply the
     operator-instance scoping rule (an ``instance_operator``
     assignment only contributes its permissions when the call targets
     that bound Instance). Resolve each contributing row's locked-role
     name → permission set via the cached ``role_permissions`` lookup.

  3. Custom-role binding (Enterprise additive): read the user's
     ``user_role_assignments`` rows under the bound Admin (NOT revoked)
     and union in the permissions for each. Custom-role assignments
     respect the ``scope_type`` field — an ``instance_specific``
     assignment only contributes when the call targets that
     Instance.

  4. Pre-resolved single role fast-path: ``request.state.role`` set
     (e.g. API-key keys minted with a role). Coerce to locked role
     and union its permission set — but only for the bound Admin.

  5. Caller is API-key only with no role/scope_assignment binding —
     resolved permission set is empty. The legacy transport-layer
     permissions list (``["admin","chat","sessions"]``) is NOT
     mapped into the Wall-2 permission catalog (those are transport-
     plane permissions, not Wall-2 permissions). Wall-2 enforcement
     deny-by-default in that case.

Zero behavioral change on Free/Pro
----------------------------------

The locked-role → permission set seed (planted by alembic
``arc12b_custom_roles_permission_model``) reproduces today's role
matrix exactly. Free and Pro tenants have no ``user_role_assignments``
rows — the resolver returns exactly the locked-role permission set,
which is identical (by construction of the seed) to the role gate
that ``enforce_role_on_instance`` enforced pre-Arc-12b.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

if TYPE_CHECKING:  # pragma: no cover
    from app.models.instance import Instance

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Permission-key constants. Keep in sync with the catalog seed in
# alembic/versions/arc12b_custom_roles_permission_model.py.
# ---------------------------------------------------------------------

PERM_VIEW_KNOWLEDGE = "can_view_knowledge"
PERM_EDIT_KNOWLEDGE = "can_edit_knowledge"
PERM_DELETE_KNOWLEDGE = "can_delete_knowledge"
PERM_INGEST_KNOWLEDGE = "can_ingest_knowledge"
PERM_VIEW_TOOLS = "can_view_tools"
PERM_CONFIGURE_TOOLS = "can_configure_tools"
PERM_CONFIGURE_CHANNELS = "can_configure_channels"
PERM_CONFIGURE_CONNECTIONS = "can_configure_connections"
PERM_VIEW_AUDIT_LOG = "can_view_audit_log"
PERM_VIEW_BILLING = "can_view_billing"
PERM_AUTHOR_SIBLING_GRANTS = "can_author_sibling_grants"
PERM_APPROVE_SIBLING_GRANTS = "can_approve_sibling_grants"
PERM_AUTHOR_CUSTOM_ROLES = "can_author_custom_roles"
PERM_ASSIGN_ROLES = "can_assign_roles"

ALL_PERMISSIONS: frozenset[str] = frozenset(
    {
        PERM_VIEW_KNOWLEDGE,
        PERM_EDIT_KNOWLEDGE,
        PERM_DELETE_KNOWLEDGE,
        PERM_INGEST_KNOWLEDGE,
        PERM_VIEW_TOOLS,
        PERM_CONFIGURE_TOOLS,
        PERM_CONFIGURE_CHANNELS,
        PERM_CONFIGURE_CONNECTIONS,
        PERM_VIEW_AUDIT_LOG,
        PERM_VIEW_BILLING,
        PERM_AUTHOR_SIBLING_GRANTS,
        PERM_APPROVE_SIBLING_GRANTS,
        PERM_AUTHOR_CUSTOM_ROLES,
        PERM_ASSIGN_ROLES,
    }
)


# Locked-role → permission set fallback that EXACTLY mirrors the
# alembic seed in ``arc12b_custom_roles_permission_model.py``. The
# resolver prefers the live DB seed (lookup via SELECT against
# role_permissions JOIN permissions), but falls back to this Python
# constant when no DB is reachable (unit tests against SQLite, missing
# pgvector at import time, etc.). The migration's
# ``LOCKED_ROLE_PERMISSIONS`` mapping is the canonical source — this
# constant is a mirror, and the test suite asserts the two match
# row-for-row so a stale fallback fails CI.
LOCKED_ROLE_PERMISSIONS_FALLBACK: dict[str, frozenset[str]] = {
    "admin_owner": frozenset(ALL_PERMISSIONS),
    "admin_manager": frozenset(
        ALL_PERMISSIONS
        - {
            PERM_APPROVE_SIBLING_GRANTS,
            PERM_AUTHOR_CUSTOM_ROLES,
            PERM_VIEW_BILLING,
            PERM_ASSIGN_ROLES,
        }
    ),
    "instance_operator": frozenset(
        {
            PERM_VIEW_KNOWLEDGE,
            PERM_VIEW_TOOLS,
        }
    ),
    "read_only_viewer": frozenset(
        {
            PERM_VIEW_TOOLS,
        }
    ),
}


# Sentinel that satisfies ANY required permission. Used as the resolved
# set for platform_admin so callsites don't need a separate branch.
class _PlatformAdminAll(frozenset):
    """Special frozenset that ``__contains__`` returns True for any str.

    A subclass so existing ``in`` membership checks just work. Used as
    the resolved permission set for ``platform_admin`` callers.
    """

    def __contains__(self, item) -> bool:  # type: ignore[override]
        return isinstance(item, str)

    def __repr__(self) -> str:  # pragma: no cover
        return "<PLATFORM_ADMIN_ALL>"


PLATFORM_ADMIN_ALL = _PlatformAdminAll()


# Cache key on request.state so we don't re-query for the same call.
_LOCKED_PERMS_CACHE_ATTR = "_arc12b_locked_role_permissions_by_role"


# ---------------------------------------------------------------------
# Helpers — locked-role permission set lookup. Cached per request.
# ---------------------------------------------------------------------


def _locked_role_permissions_map(db: Session | None = None) -> dict[str, frozenset[str]]:
    """Return ``{locked_role: frozenset(permission_keys)}`` for all four
    locked roles.

    Prefers a live DB read (one SELECT against ``role_permissions`` JOIN
    ``permissions``) so seed updates take effect without an app
    restart, but falls back to the Python constant
    :data:`LOCKED_ROLE_PERMISSIONS_FALLBACK` when no DB is reachable.
    The two are kept identical by an explicit unit test.
    """
    from sqlalchemy import text as sa_text

    if db is None:
        return LOCKED_ROLE_PERMISSIONS_FALLBACK

    try:
        rows = db.execute(
            sa_text(
                """
                SELECT rp.locked_role, p.key
                FROM role_permissions rp
                JOIN permissions p ON p.id = rp.permission_id
                WHERE rp.locked_role IS NOT NULL
                """
            )
        ).fetchall()
    except Exception:  # noqa: BLE001
        # Most likely: the SELECT was issued inside an already-aborted
        # transaction on a pooled connection, or the role_permissions /
        # permissions tables are not present (SQLite test fixture).
        # Fall back to the static map so the resolver still produces a
        # correct answer.
        try:
            db.rollback()
        except Exception:
            pass
        return LOCKED_ROLE_PERMISSIONS_FALLBACK

    if not rows:
        return LOCKED_ROLE_PERMISSIONS_FALLBACK

    by_role: dict[str, set[str]] = {}
    for r in rows:
        by_role.setdefault(r.locked_role, set()).add(r.key)
    return {role: frozenset(keys) for role, keys in by_role.items()}


def _custom_role_permissions(db: Session, custom_role_ids: list[int]) -> dict[int, frozenset[str]]:
    """Return ``{custom_role_id: frozenset(permission_keys)}``.

    Single SELECT joining role_permissions → permissions filtered by
    ``custom_role_id IN (...)``. RLS on this table is NOT applied to
    role_permissions itself (locked-role rows have NULL admin_id), but
    the custom_role_id space is already fenced by the
    user_role_assignments query the caller made — only ids returned
    from that query reach this function.
    """
    if not custom_role_ids:
        return {}
    from sqlalchemy import text as sa_text

    rows = db.execute(
        sa_text(
            """
            SELECT rp.custom_role_id, p.key
            FROM role_permissions rp
            JOIN permissions p ON p.id = rp.permission_id
            WHERE rp.custom_role_id = ANY(:ids)
            """
        ),
        {"ids": custom_role_ids},
    ).fetchall()

    by_id: dict[int, set[str]] = {}
    for r in rows:
        by_id.setdefault(r.custom_role_id, set()).add(r.key)
    return {cid: frozenset(keys) for cid, keys in by_id.items()}


def _open_db_session() -> Session:
    """Open a one-shot Session for the resolver when no request-scoped
    session is reachable from request.state. Closed by the caller.
    """
    from app.db.session import SessionLocal

    return SessionLocal()


def _get_request_db(request: Request) -> Session | None:
    """Return a session the request has already opened, if any.

    Auth middlewares do NOT stash a session on request.state. The
    resolver opens its own short-lived session in that case.
    """
    return None


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------


class PermissionResolver:
    """Unified resolver — one source of truth for Wall-2 enforcement."""

    @staticmethod
    def is_platform_admin(request: Request) -> bool:
        perms = getattr(request.state, "permissions", None) or ()
        return "platform_admin" in perms

    @classmethod
    def resolve(
        cls,
        request: Request,
        *,
        instance: "Instance | None" = None,
    ) -> frozenset[str]:
        """Compute the caller's effective permission set.

        Args:
          request: the FastAPI request.
          instance: optional target Instance — when provided, the
            resolver applies operator-instance scoping and the
            instance-specific custom-role scoping.

        Returns:
          A frozenset[str] of permission keys. For ``platform_admin``
          callers, returns :data:`PLATFORM_ADMIN_ALL` which contains
          every string.
        """
        if cls.is_platform_admin(request):
            return PLATFORM_ADMIN_ALL

        admin_id = getattr(request.state, "admin_id", None)
        if admin_id is None:
            # No bound Admin — no permissions. Wall-2 fail-closed.
            return frozenset()

        # The resolver's role→permission lookup uses
        # :data:`LOCKED_ROLE_PERMISSIONS_FALLBACK` directly — no DB read
        # is required to resolve locked roles, which is the only path
        # Free/Pro and locked-role Enterprise callers exercise. A DB
        # session is only opened when there is an actor_user_id that
        # might have ``user_role_assignments`` rows AND we couldn't
        # resolve a role from middleware-supplied data.
        actor_user_id = getattr(request.state, "actor_user_id", None)
        need_db = actor_user_id is not None

        db: Session | None = None
        try:
            if need_db:
                try:
                    db = _open_db_session()
                except Exception:  # noqa: BLE001
                    db = None
            return cls._resolve_with_db(
                db=db,
                request=request,
                admin_id=admin_id,
                instance=instance,
            )
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    @classmethod
    def _resolve_with_db(
        cls,
        *,
        db: Session | None,
        request: Request,
        admin_id: str,
        instance: "Instance | None",
    ) -> frozenset[str]:
        target_admin_id = (
            getattr(instance, "admin_id", None) if instance is not None else admin_id
        )
        if target_admin_id is None or target_admin_id != admin_id:
            # The caller's bound admin must match the target Admin. The
            # outer cross-Admin guard (enforce_admin_owns_instance) will
            # 403 — this is belt-and-suspenders so the resolver itself
            # is fail-closed.
            return frozenset()

        target_instance_id = getattr(instance, "id", None) if instance is not None else None

        # ------------------------------------------------------------------
        # (a) Locked-role contributions from ``scope_assignments`` rows.
        # ------------------------------------------------------------------
        locked_role_map = _locked_role_permissions_map(db)

        roles_contributing: set[str] = set()

        # Middleware-populated scope_assignments list. Both auth
        # middlewares stamp this attribute (api-key path empty list,
        # cookie path populated).
        assignments = getattr(request.state, "scope_assignments", None) or ()
        for sa_row in assignments:
            if getattr(sa_row, "admin_id", None) != admin_id:
                continue
            if not getattr(sa_row, "active", False):
                continue
            if getattr(sa_row, "ended_at", None) is not None:
                continue
            role_value = getattr(sa_row, "role", None)
            role_str = _coerce_role_str(role_value)
            if role_str is None:
                continue
            # Operator-instance scoping: an instance_operator
            # assignment only contributes when the call targets that
            # bound Instance. The bound-instance id is stamped onto
            # request.state.luciel_instance_id by the auth middleware
            # (api-key keys minted with luciel_instance_id) — that's
            # the source of truth for "which Instance is the operator
            # bound to right now".
            if role_str == "instance_operator":
                if target_instance_id is None:
                    # Admin-scoped call (no target Instance) — the
                    # operator's role does not contribute. (Operators
                    # have no Admin-wide authority.)
                    continue
                bound_instance_id = getattr(
                    request.state, "luciel_instance_id", None
                )
                if (
                    bound_instance_id is None
                    or bound_instance_id != target_instance_id
                ):
                    continue
            roles_contributing.add(role_str)

        # Pre-resolved single-role fast path (request.state.role).
        explicit_role = getattr(request.state, "role", None)
        if explicit_role is not None:
            role_str = _coerce_role_str(explicit_role)
            if role_str is not None:
                # Same operator-instance scoping rule applies.
                if role_str == "instance_operator":
                    if target_instance_id is not None:
                        bound_instance_id = getattr(
                            request.state, "luciel_instance_id", None
                        )
                        if (
                            bound_instance_id is not None
                            and bound_instance_id == target_instance_id
                        ):
                            roles_contributing.add(role_str)
                    # else: admin-scoped call — operator role doesn't contribute.
                else:
                    roles_contributing.add(role_str)

        # If no scope_assignments are available at all, fall back to a
        # per-request DB lookup so the resolver works under test contexts
        # that mock middleware. Matches ScopePolicy._resolve_role_on_instance
        # behavior.
        if not roles_contributing and not assignments and db is not None:
            actor_user_id = getattr(request.state, "actor_user_id", None)
            if actor_user_id is not None:
                fallback_role = _fallback_role_lookup(
                    db=db,
                    user_id=actor_user_id,
                    admin_id=admin_id,
                )
                if fallback_role is not None:
                    role_str = _coerce_role_str(fallback_role)
                    if role_str is not None:
                        if role_str == "instance_operator":
                            if target_instance_id is not None:
                                bound_instance_id = getattr(
                                    request.state, "luciel_instance_id", None
                                )
                                if (
                                    bound_instance_id is not None
                                    and bound_instance_id == target_instance_id
                                ):
                                    roles_contributing.add(role_str)
                        else:
                            roles_contributing.add(role_str)

        resolved: set[str] = set()
        for role_str in roles_contributing:
            resolved.update(locked_role_map.get(role_str, frozenset()))

        # ------------------------------------------------------------------
        # (b) Custom-role contributions from ``user_role_assignments``.
        # ------------------------------------------------------------------
        actor_user_id = getattr(request.state, "actor_user_id", None)
        if actor_user_id is not None and db is not None:
            custom_assignments = _load_user_role_assignments(
                db=db,
                user_id=actor_user_id,
                admin_id=admin_id,
            )
            custom_role_ids: list[int] = []
            locked_assignment_roles: list[str] = []
            for ura_locked_role, ura_custom_role_id, ura_scope_type, ura_instance_id in custom_assignments:
                # Apply scope_type filtering.
                if ura_scope_type == "instance_specific":
                    if (
                        target_instance_id is None
                        or ura_instance_id is None
                        or ura_instance_id != target_instance_id
                    ):
                        continue
                # else: all_instances — always contributes within the
                # bound Admin (already wall-1-fenced by the query's
                # admin_id filter).
                if ura_custom_role_id is not None:
                    custom_role_ids.append(ura_custom_role_id)
                elif ura_locked_role is not None:
                    locked_assignment_roles.append(ura_locked_role)

            # Locked-role rows under user_role_assignments (Enterprise
            # additive surface; permissions union with scope_assignments
            # locked-role rows).
            for role_str in locked_assignment_roles:
                resolved.update(locked_role_map.get(role_str, frozenset()))

            # Custom-role rows.
            if custom_role_ids:
                custom_map = _custom_role_permissions(db, custom_role_ids)
                for cid in custom_role_ids:
                    resolved.update(custom_map.get(cid, frozenset()))

        return frozenset(resolved)


def _coerce_role_str(value) -> str | None:
    """Coerce a ``ScopeRole`` or string to its canonical string value.
    Returns ``None`` for an unknown value (fail-closed deny).
    """
    if value is None:
        return None
    # ScopeRole(str, Enum) — both .value and str(member) are the string.
    try:
        from app.models.scope_assignment import ScopeRole

        if isinstance(value, ScopeRole):
            return value.value
    except Exception:  # pragma: no cover — defensive
        pass
    if isinstance(value, str):
        if value in (
            "admin_owner",
            "admin_manager",
            "instance_operator",
            "read_only_viewer",
        ):
            return value
        return None
    return None


def _fallback_role_lookup(*, db: Session, user_id, admin_id: str):
    """Per-request fallback when middleware did not populate
    ``request.state.scope_assignments``. Returns the first active
    locked role or None.
    """
    try:
        from app.models.scope_assignment import ScopeAssignment
    except Exception:  # pragma: no cover
        return None

    row = db.execute(
        select(ScopeAssignment.role)
        .where(
            ScopeAssignment.user_id == user_id,
            ScopeAssignment.admin_id == admin_id,
            ScopeAssignment.active.is_(True),
            ScopeAssignment.ended_at.is_(None),
        )
        .limit(1)
    ).first()
    return row[0] if row else None


def _load_user_role_assignments(
    *, db: Session, user_id, admin_id: str
) -> list[tuple[str | None, int | None, str, int | None]]:
    """Return active (locked_role, custom_role_id, scope_type, instance_id)
    rows for the user under the bound Admin.

    Fail-soft: returns ``[]`` on any DB error (aborted transaction, the
    table doesn't exist, SQLite test fixture, etc.). The resolver
    proceeds with locked-role data only. Wall-2 stays fail-closed
    because the absence of an assignment is itself a deny in the
    permission-set computation.
    """
    from sqlalchemy import text as sa_text

    # Ensure GUC is set so RLS doesn't fence us out when this Session
    # is a fresh one not bound to a request. Best-effort SET LOCAL.
    try:
        # Open an explicit transaction if the connection isn't already
        # in one (psycopg autocommit-off Sessions are in one by default).
        db.execute(sa_text("SET LOCAL app.admin_id = :aid"), {"aid": admin_id})
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        # Re-issue SET LOCAL on a fresh transaction; if it fails again,
        # bail and return empty.
        try:
            db.execute(sa_text("SET LOCAL app.admin_id = :aid"), {"aid": admin_id})
        except Exception:
            return []

    try:
        rows = db.execute(
            sa_text(
                """
                SELECT locked_role, custom_role_id, scope_type, instance_id
                FROM user_role_assignments
                WHERE user_id = :uid
                  AND admin_id = :aid
                  AND revoked_at IS NULL
                """
            ),
            {"uid": user_id, "aid": admin_id},
        ).fetchall()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return []
    return [
        (r.locked_role, r.custom_role_id, r.scope_type, r.instance_id)
        for r in rows
    ]


# ---------------------------------------------------------------------
# Convenience predicates the policy module + routes can call.
# ---------------------------------------------------------------------


def caller_holds_permission(
    request: Request,
    *,
    permission_key: str,
    instance: "Instance | None" = None,
) -> bool:
    """Return True if the caller holds ``permission_key`` in the given
    context. Wraps :meth:`PermissionResolver.resolve`.
    """
    return permission_key in PermissionResolver.resolve(request, instance=instance)


def caller_resolved_permissions(
    request: Request,
    *,
    instance: "Instance | None" = None,
) -> frozenset[str]:
    """Return the caller's full effective permission set."""
    return PermissionResolver.resolve(request, instance=instance)
