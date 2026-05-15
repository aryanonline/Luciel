# Step 31 follow-up (D-prod-app-logger-info-suppressed-2026-05-12):
# Configure the root logger BEFORE any `app.*` import so every
# `logger = logging.getLogger(__name__)` inside the application
# resolves its effective level against an already-configured root.
# Without this, Python's default root level (WARNING) silently drops
# every `logger.info(...)` emission across the app -- including the
# Step 31 sub-branch 1 widget-chat audit log lines
# (`widget_chat_turn_received` / `widget_chat_session_resolved` /
# `widget_chat_turn_completed`) the ARCHITECTURE §3.2.7 claim depends
# on. The worker process does NOT need this fix because Celery's
# `--loglevel=info` flag configures its own root logger at bootstrap
# (verified by the 15s heartbeat INFO lines visible in
# /ecs/luciel-worker). `force=True` is defensive against any earlier
# handler installation (e.g. uvicorn CLI bootstrap) so the level
# change is observable regardless of import order.
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    force=True,
)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded

from app.api.router import api_router
from app.core.config import settings
from app.middleware.auth import ApiKeyAuthMiddleware
from app.middleware.session_cookie_auth import SessionCookieAuthMiddleware
from app.middleware.rate_limit import (
    limiter,
    rate_limit_exceeded_handler,
    create_rate_limit_middleware,
)
from app.repositories.audit_chain import install_audit_chain_event

# Step 29.y gap-fix C13 (D-celery-app-not-imported-on-uvicorn-boot-2026-05-07):
# Import the configured Celery app at uvicorn boot. Without this import, the
# `@shared_task` decorator on `extract_memory_from_turn` (and any other task)
# resolves to Celery's default `current_app`, whose default broker URL is
# `amqp://guest@localhost//` — the wrong protocol AND the wrong port for our
# Redis-broker dev setup (and SQS in prod). The latent failure mode is that
# the FIRST producer-side `apply_async` call on a fresh uvicorn process
# raises `kombu.exceptions.OperationalError` because it tries to publish to
# AMQP/5672 instead of Redis/6379 (or the configured SQS endpoint). Symptom
# is a 500 on any chat-turn that triggers async memory extraction OR the
# Pillar 25 worker-pipeline-probe route. Fix: import here so the configured
# Celery app is registered as `current_app` before any task module is loaded.
# noqa: F401 — import is for side effects only.
from app.worker.celery_app import celery_app  # noqa: F401

# Step 28 P3-E.2 / Pillar 23: tamper-evident hash chain on every audit
# row. The before_flush event populates row_hash / prev_row_hash on
# every AdminAuditLog instance pending in any session. Installed here
# at module-import time so every ORM session created downstream
# (FastAPI requests, scripts that import from app.*) inherits it.
# Worker processes install the event in their own bootstrap (worker
# does not import app.main).
install_audit_chain_event()

app = FastAPI(title=settings.app_name)

# Attach limiter to app state (required by SlowAPI)
app.state.limiter = limiter

# Register the clean 429 handler for normal rate limit violations
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# Middleware mount order:
#   - Starlette/FastAPI runs LAST-ADDED OUTERMOST, so the request
#     dispatch order on the way in is the REVERSE of the add order.
#   - We want: RateLimitFallback (outermost) -> SessionCookieAuth ->
#     ApiKeyAuth -> route. That requires the ADD order below.
#
# Step 31.2 commit A: SessionCookieAuthMiddleware bridges the Step 30a
# session cookie into request.state on /api/v1/admin/* and
# /api/v1/dashboard/*, tagging the request with auth_method="cookie".
# When ApiKeyAuthMiddleware runs after it, it checks that tag and
# short-circuits if cookie auth already populated state. Cookie auth
# falls through silently for non-cookied requests, preserving the
# existing API-key contract for every server-to-server caller.
app.add_middleware(ApiKeyAuthMiddleware)
app.add_middleware(SessionCookieAuthMiddleware)

# Add the fallback middleware — catches Redis outages and fails open
RateLimitFallbackMiddleware = create_rate_limit_middleware()
app.add_middleware(RateLimitFallbackMiddleware)

# Step 30a.2-pilot Commit 3d — CORSMiddleware mounted LAST so it ends up
# OUTERMOST in the dispatch chain. The browser's CORS preflight
# (OPTIONS + Access-Control-Request-Method header) must short-circuit
# BEFORE auth middleware runs, otherwise SessionCookieAuthMiddleware /
# ApiKeyAuthMiddleware will 401 the preflight and the browser will refuse
# to send the real request. starlette's CORSMiddleware answers preflight
# directly without forwarding downstream.
#
# Settings:
#   allow_origins        — settings.cors_allowed_origins. Both apex and www
#                          vantagemind.ai are in the default list so the
#                          marketing site and any apex-issued fetch both work.
#   allow_credentials    — True. Required because the SPA uses
#                          credentials: "include" on every billing call so the
#                          Step 30a session cookie is sent (GET /me, POST
#                          /portal, POST /logout, POST /pilot-refund).
#                          Per the CORS spec, allow_credentials=True forbids
#                          a wildcard origin — which is fine, we use an
#                          explicit allowlist.
#   allow_methods        — narrow set actually used by the SPA (GET/POST/OPTIONS).
#                          Not '*' because allow_credentials=True forbids that too.
#   allow_headers        — narrow set actually sent by the SPA. "Authorization"
#                          is included for the admin-key-via-bearer pattern
#                          (currently unused by the SPA but kept future-safe).
#   max_age              — 600s. Lets the browser cache the preflight result
#                          so repeat checkout submits don't pay an extra RTT.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Accept", "Authorization"],
    max_age=600,
)

# Register all API routes
app.include_router(api_router, prefix=settings.api_v1_prefix)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": settings.app_name}