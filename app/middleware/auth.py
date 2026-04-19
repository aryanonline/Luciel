"""
Authentication middleware.

Validates API keys on incoming requests and injects tenant_id, domain_id,
agent_id, and luciel_instance_id into the request state.

PATCHED (Step 21): Admin routes are no longer skipped. They now require a
valid API key with 'admin' in permissions.

PATCHED (Step 24.5 File 10.5): Inject key_prefix + actor_label for audit.

PATCHED (Step 24.5 File 14): Inject luciel_instance_id onto request.state
so chat_service (File 15) can resolve persona / provider / tools / prompt
from the bound LucielInstance. None for legacy / unbound keys -> legacy
fallback path in chat_service.
"""
from __future__ import annotations

import logging

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.db.session import SessionLocal
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

        return await call_next(request)