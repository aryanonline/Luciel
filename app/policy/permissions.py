"""Permission resolver — single-login (account_owner) model.

The multi-role RBAC machinery (custom roles, scope assignments, locked-role
permission seeds, the per-row resolution algorithm) was excised in the
audit-and-alignment phase (Unit 1) to match the single-login doctrine
(Locked Decision #19, Architecture §3.7.1): one ``account_owner`` per account,
who holds every tenant-scoped permission.

This module is retained as a thin shim so the admin routes that consult it are
unchanged. ``PermissionResolver.resolve`` returns:

  * :data:`PLATFORM_ADMIN_ALL` for the cross-Admin operator (platform_admin),
  * :data:`ALL_PERMISSIONS` for an authenticated account owner,
  * an empty set otherwise (fail-closed).

The cross-Admin / owns-instance guard in :class:`app.policy.scope.ScopePolicy`
does the real tenant-isolation work before these routes resolve permissions;
this resolver answers only "what may the (already tenant-scoped) caller do?",
which for the sole owner identity is "everything."
"""

from __future__ import annotations

from fastapi import Request

from app.policy.scope import PLATFORM_ADMIN


# ---------------------------------------------------------------------
# Permission vocabulary — the owner's atomic capabilities (Architecture
# §3.7.1: the owner does everything). The sibling-grant / custom-role /
# role-assignment permissions were removed with those deferred surfaces.
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
    }
)


class _PlatformAdminAll:
    """Sentinel that compares as a superset of any required permission.

    Returned for platform_admin callers (the operator wall, §5.11). It
    satisfies any ``perm in resolved`` / ``perm <= resolved`` style check.
    """

    _instance: "_PlatformAdminAll | None" = None

    def __new__(cls) -> "_PlatformAdminAll":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __contains__(self, _item: object) -> bool:  # `perm in PLATFORM_ADMIN_ALL`
        return True

    def __repr__(self) -> str:  # pragma: no cover
        return "<PLATFORM_ADMIN_ALL>"


PLATFORM_ADMIN_ALL = _PlatformAdminAll()


def permission_set_satisfies(resolved: object, required: str) -> bool:
    """True iff ``resolved`` grants ``required`` (handles the sentinel)."""
    if resolved is PLATFORM_ADMIN_ALL:
        return True
    try:
        return required in resolved  # type: ignore[operator]
    except TypeError:
        return False


class PermissionResolver:
    """Single-owner permission resolver (see module docstring)."""

    @staticmethod
    def resolve(request: Request, instance=None):
        """Resolve the caller's effective permission set.

        platform_admin → PLATFORM_ADMIN_ALL; an authenticated account owner →
        ALL_PERMISSIONS; otherwise → empty frozenset (fail-closed). Worker/
        system service identities that carry explicit transport-layer
        permission strings are handled by ``ScopePolicy.enforce_action``'s
        transport-permission branch, not here.
        """
        perms = getattr(request.state, "permissions", []) or []
        if PLATFORM_ADMIN in perms:
            return PLATFORM_ADMIN_ALL

        admin_id = getattr(request.state, "admin_id", None)
        if admin_id is None:
            return frozenset()

        # Single-login: the authenticated owner of the bound Admin holds every
        # tenant permission. When an instance is supplied, confirm it belongs
        # to the caller's Admin (defence in depth alongside the route's own
        # ScopePolicy guard).
        if instance is not None:
            target_admin = getattr(instance, "admin_id", None)
            if target_admin is not None and target_admin != admin_id:
                return frozenset()

        return ALL_PERMISSIONS
