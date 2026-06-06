"""Scope enforcement policy — single-login Admin → Instance surface.

The platform is single-login (Locked Decision #19, Architecture §3.7.1): each
account has exactly one operating identity, the ``account_owner``, who does
everything. There is no role hierarchy, no team seats, no custom roles, and no
secondary login. The multi-role RBAC machinery (manager / operator /
read-only-viewer roles, scope assignments, custom-role authoring, the
PermissionResolver) was excised in the audit-and-alignment phase (Unit 1).

Two scope levels remain — exactly the two isolation walls that matter
(Architecture §3.7.2b, Vision §5):

* ``platform_admin`` — the cross-Admin operator (the operator wall, §5.11).
  Unchanged; preserved intact.
* Admin (``account_owner``) — one boundary per billing tenant. The owner holds
  every tenant-scoped permission by definition.

Enforcement seam preserved. The admin routes still call
:func:`enforce_action` / :func:`enforce_role_on_instance` / the cross-Admin and
owns-instance guards — this keeps the §3.4.14 Defense-1 property (privilege is
enforced in code around the model, never by the prompt). With a single owner
identity, the role gates reduce to "is this the authenticated owner of the
target Admin/Instance (or a platform_admin)?" — the cross-Admin guard does the
real isolation work; the owner then passes every action gate.
"""

from __future__ import annotations

import enum
import logging
from typing import Literal

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)

PLATFORM_ADMIN = "platform_admin"
ADMIN = "admin"


class ScopeRole(str, enum.Enum):
    """The sole tenant role under the single-login model (Locked Dec #19).

    Retained as a one-member enum (not a bare constant) so the handful of
    call sites and tests that reference ``ScopeRole.ADMIN_OWNER`` or coerce a
    role string keep working without change. The manager / operator /
    read-only-viewer members were removed with the multi-role RBAC surface.
    """

    ADMIN_OWNER = "admin_owner"


ROLE_ADMIN_OWNER = ScopeRole.ADMIN_OWNER

# Every tenant role collapses to the single owner; the knowledge-action matrix
# (Architecture §3.2.2) therefore admits the owner for every action. Kept as a
# table so the wrapper API and its tests are unchanged.
ALL_KNOWLEDGE_ROLES: frozenset[ScopeRole] = frozenset({ROLE_ADMIN_OWNER})

_KNOWLEDGE_ACTION_ROLES: dict[str, frozenset[ScopeRole]] = {
    "list": frozenset({ROLE_ADMIN_OWNER}),
    "view": frozenset({ROLE_ADMIN_OWNER}),
    "edit": frozenset({ROLE_ADMIN_OWNER}),
    "delete": frozenset({ROLE_ADMIN_OWNER}),
}


