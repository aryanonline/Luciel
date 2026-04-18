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