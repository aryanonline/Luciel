"""Session-cookie authentication middleware (Step 31.2 commit A).

This middleware bridges the Step 30a magic-link session cookie into the
same `request.state` shape that `ApiKeyAuthMiddleware` populates for
admin API keys. After this middleware runs, downstream code (routes,
`AuditContext.from_request`, `ScopePolicy.enforce_*_scope`) cannot tell
whether the caller authenticated with a cookie or an admin API key --
they read the same fields off `request.state`.

Why a sibling middleware rather than extending `ApiKeyAuthMiddleware`
----------------------------------------------------------------------
The existing middleware rejects any request missing an `Authorization:
Bearer ...` header on protected paths. A cookied browser request has no
Authorization header by design (cookies are the auth vector). Rather
than re-shape the rejection logic, we run cookie auth BEFORE the api-key
middleware and short-circuit `call_next` if the cookie is valid. If the
cookie is missing, malformed, or invalid we fall through silently and
let the api-key middleware do its job -- which preserves the existing
behaviour for server-to-server / agent / admin-API-key callers.

Cookie reach is intentionally narrow
------------------------------------
At v1 the cookie authenticates the following path prefixes ONLY:

  * /api/v1/admin/*       -- customer admin actions (instance CRUD,
                             embed-key minting, dashboards reads via
                             the admin envelope)
  * /api/v1/dashboard/*   -- the Step 31 dashboard read endpoints

The cookie deliberately does NOT authenticate:

  * /api/v1/chat/*        -- widget chat MUST stay embed-key-only;
                             allowing cookies here would let a logged-in
                             customer's browser drive widget conversation
                             traffic outside the embed-key scope envelope
  * /api/v1/billing/*     -- already handled by per-route cookie checks
                             in app/api/v1/billing.py; we don't double-
                             handle it here

Audit attribution
-----------------
Cookied admin actions write `admin_audit_logs` rows with:

  * actor_key_prefix = NULL              (no API key was used)
  * actor_user_id    = <User.id UUID>    (the cookied user)
  * actor_label      = "cookie:<email>"  (human-readable provenance)
  * actor_permissions= ("admin","chat","sessions") tuple

The `actor_key_prefix` column is already `Mapped[str | None]` on
`admin_audit_logs` (Step 24 schema), so NO migration is required for
this commit. Pillar 4 audit invariants still hold: every cookied
mutation pairs an `admin_audit_logs` row inside the same transaction
via the existing `before_flush` listener path.

Scope shape
-----------
A cookied customer is treated as a *tenant-admin* caller -- they see
their own tenant, all domains within it, all agents and instances
within those domains. They MAY NOT pass `?tenant_id=other-tenant`; the
`ScopePolicy.enforce_tenant_scope` call in each route rejects that with
the same 403 it returns for cross-tenant API-key calls. Platform-admin
operations stay API-key-only at v1 (no platform admin can log in via
cookie because there is no platform-admin user row at v1).

What happens when a cookied user has no subscription
----------------------------------------------------
This is an edge case that should not happen in steady state but can
occur if a webhook race lands the cookie before the subscription row
commits (we mint the magic link inside the same txn as the subscription
write, so this window is microseconds, but it exists). We treat it as
401 with a clear message; the marketing site clears the cookie and
shows a friendly "Try again in a moment" panel.
"""
from __future__ import annotations

import logging

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings
from app.db.session import SessionLocal
from app.integrations.stripe import get_stripe_client
from app.models.user import User
from app.services.billing_service import BillingService
from app.services.magic_link_service import MagicLinkError, validate_session_token

logger = logging.getLogger(__name__)


# Path prefixes the cookie is allowed to authenticate. Anything else
# falls through to ApiKeyAuthMiddleware unchanged.
COOKIE_AUTH_PATHS: tuple[str, ...] = (
    "/api/v1/admin",
    "/api/v1/dashboard",
)


# Cookied callers are minted with this fixed permission tuple. It mirrors
# the permission set that `OnboardingService.onboard_tenant` grants to
# the tenant's admin API key (`["chat","sessions","admin"]`), so a
# cookied request can do anything the admin key can do at its own tenant
# scope. This list is server-set; we never read it from the cookie payload.
COOKIE_PERMISSIONS: tuple[str, ...] = ("admin", "chat", "sessions")


