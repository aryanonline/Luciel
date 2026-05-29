"""
Authentication middleware.

Validates API keys on incoming requests and injects admin_id, domain_id,
agent_id, luciel_instance_id, and (Step 24.5b) user_id into the request
state.

PATCHED (Step 21): Admin routes are no longer skipped. They now require a
valid API key with 'admin' in permissions.

PATCHED (Step 24.5 File 10.5): Inject key_prefix + actor_label for audit.

PATCHED (Step 24.5 File 14): Inject luciel_instance_id onto request.state
so chat_service (File 15) can resolve persona / provider / tools / prompt
from the bound LucielInstance. None for legacy / unbound keys -> legacy
fallback path in chat_service.

PATCHED (Step 30b commit (c)): Inject the four widget-related fields
from api_keys onto request.state for downstream consumption by the
widget endpoint. The middleware does NOT enforce origin or key_kind
here -- the existing middleware is shared by every authenticated
route and admin/server-to-server keys must continue to flow without
origin checks. Enforcement lives in app/api/widget_deps.py, scoped
only to the widget endpoint that needs it.

PATCHED (Step 24.5b File 2.4): Inject actor_user_id onto request.state.
Resolved from the Agent row keyed by (admin_id, domain_id, agent_id) when
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
# Arc 5 Path A — AgentRepository deleted at Commit A5. The actor_user_id
# resolution block below previously walked (admin_id, domain_id, agent_id)
# → Agent.user_id. V2 has no Agent layer; the V2 resolution path is
# (admin_id, instance_id) → ScopeAssignment.user_id, which B2 will land.
# Until then the actor_user_id stays None for non-cookied paths (which
# matches the pre-Step-24.5b behaviour and is safe — audit rows that
# need actor_user_id are written from the cookied path that already
# resolves it from request.state).
from app.services.api_key_service import ApiKeyService

logger = logging.getLogger(__name__)


SKIP_AUTH_PATHS = {
    "/health",
    # Arc 8 Commit 1 (WU-1 Reliability): /ready readiness probe — same posture
    # as /health (no auth gate) so the Arc 8 Commit 4 in-cluster Fargate
    # deploy-gate smoke probe and uptime monitors can hit it without holding
    # a JWT. The endpoint itself is rate-limited and exposes only subsystem
    # status (no connection strings or internals). Closes
    # D-health-endpoint-shallow-no-db-readiness-check-2026-05-22.
    "/ready",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/api/v1/version",
    # Step 30a -- subscription billing routes. These are callable by:
    #   * the marketing site (no api key; relies on cookie or no auth)
    #   * Stripe (no api key; signature-verified inside the route)
    #   * the cookied buyer post-login (cookie verified inside the route)
    # The api-key middleware is the wrong perimeter for any of those
    # callers, so we exempt the billing namespace here and let each
    # route enforce its own gate (Stripe signature, cookie validation,
    # or none for the public-by-design checkout/claim endpoints).
    "/api/v1/billing",
    # Step 30a.3 -- password-auth routes (login, set-password,
    # forgot-password). Same exemption rationale as /api/v1/billing:
    # these routes are reachable by an anonymous client (the buyer
    # who has not yet been issued a session cookie), and each route
    # enforces its own gate inside the handler (password verify,
    # JWT token-class consume, or always-200 for forgot-password).
    # The api-key middleware is the wrong perimeter here.
    "/api/v1/auth",
    # Arc 8 WU-6 Phase C -- SES feedback / suppression sink. This
    # route is POSTed by AWS SNS (no api key); the trust gate is the
    # two-check defence inside the route (TopicArn allowlist +
    # SigningCertURL host check). The api-key middleware is the
    # wrong perimeter here.
    "/api/v1/ses-events",
}

# Step 31 sub-branch 3: dashboard reads are admin-side observability.
# Mounting `/api/v1/dashboard` under ADMIN_AUTH_PATHS gives us the same
# perimeter denial that `/api/v1/admin/*` enjoys -- embed keys (whose
# `EMBED_REQUIRED_PERMISSIONS = {"chat"}` excludes `admin`) get a 403
# from this middleware before any route handler runs. ScopePolicy still
# enforces tenant/domain/agent isolation inside each handler; this is
# defense-in-depth.
ADMIN_AUTH_PATHS = ("/api/v1/admin", "/api/v1/dashboard")


class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        for skip_path in SKIP_AUTH_PATHS:
            if path.startswith(skip_path) or path == skip_path:
                return await call_next(request)

        # Step 31.2 commit A: if SessionCookieAuthMiddleware already
        # authenticated this request via a Step 30a session cookie,
        # request.state.auth_method is set to "cookie" and request.state
        # already carries admin_id / permissions / actor_user_id /
        # actor_label. Treat this as a pre-authenticated request and
        # skip the Authorization header check entirely. The cookie
        # middleware's path filter (COOKIE_AUTH_PATHS) restricts this
        # short-circuit to /api/v1/admin/* and /api/v1/dashboard/* --
        # the widget and other paths are never reached via cookie.
        if getattr(request.state, "auth_method", None) == "cookie":
            return await call_next(request)

        # Step 30b commit (c): CORS preflight has no Authorization
        # header by spec; let it pass through to the route handler
        # which answers with the CORS-allowed headers (origin check
        # happens against any active embed key's allowlist there).
        # This applies only to the widget endpoint -- other endpoints
        # are not browser-callable and never see OPTIONS.
        if request.method == "OPTIONS" and path == "/api/v1/chat/widget":
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

            admin_id = apikey.admin_id
            # Arc 12 EX1a — V2 auth subject is admin_id (+ luciel_instance_id
            # where instance-scoping applies). The legacy domain_id / agent_id
            # api_key columns are read by other layers (forensics/dashboard)
            # which a sibling EX-step rewrites; the auth layer no longer
            # reads them and no longer stamps request.state.{domain,agent}_id.
            api_key_id = apikey.id
            permissions = apikey.permissions or []
            key_prefix = apikey.key_prefix
            actor_label = apikey.created_by
            luciel_instance_id = apikey.luciel_instance_id

            # Step 30b commit (c): widget-related fields. NOT enforced here;
            # surfaced for the widget endpoint dependency to consume. Existing
            # admin keys carry key_kind='admin' (column default) and NULLs for
            # the other three -- the widget endpoint rejects any non-'embed'
            # key, so admin keys cannot accidentally drive widget traffic.
            key_kind = getattr(apikey, "key_kind", "admin") or "admin"
            allowed_origins = getattr(apikey, "allowed_origins", None)
            rate_limit_per_minute = getattr(apikey, "rate_limit_per_minute", None)
            widget_config = getattr(apikey, "widget_config", None)

            # Step 24.5b: resolve user_id from the bound Agent row.
            # Only agent-scoped keys carry a meaningful user_id;
            # tenant-admin / platform-admin / chat-key-only paths
            # leave user_id=None. The Agent natural-key lookup hits
            # the existing uq_agents_tenant_domain_agent unique
            # constraint composite index -- ~1ms.
            # Arc 5 Path A (Commit A5) — Agent layer eliminated; the
            # legacy (admin_id, domain_id, agent_id) → Agent.user_id
            # resolution path is gone. V2 actor_user_id resolution
            # walks ScopeAssignment by (admin_id, instance_id) instead;
            # that B2 rewrite lands alongside the cascade-spine V2
            # collapse. Until then leave actor_user_id=None on the
            # API-key middleware path (cookied path resolves it
            # independently from the session cookie).
            actor_user_id = None

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

        request.state.admin_id = admin_id
        # Arc 12 EX1a — request.state.domain_id / agent_id are NO LONGER
        # stamped. V2 auth subject = admin_id (+ luciel_instance_id).
        # Downstream code that still calls
        # ``getattr(request.state, "domain_id", None)`` already treats the
        # absent attribute as ``None`` (V2 collapse). ScopePolicy._caller
        # was rewritten at Revision B to return ``None`` for those slots
        # regardless of request.state. The api-key columns still exist on
        # the row (later EX-step drops them); this layer just stops
        # forwarding their values into request.state.
        request.state.api_key_id = api_key_id
        request.state.permissions = permissions
        request.state.key_prefix = key_prefix
        request.state.actor_label = actor_label
        request.state.luciel_instance_id = luciel_instance_id
        request.state.actor_user_id = actor_user_id  # Step 24.5b

        # Arc 11 Cleanup C item #8 — API-key callers don't resolve a
        # User on this path (Step 24.5b doctrine), so no
        # scope_assignments to load; set ``[]`` for shape uniformity
        # so ScopePolicy's middleware-first resolution sees a list
        # rather than an unset attribute.
        request.state.scope_assignments = []

        # Step 30b commit (c): widget fields surfaced for the widget
        # endpoint dependency. Other routes ignore these.
        request.state.key_kind = key_kind
        request.state.allowed_origins = allowed_origins
        request.state.rate_limit_per_minute = rate_limit_per_minute
        request.state.widget_config = widget_config

        return await call_next(request)