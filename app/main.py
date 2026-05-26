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

from typing import Any

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
from app.middleware.request_logging import RequestLoggingMiddleware
from app.repositories.audit_chain import install_audit_chain_event

# Arc 8 Commit 1 (WU-1 Reliability): dedicated logger for the /ready probe so
# probe-failure rows land under a stable name in CloudWatch (luciel.ready.*)
# rather than the unstructured root logger.
ready_logger = logging.getLogger("luciel.ready")

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

# Arc 9 WS4c — Structured request logging. Mounted AFTER rate-limit
# (so it observes 429 denials) and BEFORE CORS (so CORS preflight
# short-circuits without us logging every browser OPTIONS as a real
# request). Emits one JSON-shaped INFO/WARN/ERROR line per request
# with stable fields (request_id, method, route, status, duration_ms,
# user_id, admin_id, auth_method, client_ip, detail) -- see
# app/middleware/request_logging.py header for the full contract.
app.add_middleware(RequestLoggingMiddleware)

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
#   allow_methods        — narrow set actually used by the SPA (GET/POST/PATCH/
#                          DELETE/OPTIONS). Step 30a.4 hot-fix (D-cors-delete-
#                          method-blocked-2026-05-17): DELETE and PATCH added
#                          because /app/team's Revoke button calls DELETE
#                          /api/v1/admin/invites/{id} cross-origin; the
#                          browser preflight was returning 'GET, POST, OPTIONS'
#                          and refusing to send the real DELETE, producing
#                          the generic 'Couldn't revoke invite' toast (the
#                          non-AdminApiError branch in src/lib/admin.ts).
#                          PATCH is included pre-emptively because
#                          /admin/tenants/{id} and /admin/domains/{...} use
#                          PATCH and may be wired into cookied admin UIs in
#                          a follow-up step.
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
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Accept", "Authorization"],
    max_age=600,
)

# Register all API routes
app.include_router(api_router, prefix=settings.api_v1_prefix)


@app.get("/health")
def health() -> dict:
    """Liveness probe — process is up.

    Strict process-liveness only. No external dependencies (no DB, no Redis,
    no SES). The ALB target-group health check binds to this endpoint, so
    any external-dependency failure must NOT remove a healthy task from the
    target group — that would amplify a downstream outage into a full-cluster
    outage. Downstream-dependency probing lives at ``/ready`` (Arc 8 Commit 1).
    """
    return {"status": "ok", "service": settings.app_name}


# Arc 8 Commit 1 (WU-1 Reliability): ``/ready`` readiness probe.
#
# Distinct from ``/health`` (liveness) by design. The ALB target-group health
# check stays bound to ``/health`` because a transient Redis or RDS blip must
# not remove a healthy task from rotation (that amplifies a downstream outage
# into a full-cluster outage). ``/ready`` is a richer signal consumed by:
#
#   - the Arc 8 Commit 4 in-cluster Fargate deploy-gate smoke probe (which
#     gates rolling-deploy completion on actual ALB→app→DB+Redis reachability,
#     closing ``D-no-internal-smoke-path-for-direct-alb-2026-05-22``);
#   - uptime monitors that want a true "can serve a real request" signal
#     rather than just "the process answers TCP";
#   - human operators investigating customer-reported slowdowns who need to
#     localise the failure to DB, Redis, or app-layer before paging deeper.
#
# Closes ``D-health-endpoint-shallow-no-db-readiness-check-2026-05-22``.
#
# Probe shape:
#   - DB:    ``SELECT 1`` through the shared SQLAlchemy engine; bounded by the
#            engine's own pool_pre_ping timeout (~1.5s in default psycopg2
#            settings) and our explicit ``connect_timeout`` query parameter
#            on ``DATABASE_URL`` (set in prod via SSM, defaults to 5s).
#   - Redis: ``PING`` against ``settings.redis_url`` with 1.0s socket timeouts.
#            We construct a one-shot client (NOT the shared limiter pool) so a
#            slow-probe scenario can never starve the limiter of pool slots.
#
# Failure posture (return 503 with a structured body):
#   {
#     "status": "not_ready",
#     "checks": {"db": "ok" | "<err-class>", "redis": "ok" | "<err-class>"}
#   }
# The body never leaks the underlying exception message (which can carry
# connection strings or table names) — only the exception class name.
#
# Public surface — no auth gate (same posture as ``/health`` and ``/version``).
# Listed in ApiKeyAuthMiddleware.SKIP_AUTH_PATHS via the existing ``/health``
# entry pattern + an explicit ``/ready`` entry added in this commit.
@app.get("/ready")
def ready() -> Any:  # noqa: ANN401 — FastAPI handles JSONResponse return path
    """Readiness probe — process can serve a real request end-to-end."""
    from fastapi.responses import JSONResponse

    checks: dict[str, str] = {"db": "ok", "redis": "ok"}
    failed = False

    # DB probe — SELECT 1 through the shared engine. We deliberately use the
    # engine directly (not a SessionLocal scope) so we don't pay the per-call
    # session bootstrap cost (audit-chain listener install check, etc.) on
    # every probe.
    try:
        from sqlalchemy import text as _sa_text
        from app.db.session import engine as _db_engine

        with _db_engine.connect() as _conn:
            _conn.execute(_sa_text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — probe must surface any failure
        checks["db"] = type(exc).__name__
        failed = True
        ready_logger.warning("ready_probe_db_failed exc=%s", type(exc).__name__)

    # Redis probe — one-shot client at settings.redis_url. Construct fresh so
    # we never starve the limiter's shared pool; tight socket timeouts so a
    # genuinely-unreachable Redis returns 503 in ~1s rather than stalling the
    # probe (and the deploy gate) for the OS connect timeout.
    if settings.redis_url:
        try:
            import redis as _redis_pkg

            _r = _redis_pkg.Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
            )
            try:
                _r.ping()
            finally:
                try:
                    _r.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            checks["redis"] = type(exc).__name__
            failed = True
            ready_logger.warning(
                "ready_probe_redis_failed exc=%s", type(exc).__name__
            )
    else:
        # No Redis configured (dev fallback to memory:// in the limiter).
        # That's fine — readiness is still "ok" because the limiter and any
        # other consumer will degrade gracefully. We label it explicitly so
        # a probe reader can tell "Redis not configured" from "Redis ok".
        checks["redis"] = "not_configured"

    if failed:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "checks": checks},
        )
    return {"status": "ready", "checks": checks}