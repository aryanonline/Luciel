"""
Authentication middleware.

Validates API keys on incoming requests and injects
tenant_id, domain_id, and agent_id into the request state.

PATCHED (Step 21): Admin routes are no longer skipped.
They now require a valid API key with "admin" in permissions.
"""

from __future__ import annotations

import logging

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.db.session import SessionLocal
from app.services.api_key_service import ApiKeyService

logger = logging.getLogger(__name__)

# Paths that do not require authentication at all.
# NOTE: /api/v1/admin is intentionally NOT here anymore.
SKIP_AUTH_PATHS = (
    "/health",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/api/v1/version",
)

# Paths that require a valid API key AND "admin" permission.
ADMIN_AUTH_PATHS = (
    "/api/v1/admin",
)


class ApiKeyAuthMiddleware(BaseHTTPMiddleware):

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Skip auth for truly public paths (health, docs).
        for skip_path in SKIP_AUTH_PATHS:
            if path.startswith(skip_path) or path == skip_path:
                return await call_next(request)

        # Determine if this is an admin route.
        is_admin_route = any(
            path.startswith(admin_path) for admin_path in ADMIN_AUTH_PATHS
        )

        # Extract the API key from the Authorization header.
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header. Use: Bearer <api_key>"},
            )

        raw_key = auth_header.replace("Bearer ", "").strip()
        if not raw_key:
            return JSONResponse(
                status_code=401,
                content={"detail": "API key is empty"},
            )

        # Validate the key using an isolated DB session.
        db = SessionLocal()
        try:
            service = ApiKeyService(db)
            api_key = service.validate_key(raw_key)

            if api_key is None:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or inactive API key"},
                )

            # Copy values before closing the session.
            tenant_id = api_key.tenant_id
            domain_id = api_key.domain_id
            agent_id = api_key.agent_id
            key_id = api_key.id
            permissions = api_key.permissions or []

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

        # Admin routes require "admin" permission on the API key.
        if is_admin_route and "admin" not in permissions:
            return JSONResponse(
                status_code=403,
                content={"detail": "This API key does not have admin permissions"},
            )

        # Inject tenant, domain, and agent context into the request.
        request.state.tenant_id = tenant_id
        request.state.domain_id = domain_id
        request.state.agent_id = agent_id
        request.state.api_key_id = key_id
        request.state.permissions = permissions

        return await call_next(request)
