from __future__ import annotations

import logging
import os
import time

from slowapi import Limiter
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("luciel.ratelimit")

REDIS_URL = os.getenv("REDISURL")

if REDIS_URL:
    storage_uri = REDIS_URL
    storage_note = f"Redis: {REDIS_URL}"
else:
    storage_uri = "memory://"
    storage_note = "In-memory local dev only, not shared across containers"

logger.info("Rate limit storage: %s", storage_note)


def get_api_key_or_ip(request: Request) -> str:
    """
    Identify the caller by API key if present, otherwise by IP address.
    This keeps tenant limits fair even when multiple users share one IP.
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        raw_key = auth_header.replace("Bearer ", "").strip()
        if raw_key:
            return raw_key

    x_api_key = request.headers.get("X-API-Key")
    if x_api_key:
        return x_api_key

    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()

    client = getattr(request, "client", None)
    if client and client.host:
        return client.host

    return "unknown"


limiter = Limiter(
    key_func=get_api_key_or_ip,
    default_limits=["60/minute"],
    storage_uri=storage_uri,
)

CHAT_RATE_LIMIT = "20/minute"
KNOWLEDGE_UPLOAD_RATE_LIMIT = "10/minute"
ADMIN_RATE_LIMIT = "30/minute"

_last_fallback_warning = 0.0
FALLBACK_LOG_INTERVAL_SECONDS = 60


def rate_limit_exceeded_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Clean JSON response when a request exceeds its configured limit.
    """
    detail = getattr(exc, "detail", str(exc))
    return JSONResponse(
        status_code=429,
        content={
            "error": "rate_limit_exceeded",
            "detail": str(detail),
            "message": "You have exceeded the allowed request rate. Please wait and try again.",
        },
    )


def create_rate_limit_middleware():
    """
    Return middleware that fails open if the rate-limit storage backend is unavailable.
    Luciel must stay available even if Redis has a temporary outage.
    """
    from starlette.middleware.base import BaseHTTPMiddleware

    class RateLimitFallbackMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            global _last_fallback_warning

            try:
                return await call_next(request)
            except Exception as exc:
                error_text = str(exc).lower()
                is_rate_limit_backend_error = any(
                    token in error_text
                    for token in (
                        "redis",
                        "connection refused",
                        "connection reset",
                        "timed out",
                        "timeout",
                        "storage",
                    )
                )

                if not is_rate_limit_backend_error:
                    raise

                now = time.time()
                if now - _last_fallback_warning >= FALLBACK_LOG_INTERVAL_SECONDS:
                    logger.warning(
                        "Rate-limit backend unavailable. Failing open so Luciel stays available. Error: %s",
                        exc,
                    )
                    _last_fallback_warning = now

                limiter.enabled = False
                try:
                    response = await call_next(request)
                    return response
                finally:
                    limiter.enabled = True

    return RateLimitFallbackMiddleware