class SessionCookieAuthMiddleware(BaseHTTPMiddleware):
    """Bridge the Step 30a session cookie into `request.state`.

    Runs BEFORE `ApiKeyAuthMiddleware`. If the request has a valid cookie
    on a cookie-eligible path, populate `request.state` and call through.
    Otherwise fall through to the next middleware unchanged.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Only attempt cookie auth on the explicit allowlist. For every
        # other path (chat, billing, health, etc.) we are invisible.
        if not any(path.startswith(prefix) for prefix in COOKIE_AUTH_PATHS):
            return await call_next(request)

        cookie = request.cookies.get(settings.session_cookie_name)
        if not cookie:
            # No cookie -- let ApiKeyAuthMiddleware handle the request.
            # If the caller has no API key either, that middleware
            # returns the 401 with the "Missing or invalid Authorization
            # header" message, preserving the existing contract.
            return await call_next(request)

        # Validate the cookie. Malformed/expired -> fall through to api-
        # key middleware. We do NOT 401 here because a request might
        # carry both a stale cookie AND a valid API key (unusual but
        # possible during cookie/key migration windows); the api-key
        # middleware is the authoritative gate when the cookie is bad.
        try:
            payload = validate_session_token(cookie)
        except MagicLinkError as exc:
            logger.debug("cookie auth: invalid session cookie on %s: %s", path, exc)
            return await call_next(request)

        user_id = payload.get("sub")
        if not user_id:
            logger.warning("cookie auth: cookie payload missing 'sub' on %s", path)
            return await call_next(request)

        # Resolve the User row + their active tenant_id. We open a short
        # DB session here (same shape as ApiKeyAuthMiddleware) and
        # release it before invoking the route. The route gets its own
        # request-scoped session via the existing `DbSession` dependency.
        db = SessionLocal()
        try:
            user = db.get(User, user_id)
            if user is None or not user.active:
                # Cookie references a User we no longer recognise. This
                # is rare (we deactivate via cascade on subscription
                # cancellation, which also clears the cookie at the
                # browser via /billing/logout, but a cookie issued
                # before deactivation could still arrive). Return 401
                # with a clear message; the marketing site clears the
                # cookie and shows the login screen.
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Session user not found or inactive."},
                )

            # BillingService requires (db, stripe_client). The stripe
            # client is a process-singleton via get_stripe_client(); the
            # subscription read below does not actually touch Stripe (it
            # is a pure DB lookup), but the constructor contract is what
            # it is and we honor it. Mirror the canonical construction
            # pattern in app/api/v1/billing.py:_service.
            #
            # D-cookie-middleware-billingservice-missing-stripe-client-
            # 2026-05-16: prior to this commit the middleware passed
            # only `db` and hit `TypeError: BillingService.__init__()
            # missing 1 required positional argument: 'stripe_client'`,
            # which the broad `except Exception` below swallowed and
            # logged as ERROR. The fall-through let ApiKeyAuthMiddleware
            # return its 'Missing Bearer' 401 to every cookied dashboard
            # / admin request -- silently breaking the entire cookied
            # browser flow since whenever BillingService gained the
            # required arg. T9 leg 5 surfaced it on prod.
            svc = BillingService(db, get_stripe_client())
            sub = svc.get_active_subscription_for_user(user_id=user.id)
            if sub is None:
                # User exists but has no active subscription. Possible
                # if a checkout webhook is still in flight (microseconds
                # window) or if their subscription was just cancelled.
                # 401 is the right code; the marketing site can show a
                # "Subscription not active" panel and a "Manage billing"
                # link that exercises the /billing/portal path (which
                # cookied users can still reach by per-route cookie
                # check, bypassing this middleware via the path filter
                # above).
                return JSONResponse(
                    status_code=401,
                    content={"detail": "No active subscription for this user."},
                )

            tenant_id = sub.tenant_id

            # Cookied callers are tenant-admin. They never carry a
            # domain or agent scope at v1 -- those are concepts for
            # scoped API keys minted within the tenant. If a tenant
            # later adds department-scoped logins (Step 30a.1 multi-
            # seat work), we'll extend this to read scope from the
            # cookie payload or a ScopeAssignment row.
            domain_id: str | None = None
            agent_id: str | None = None
            luciel_instance_id: str | None = None

            # Audit fields. key_prefix is None for cookied actions --
            # the existing nullable column on admin_audit_logs absorbs
            # this without a migration. actor_label encodes the
            # provenance so an auditor can trace a row to its cookied
            # user without joining to users.
            key_prefix: str | None = None
            actor_label = f"cookie:{user.email}"
            actor_user_id = user.id

            # Widget-related fields are meaningful only for embed keys;
            # cookied requests never drive widget traffic (path filter
            # excludes /api/v1/chat), so we set them to None / sensible
            # defaults. key_kind="cookie" is novel; downstream code
            # checks key_kind only on the widget path which we do not
            # reach via cookie.
            request.state.tenant_id = tenant_id
            request.state.domain_id = domain_id
            request.state.agent_id = agent_id
            request.state.api_key_id = None
            request.state.permissions = list(COOKIE_PERMISSIONS)
            request.state.key_prefix = key_prefix
            request.state.actor_label = actor_label
            request.state.luciel_instance_id = luciel_instance_id
            request.state.actor_user_id = actor_user_id

            request.state.key_kind = "cookie"
            request.state.allowed_origins = None
            request.state.rate_limit_per_minute = None
            request.state.widget_config = None

            # A marker downstream code can check if it needs to
            # distinguish a cookied caller from an API-key caller. Most
            # code paths should NOT need to -- they should treat
            # `request.state` opaquely -- but rate-limiter key
            # selection and a small number of forensic paths benefit
            # from knowing the auth vector.
            request.state.auth_method = "cookie"

        except Exception as exc:
            logger.error("cookie auth middleware error on %s: %s", path, exc)
            # Fall through to api-key middleware on infra errors rather
            # than 500-ing. If the api-key middleware also fails it
            # will return its own 500. This preserves the existing
            # observability surface.
            try:
                db.rollback()
            except Exception:
                pass
            db.close()
            return await call_next(request)
        finally:
            try:
                if db.is_active:
                    db.rollback()
            except Exception:
                pass
            db.close()

        # SHORT-CIRCUIT: skip ApiKeyAuthMiddleware. We achieve this by
        # tagging the request and having the api-key middleware check
        # the tag (commit B of this branch wires that check). For now,
        # we set the tag and let call_next handle dispatch.
        return await call_next(request)
