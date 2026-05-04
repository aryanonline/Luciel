"""
Authentication middleware.

Validates API keys on incoming requests and injects tenant_id, domain_id,
agent_id, luciel_instance_id, and (Step 24.5b) user_id into the request
state.

PATCHED (Step 21): Admin routes are no longer skipped. They now require a
valid API key with 'admin' in permissions.

PATCHED (Step 24.5 File 10.5): Inject key_prefix + actor_label for audit.

PATCHED (Step 24.5 File 14): Inject luciel_instance_id onto request.state
so chat_service (File 15) can resolve persona / provider / tools / prompt
from the bound LucielInstance. None for legacy / unbound keys -> legacy
fallback path in chat_service.

PATCHED (Step 24.5b File 2.4): Inject actor_user_id onto request.state.
Resolved from the Agent row keyed by (tenant_id, domain_id, agent_id) when
the key is agent-scoped. None for tenant-admin / platform-admin keys (no
single bound User), and None for agent-scoped keys whose Agent.user_id is
still NULL (legacy rows pending the Commit 3 backfill).

Distinct from the existing session.user_id which is a free-form
client-supplied end-user identifier string -- actor_user_id is the
platform User UUID identifying which Agent's durable identity wrote a
given memory/trace row. The two coexist by design (drift item D7
resolution): a single platform User (Sarah-the-listings-agent) may
handle conversations on behalf of many session.user_id values
(prospect-1234, lead-5678, etc.). ChatService and the async memory
worker treat actor_user_id=None as "no platform User attribution
available" -- writes still work, just without the FK populated until
backfill.
"""
from __future__ import annotations

import logging

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.db.session import SessionLocal
from app.repositories.agent_repository import AgentRepository
from app.services.api_key_service import ApiKeyService

logger = logging.getLogger(__name__)


SKIP_AUTH_PATHS = {
    "/health",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/api/v1/version",
}

ADMIN_AUTH_PATHS = ("/api/v1/admin",)


class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        for skip_path in SKIP_AUTH_PATHS:
            if path.startswith(skip_path) or path == skip_path:
                return await call_next(request)

        is_admin_route = any(
            path.startswith(admin_path) for admin_path in ADMIN_AUTH_PATHS
        )

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "Missing or invalid Authorization header. Use Bearer <api_key>"
                },
            )

        raw_key = auth_header.replace("Bearer ", "").strip()
        if not raw_key:
            return JSONResponse(
                status_code=401,
                content={"detail": "API key is empty"},
            )

        db = SessionLocal()
        try:
            service = ApiKeyService(db)
            apikey = service.validate_key(raw_key)

            if apikey is None:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or inactive API key"},
                )

            tenant_id = apikey.tenant_id
            domain_id = apikey.domain_id
            agent_id = apikey.agent_id
            api_key_id = apikey.id
            permissions = apikey.permissions or []
            key_prefix = apikey.key_prefix
            actor_label = apikey.created_by
            luciel_instance_id = apikey.luciel_instance_id

            # Step 24.5b: resolve user_id from the bound Agent row.
            # Only agent-scoped keys carry a meaningful user_id;
            # tenant-admin / platform-admin / chat-key-only paths
            # leave user_id=None. The Agent natural-key lookup hits
            # the existing uq_agents_tenant_domain_agent unique
            # constraint composite index -- ~1ms.
            actor_user_id = None
            if tenant_id is not None and domain_id is not None and agent_id is not None:
                agent_repo = AgentRepository(db)
                agent = agent_repo.get_scoped(
                    tenant_id=tenant_id,
                    domain_id=domain_id,
                    agent_id=agent_id,
                )
                if agent is not None:
                    user_id = agent.user_id  # None until Commit 3 backfill
                else:
                    # Defensive: ApiKey references a non-existent Agent.
                    # This shouldn't happen in steady state (keys are
                    # minted against existing agents), but log loudly
                    # if it does -- it indicates orphan key drift that
                    # Step 28 cleanup should sweep.
                    logger.warning(
                        "auth middleware: ApiKey.id=%s references "
                        "missing Agent (tenant=%s, domain=%s, "
                        "agent_id=%s) -- user_id resolution skipped",
                        api_key_id, tenant_id, domain_id, agent_id,
                    )

        except Exception as exc:
            logger.error("Auth middleware error: %s", exc)
            return JSONResponse(
                status_code=500,
                content={"detail": "Authentication service error"},
            )
        finally:
            try:
                db.rollback()
            except Exception:
                pass
            db.close()

        if is_admin_route and "admin" not in permissions:
            return JSONResponse(
                status_code=403,
                content={"detail": "This API key does not have admin permissions"},
            )

        request.state.tenant_id = tenant_id
        request.state.domain_id = domain_id
        request.state.agent_id = agent_id
        request.state.api_key_id = api_key_id
        request.state.permissions = permissions
        request.state.key_prefix = key_prefix
        request.state.actor_label = actor_label
        request.state.luciel_instance_id = luciel_instance_id
        request.state.actor_user_id = actor_user_id  # Step 24.5b

        # P13_DIAG (temporary -- D-pillar-13-a3-real-root-cause-2026-05-04):
        # Surface the actor_user_id binding decision. If actor_user_id is
        # None despite a real Agent.user_id existing, that's the bug.
        try:
            path = request.url.path
        except Exception:
            path = "?"
        if path == "/api/v1/chat" or path.startswith("/api/v1/chat"):
            logger.warning(
                "P13_DIAG auth-bind path=%s tenant=%s agent=%s "
                "key_prefix=%r api_key_id=%s actor_user_id=%r "
                "luciel_instance_id=%s permissions=%s",
                path, tenant_id, agent_id,
                key_prefix, api_key_id, actor_user_id,
                luciel_instance_id, permissions,
            )

        return await call_next(request)