class ScopePolicy:
    @staticmethod
    def _caller(request: Request) -> tuple[str | None, list[str]]:
        """Return the caller's ``(admin_id, permissions)`` tuple."""
        admin_id = getattr(request.state, "admin_id", None)
        permissions = getattr(request.state, "permissions", []) or []
        return admin_id, permissions

    @classmethod
    def is_platform_admin(cls, request: Request) -> bool:
        _, perms = cls._caller(request)
        return PLATFORM_ADMIN in perms

    # ------------------------------------------------------------------
    # Cross-Admin guard (the tenant wall — Architecture §3.7.2b)
    # ------------------------------------------------------------------

    @classmethod
    def enforce_tenant_scope(cls, request: Request, target_admin_id: str) -> None:
        """Reject the call if the caller's Admin does not match the target
        Admin (and the caller is not a platform_admin)."""
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

    @classmethod
    def enforce_admin_owns_instance(cls, request: Request, instance) -> None:
        """Verify the caller's Admin owns this Instance row (flat admin_id ==
        instance.admin_id check, with the platform_admin bypass)."""
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

    @classmethod
    def enforce_no_privilege_escalation(
        cls, request: Request, target_permissions: list[str]
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
    def enforce_luciel_instance_scope(cls, request: Request, instance) -> None:
        """V2-collapsed: delegate to :func:`enforce_admin_owns_instance`."""
        cls.enforce_admin_owns_instance(request, instance)

    # ------------------------------------------------------------------
    # Role / action gates — single-owner model.
    #
    # Under single-login, the authenticated owner of the target Admin holds
    # every tenant-scoped permission. These gates therefore reduce to the
    # cross-Admin / owns-instance guard plus the platform_admin bypass. They
    # remain as methods (the seam) so the 20 admin routes are unchanged and
    # the Defense-1 "privilege enforced in code" property holds.
    # ------------------------------------------------------------------

    @classmethod
    def _resolve_role_on_instance(cls, request: Request, instance) -> ScopeRole | None:
        """Resolve the caller's role on an Instance. Under single-login, the
        owner of the Instance's Admin is ADMIN_OWNER; everyone else is None
        (deny). platform_admin returns None (handled by the bypass upstream)."""
        if cls.is_platform_admin(request):
            return None
        caller_admin, _ = cls._caller(request)
        target_admin = getattr(instance, "admin_id", None)
        if caller_admin is not None and target_admin is not None and caller_admin == target_admin:
            return ROLE_ADMIN_OWNER
        return None

    @staticmethod
    def _coerce_role(value) -> ScopeRole | None:
        """Best-effort coercion to ``ScopeRole``. Unknown → None (deny)."""
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
        """Verify the caller may act on this Instance.

        Single-login model: the only tenant role is ``account_owner``. The
        gate passes iff the caller is a platform_admin (operator bypass) or
        the authenticated owner of the target Instance's Admin. ``allowed_roles``
        is retained for signature compatibility (every gated action admits the
        owner); an empty set still fails closed.
        """
        # 1. Platform-admin bypass (operator wall).
        if cls.is_platform_admin(request):
            return

        if not allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No roles configured to permit this action",
            )

        # 2. Cross-Admin guard — caller's Admin must own the Instance. This is
        #    the real isolation work (Architecture §3.7.2b).
        cls.enforce_admin_owns_instance(request, instance)

        # 3. The owner of the target Admin holds every tenant permission.
        if cls._resolve_role_on_instance(request, instance) is ROLE_ADMIN_OWNER:
            return

        logger.warning(
            "Role denial: caller is not the account owner for admin=%s instance=%s",
            getattr(instance, "admin_id", None),
            getattr(instance, "id", None),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Caller is not the account owner for this Admin",
        )

    @classmethod
    def require_knowledge_role(
        cls,
        request: Request,
        instance,
        action: Literal["list", "view", "edit", "delete"],
    ) -> None:
        """Knowledge-action wrapper. Under single-login the owner may perform
        every knowledge action (Architecture §3.2.2 collapses to one role)."""
        if action not in _KNOWLEDGE_ACTION_ROLES:
            raise ValueError(
                f"Unknown knowledge action {action!r}; expected one of "
                f"{sorted(_KNOWLEDGE_ACTION_ROLES.keys())}"
            )
        cls.enforce_role_on_instance(
            request, instance, allowed_roles=_KNOWLEDGE_ACTION_ROLES[action]
        )

    @classmethod
    def enforce_action(
        cls,
        request: Request,
        *,
        required_permission: str,
        action_label: str,
        instance=None,
    ) -> None:
        """Verify the caller may perform ``action_label``.

        Single-login model: ``platform_admin`` satisfies any permission; the
        authenticated account owner holds every tenant permission. The legacy
        transport-layer permission list (e.g. ``["admin","chat","sessions"]``)
        is still honoured for non-owner service identities (worker/system keys)
        that carry an explicit permission string.
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

        caller_admin, perms = cls._caller(request)

        # Platform-admin bypass (operator wall).
        if PLATFORM_ADMIN in perms:
            return

        # Transport-layer permission check (worker/system/API-key identities
        # that carry an explicit permission string such as "admin").
        if required_permission in perms:
            return

        # The authenticated account owner holds every tenant-scoped permission.
        # If an instance is supplied, confirm ownership; otherwise the bound
        # admin_id on the request is itself the owner identity.
        if instance is not None:
            if cls._resolve_role_on_instance(request, instance) is ROLE_ADMIN_OWNER:
                return
        elif caller_admin is not None:
            # Admin-scoped action with an authenticated owner identity.
            return

        logger.warning(
            "Action denied: action=%s required_permission=%s transport_perms=%s",
            action_label,
            required_permission,
            sorted(perms),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"This caller does not have permission "
                f"{required_permission!r} required for action {action_label!r}."
            ),
        )


def _role_to_str(value) -> str | None:
    """Coerce a ``ScopeRole`` member or the owner string to its canonical
    value; return ``None`` on anything else (fail-closed)."""
    if isinstance(value, ScopeRole):
        return value.value
    if isinstance(value, str) and value == "admin_owner":
        return value
    return None
