from __future__ import annotations

import logging
import os
import time

from slowapi import Limiter
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import settings

logger = logging.getLogger("luciel.ratelimit")

# Step 29.y Cluster 5 (B-1): correct env-var name. Pre-29.y this
# read os.getenv("REDISURL") (no underscore), which silently
# resolved to None in every environment that exports REDIS_URL --
# i.e. all of prod. SlowAPI then fell through to memory:// storage,
# making per-route rate limits per-process instead of shared, and
# every other component reads REDIS_URL with the underscore. The
# typo neutered our rate limits for the entire prod lifetime.
#
# Step 29.y close (D-redis-url-centralize-via-settings-2026-05-08):
# Read through `settings.redis_url` so this module shares the single
# source of truth defined in `app.core.config`. The empty-string
# fallback below preserves the prior behaviour where an unset
# REDIS_URL means "no shared backend, use in-memory storage" -- the
# Settings default is `redis://localhost:6379/0` for dev, but that
# default is ONLY appropriate when running locally. Prod ALWAYS
# injects REDIS_URL from SSM via the ECS task-def `secrets:` block,
# so prod never sees the localhost fallback. To force in-memory
# (e.g. unit tests), set REDIS_URL="" explicitly.
REDIS_URL = settings.redis_url or None

# Step 29.y Cluster 5 (B-1): hardened Redis pool. retry_on_timeout
# rides over single-RTT blips without raising; tight socket
# timeouts make the limiter fail FAST when the storage truly is
# unreachable so the fallback middleware can decide fail-open vs
# fail-closed (see WRITE_METHODS below) instead of stalling the
# request.
REDIS_SOCKET_CONNECT_TIMEOUT_S = float(
    os.getenv("RATE_LIMIT_REDIS_CONNECT_TIMEOUT", "1.5")
)
REDIS_SOCKET_TIMEOUT_S = float(
    os.getenv("RATE_LIMIT_REDIS_SOCKET_TIMEOUT", "1.5")
)
REDIS_HEALTH_CHECK_INTERVAL_S = int(
    os.getenv("RATE_LIMIT_REDIS_HEALTH_CHECK_INTERVAL", "30")
)

if REDIS_URL:
    storage_uri = REDIS_URL
    storage_options = {
        "socket_connect_timeout": REDIS_SOCKET_CONNECT_TIMEOUT_S,
        "socket_timeout": REDIS_SOCKET_TIMEOUT_S,
        "retry_on_timeout": True,
        "health_check_interval": REDIS_HEALTH_CHECK_INTERVAL_S,
    }
    storage_note = (
        f"Redis: {REDIS_URL} (timeouts {REDIS_SOCKET_TIMEOUT_S}s, "
        "retry_on_timeout=True)"
    )
else:
    storage_uri = "memory://"
    storage_options = {}
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


# Step 29.y Cluster 5 (B-1): in_memory_fallback_enabled lets SlowAPI
# transparently fall back to per-process memory storage when the
# primary Redis backend dies. This is the FAIL-OPEN path for reads
# -- the limiter keeps working at degraded fidelity (per-process
# instead of cluster-wide) rather than 500ing every request. Writes
# are handled separately by the fallback middleware below, which
# returns 503 so write quota integrity is preserved.
limiter = Limiter(
    key_func=get_api_key_or_ip,
    default_limits=["60/minute"],
    storage_uri=storage_uri,
    storage_options=storage_options,
    in_memory_fallback_enabled=True,
)

CHAT_RATE_LIMIT = "20/minute"
KNOWLEDGE_UPLOAD_RATE_LIMIT = "10/minute"
ADMIN_RATE_LIMIT = "30/minute"

# Step 29.y Cluster 5 (B-1): write methods fail CLOSED when the
# rate-limit backend bubbles an exception that escapes the
# in_memory_fallback. A 503 with Retry-After lets the ALB route the
# request to a healthy task or surface a clean retryable error to
# the client. Read methods fail OPEN -- the in-memory fallback in
# the Limiter handles them, and any exception that still escapes is
# allowed to surface (we do not silently turn it into a 200).
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

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


def _is_rate_limit_backend_error(exc: BaseException) -> bool:
    """
    Heuristic: was this exception raised by the rate-limit storage
    backend (Redis) rather than the application itself? Substring
    match on the exception text catches the redis-py exception
    families (ConnectionError, TimeoutError, BusyLoadingError) as
    well as slowapi/limits storage errors without forcing a hard
    import dependency on the exception classes.

    The match is intentionally narrow: "redis" or specific
    connection-failure phrases. Generic words like "timeout" alone
    are not enough -- a route handler raising ValueError("request
    timed out for user") would otherwise be misclassified as a
    backend error.
    """
    error_text = str(exc).lower()
    # First try the strong signal: the literal token "redis".
    if "redis" in error_text:
        return True
    # Then phrases that, taken together with the exception class
    # names redis-py raises, are the clear backend-failure surface.
    backend_phrases = (
        "connection refused",
        "connection reset",
        "connectionerror",
        "broken pipe",
        "no connection available",
    )
    return any(phrase in error_text for phrase in backend_phrases)


def create_rate_limit_middleware():
    """
    Return middleware that handles rate-limit storage outages.

    Step 29.y Cluster 5 (B-1) split fail-modes:
      * write methods (POST/PUT/PATCH/DELETE): fail CLOSED -> 503,
        so the caller backs off and the ALB routes around the box.
        Quota integrity matters more than availability for writes
        because writes mutate state.
      * read methods (GET/HEAD/OPTIONS/etc.): the SlowAPI
        in_memory_fallback handles the fall-through. Anything that
        still escapes is re-raised so it surfaces as a real error
        rather than being silently swallowed.

    Pre-29.y this middleware tried to fail-open by re-calling
    call_next() with the limiter disabled. That path is broken in
    modern Starlette because BaseHTTPMiddleware streams cannot be
    re-consumed within a single request; the retried call_next
    silently produced a 500. The new posture relies on the
    in_memory_fallback in the Limiter for the read fail-open and
    only intervenes for writes.
    """
    from starlette.middleware.base import BaseHTTPMiddleware

    class RateLimitFallbackMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            global _last_fallback_warning

            try:
                return await call_next(request)
            except Exception as exc:
                if not _is_rate_limit_backend_error(exc):
                    raise

                now = time.time()
                if now - _last_fallback_warning >= FALLBACK_LOG_INTERVAL_SECONDS:
                    logger.warning(
                        "Rate-limit backend unavailable. method=%s path=%s err=%s",
                        request.method,
                        request.url.path,
                        exc,
                    )
                    _last_fallback_warning = now

                method = (request.method or "GET").upper()
                if method in WRITE_METHODS:
                    # Fail closed for writes. 503 + Retry-After so
                    # the client and the ALB both treat this as
                    # transient and route around / back off.
                    return JSONResponse(
                        status_code=503,
                        headers={"Retry-After": "5"},
                        content={
                            "error": "rate_limit_backend_unavailable",
                            "detail": (
                                "Rate-limit storage is temporarily "
                                "unreachable. Write requests are "
                                "rejected to preserve quota integrity. "
                                "Please retry shortly."
                            ),
                        },
                    )

                # Read path: re-raise so the caller sees the real
                # error. The in_memory_fallback in the Limiter is
                # supposed to have caught the storage death before
                # we got here; if it did not, we do NOT silently
                # mask it as a 200.
                raise

    return RateLimitFallbackMiddleware
