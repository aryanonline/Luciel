"""
Scope enforcement policy.

Central authorization layer that verifies a caller's API key scope
(tenant_id, domain_id, agent_id, permissions) is allowed to act on
a target resource. Step 24.

Rules:
- platform_admin permission -> can act across all tenants.
- Otherwise caller.tenant_id must match target.tenant_id.
- If caller.domain_id is set, it must match target.domain_id.
- If caller.agent_id is set, it must match target.agent_id.
- Privilege escalation is rejected: a non-platform_admin caller
  cannot create a platform_admin key.
"""
from __future__ import annotations

import logging
from fastapi import HTTPException, Request, status

# Step 24.5 — scope-level constants for LucielInstance authorization.
# Imported via a late-bound local import in each method below to keep
# app.policy.scope free of SQLAlchemy-model dependencies at module
# load time. (Same discipline used in app.policy.consent /
# app.policy.retention.)

logger = logging.getLogger(__name__)

PLATFORM_ADMIN = "platform_admin"
ADMIN = "admin"


class ScopePolicy:
    @staticmethod
    def _caller(request: Request) -> tuple[str | None, str | None, str | None, list[str]]:
        tenant_id = getattr(request.state, "tenant_id", None)
        domain_id = getattr(request.state, "domain_id", None)
        agent_id = getattr(request.state, "agent_id", None)
        permissions = getattr(request.state, "permissions", []) or []
        return tenant_id, domain_id, agent_id, permissions

    @classmethod
    def is_platform_admin(cls, request: Request) -> bool:
        _, _, _, perms = cls._caller(request)
        return PLATFORM_ADMIN in perms

    @classmethod
    def enforce_tenant_scope(cls, request: Request, target_tenant_id: str) -> None:
        caller_tenant, _, _, perms = cls._caller(request)
        if PLATFORM_ADMIN in perms:
            return
        if caller_tenant is None or caller_tenant != target_tenant_id:
            logger.warning(
                "Scope violation: caller tenant=%s tried to access tenant=%s",
                caller_tenant, target_tenant_id,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cross-tenant access is not permitted for this key",
            )

    @classmethod
    def enforce_domain_scope(
        cls, request: Request, target_tenant_id: str, target_domain_id: str | None,
    ) -> None:
        cls.enforce_tenant_scope(request, target_tenant_id)
        _, caller_domain, _, perms = cls._caller(request)
        if PLATFORM_ADMIN in perms:
            return
        # If caller key is domain-scoped, target must match that domain.
        if caller_domain is not None and caller_domain != target_domain_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This key is scoped to a different domain",
            )

    @classmethod
    def enforce_agent_scope(
        cls,
        request: Request,
        target_tenant_id: str,
        target_domain_id: str | None,
        target_agent_id: str | None,
    ) -> None:
        cls.enforce_domain_scope(request, target_tenant_id, target_domain_id)
        _, _, caller_agent, perms = cls._caller(request)
        if PLATFORM_ADMIN in perms:
            return
        if caller_agent is not None and caller_agent != target_agent_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This key is scoped to a different agent",
            )

    @classmethod
    def enforce_no_privilege_escalation(
        cls, request: Request, target_permissions: list[str],
    ) -> None:
        """Callers without platform_admin cannot mint platform_admin keys."""
        if PLATFORM_ADMIN in (target_permissions or []) and not cls.is_platform_admin(request):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only platform_admin may grant platform_admin permission",
            )
    # -----------------------------------------------------------------
    # Step 24.5 — LucielInstance authorization
    # -----------------------------------------------------------------

    @classmethod
    def _caller_creation_ceiling(
        cls, request: Request
    ) -> str:
        """Return the highest scope level the caller is allowed to
        create at. Used by enforce_luciel_creation_scope.

        Returns one of: "platform" | "tenant" | "domain" | "agent".

        Rules (matches the permission matrix in the Step 24.5 plan):
          - platform_admin permission             -> 'platform'
          - tenant-scoped admin (no domain/agent) -> 'tenant'
          - domain-scoped admin (domain, no agent)-> 'domain'
          - agent-scoped admin                    -> 'agent'
          - any caller without admin at all       -> raises 403
            (creation is an admin-only operation; this should
            normally be caught upstream by permission checks, but
            we reject here defensively)
        """
        if cls.is_platform_admin(request):
            return "platform"

        caller_tenant, caller_domain, caller_agent, perms = cls._caller(request)

        if ADMIN not in perms:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This API key does not have admin permissions.",
            )

        if caller_agent is not None:
            return "agent"
        if caller_domain is not None:
            return "domain"
        if caller_tenant is not None:
            return "tenant"

        # Admin permission but no scope at all — shouldn't happen for
        # a non-platform_admin key. Reject defensively.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This admin key has no tenant scope; cannot create resources.",
        )

    @classmethod
    def enforce_luciel_creation_scope(
        cls,
        request: Request,
        *,
        target_scope_level: str,
        target_tenant_id: str,
        target_domain_id: str | None = None,
        target_agent_id: str | None = None,
    ) -> None:
        """Enforce the 'create at or below your own scope' rule for
        a new LucielInstance.

        Authorization matrix:

          caller scope          | may create at target_scope_level
          --------------------- | -------------------------------------
          platform_admin        | tenant / domain / agent (any tenant)
          tenant-scoped admin   | tenant / domain / agent (own tenant)
          domain-scoped admin   | domain / agent (own domain only)
          agent-scoped admin    | agent (own agent only)

        Additionally:
          - target_tenant_id must match caller's tenant (enforced by
            enforce_tenant_scope).
          - For domain-scoped callers: target_domain_id must match
            caller's domain_id.
          - For agent-scoped callers: target_agent_id must match
            caller's agent_id (and by transitivity, target_domain_id
            must match caller's domain_id).

        Raises HTTPException(403) on any violation.
        """
        # Late-bound import to avoid circularity — scope.py does not
        # depend on SQLAlchemy models at module load.
        from app.models.luciel_instance import (
            SCOPE_LEVEL_AGENT,
            SCOPE_LEVEL_DOMAIN,
            SCOPE_LEVEL_TENANT,
        )

        _LEVEL_RANK = {
            SCOPE_LEVEL_TENANT: 1,
            SCOPE_LEVEL_DOMAIN: 2,
            SCOPE_LEVEL_AGENT: 3,
        }
        _CEILING_RANK = {
            "platform": 0,  # platform is "above tenant"; can create anything
            "tenant": 1,
            "domain": 2,
            "agent": 3,
        }

        if target_scope_level not in _LEVEL_RANK:
            # Defensive — schema validator already rejects this,
            # but authorization should never rely solely on schema.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown target_scope_level: {target_scope_level!r}",
            )

        # Step 1 — cross-tenant guard (reuses Step 24's helper).
        cls.enforce_tenant_scope(request, target_tenant_id)

        # Step 2 — caller's ceiling must be at or above the target level.
        ceiling = cls._caller_creation_ceiling(request)
        if _CEILING_RANK[ceiling] > _LEVEL_RANK[target_scope_level]:
            logger.warning(
                "Luciel creation denied: caller ceiling=%s target_level=%s "
                "tenant=%s",
                ceiling,
                target_scope_level,
                target_tenant_id,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "This API key cannot create a Luciel at a higher scope "
                    "than its own key scope."
                ),
            )

        # Step 3 — if the caller is domain- or agent-scoped, the target
        # owner identifiers must lie within the caller's own scope.
        if cls.is_platform_admin(request):
            return

        _, caller_domain, caller_agent, _ = cls._caller(request)

        # Domain-scoped callers: target domain must match caller domain.
        if caller_domain is not None:
            if target_domain_id != caller_domain:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="This key is scoped to a different domain.",
                )

        # Agent-scoped callers: target agent must match caller agent.
        if caller_agent is not None:
            if target_scope_level != SCOPE_LEVEL_AGENT:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        "Agent-scoped keys may only create agent-level Luciels."
                    ),
                )
            if target_agent_id != caller_agent:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="This key is scoped to a different agent.",
                )

    # -----------------------------------------------------------------
    # Step 29.y gap-fix C3 (D-scope-policy-action-class-gap-2026-05-07)
    # -----------------------------------------------------------------
    #
    # Origin: every existing ScopePolicy method enforces SCOPE
    # (tenant/domain/agent reach), but action-class enforcement ("this
    # caller must hold permission P to perform action A on resource R")
    # was scattered across middleware (auth.py: is_admin_route check)
    # and ad-hoc per-route asserts. There was no named primitive a new
    # route author could call. That gap meant a future route added
    # without going through middleware (e.g. an internal worker entry,
    # a queued task that pulls a request-like object) had no single
    # call to make.
    #
    # enforce_action() closes the gap. It does NOT replace the middleware
    # admin-route check (that path stays the primary fast-fail), and it
    # is intentionally NOT wired into any existing route in this commit.
    # Wiring it would change behaviour, which is out of scope for this
    # code-only gap-fix session per binding session rules. Future routes
    # and any audit of existing routes can adopt it.
    #
    # Action labels are free-form strings used in the audit trail when
    # a permission check fails. They are NOT the same vocabulary as
    # ALLOWED_ACTIONS in app.models.admin_audit_log (those describe
    # successful mutations); these describe attempted ones.
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

        platform_admin satisfies any required_permission by design --
        same precedent as enforce_tenant_scope. This keeps the policy
        layer self-consistent: a platform_admin key is the privileged
        identity in EVERY enforcement primitive.

        Validation:
          - required_permission must be a non-empty plain identifier
            (no comma, no whitespace control). This matches the
            actor_permissions on-disk format invariant established in
            gap-fix C1.
          - action_label must be a non-empty string. Used in the
            403 detail and the warning log line so an operator can
            grep for the failing action class.
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
            action_label, required_permission, sorted(perms),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"This API key does not have permission "
                f"{required_permission!r} required for action {action_label!r}."
            ),
        )

    @classmethod
    def enforce_luciel_instance_scope(
        cls,
        request: Request,
        instance,  # app.models.luciel_instance.LucielInstance
    ) -> None:
        """Enforce read / update / delete authorization against an
        existing LucielInstance row.

        An action on `instance` is allowed when the caller could have
        CREATED `instance` in the first place. So we delegate to
        enforce_luciel_creation_scope with the instance's owner
        triple as the target.

        This keeps the read/write rules identical to the create rules —
        no divergence possible.
        """
        cls.enforce_luciel_creation_scope(
            request,
            target_scope_level=instance.scope_level,
            target_tenant_id=instance.scope_owner_tenant_id,
            target_domain_id=instance.scope_owner_domain_id,
            target_agent_id=instance.scope_owner_agent_id,
        )