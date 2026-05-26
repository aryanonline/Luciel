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
* :func:`ScopePolicy.enforce_no_privilege_escalation` — unchanged.
* :func:`ScopePolicy.enforce_action` — unchanged.

Legacy methods that referenced domain_id / agent_id (``enforce_domain_scope``,
``enforce_agent_scope``, ``enforce_luciel_creation_scope``,
``enforce_luciel_instance_scope``, ``_caller_creation_ceiling``) survive
as V2-collapsed delegations to ``enforce_tenant_scope`` (cross-Admin
guard) plus the admin-owns-instance check. They keep the existing call
surface working through B1's route-body rewrite; B1 sweeps the
callsites to the V2 method names and these delegations get removed at
Arc 6.

Cross-refs: D-arc5-aggressive-cleanup-doctrine-amendment-2026-05-23,
D-arc5-b2-incomplete-instance-service-not-collapsed-2026-05-23 row #4.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)

PLATFORM_ADMIN = "platform_admin"
ADMIN = "admin"


class ScopePolicy:
    @staticmethod
    def _caller(request: Request) -> tuple[str | None, str | None, str | None, list[str]]:
        """Return the caller's (admin_id, domain_id, agent_id, permissions) tuple.

        V2 surfaces only ``admin_id`` via ``request.state.admin_id``
        (the field name carries the legacy ``admin_id`` for one
        release window — Revision B backfilled the values to be equal
        to the V2 Admin slug). ``domain_id`` and ``agent_id`` are
        always None in V2 callers post-B1; they remain in the return
        tuple as ``None`` literals so legacy call-sites that destructure
        the tuple still parse.
        """
        admin_id = getattr(request.state, "admin_id", None)
        permissions = getattr(request.state, "permissions", []) or []
        return admin_id, None, None, permissions

    @classmethod
    def is_platform_admin(cls, request: Request) -> bool:
        _, _, _, perms = cls._caller(request)
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
        caller_admin, _, _, perms = cls._caller(request)
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
        caller_admin, _, _, _ = cls._caller(request)
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
    # Legacy V1 method names — V2-collapsed delegations.
    # ------------------------------------------------------------------
    #
    # The V1 hierarchy had three levels (tenant / domain / agent). V2
    # has one (Admin). The methods below survive only because B1 has
    # not yet rewritten the 24+ callsites in admin.py; once B1 lands,
    # these become unused and are removed at Arc 6.

    @classmethod
    def enforce_domain_scope(
        cls, request: Request, target_tenant_id: str, target_domain_id: str | None,
    ) -> None:
        """V2-collapsed: domain is not a V2 concept. Delegates to the
        cross-Admin guard and ignores ``target_domain_id``.
        """
        cls.enforce_tenant_scope(request, target_tenant_id)

    @classmethod
    def enforce_agent_scope(
        cls,
        request: Request,
        target_tenant_id: str,
        target_domain_id: str | None,
        target_agent_id: str | None,
    ) -> None:
        """V2-collapsed: agent is not a V2 concept. Delegates to the
        cross-Admin guard.
        """
        cls.enforce_tenant_scope(request, target_tenant_id)

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
    def enforce_luciel_creation_scope(
        cls,
        request: Request,
        *,
        target_tenant_id: str,
        target_domain_id: str | None = None,
        target_agent_id: str | None = None,
        **_legacy_kwargs,
    ) -> None:
        """V2-collapsed Instance-create authorization.

        V2 has no hierarchy below the Admin; the only check left is
        "caller's Admin == target_tenant_id" (or platform_admin). The
        legacy V1 keyword (the level discriminator) is swallowed via
        ``**_legacy_kwargs`` so B1's route-body rewrite can drop it
        from callsites incrementally without changing the signature
        in the meantime.
        """
        cls.enforce_tenant_scope(request, target_tenant_id)

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

        _, _, _, perms = cls._caller(request)
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
