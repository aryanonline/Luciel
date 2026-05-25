"""Arc 9 WS4c -- Structured request logging.

THE GAP THIS CLOSES
===================
ARC9_C12-C22_CORRIGENDUM §7 carry-forward:

    "Structured error logging (request_id, user_id, tenant_id, route,
     status, detail) -- DEFERRED."

The first Free-signup demo (C19 -> C22) wasted ~5 hours of debugging
because the production CloudWatch stream emitted unstructured one-liners
per request -- different format per code path, no stable correlation
id, no tenant context on the failure rows. We could see "something 500'd"
but not "which user, on which tenant, on which route, with what
exception class". Identity bootstrap also needed before-and-after
visibility (was the GUC bound? was the snapshot empty?) and we had
none.

WS4c installs a single OUTERMOST middleware that emits exactly one
structured log line per HTTP request, in a stable JSON shape every
downstream tool (CloudWatch Insights, Datadog, jq pipelines) can parse:

    {
        "evt":         "http_request",
        "request_id":  "<uuid4 OR X-Request-ID inbound>",
        "method":      "POST",
        "route":       "/api/v1/admin/instances",
        "status":      201,
        "duration_ms": 38,
        "user_id":     "62e9d099-...",   # None if unauth'd / not resolved
        "tenant_id":   "free-56c7f4e5",  # None if pre-bootstrap / no scope
        "auth_method": "cookie",         # 'cookie' | 'api_key' | None
        "client_ip":   "203.0.113.7",
        "detail":      "...",            # set only on error paths
    }

CONTRACT
========

* OUTERMOST middleware. Mounted last in app/main.py so it dispatches
  first on the way in and last on the way out -- it must observe the
  final status code (including 5xx raised from inner middleware / route
  handlers), and it must run AFTER auth middlewares so that user_id /
  tenant_id / auth_method are populated on request.state when we read
  them.

* Sets ``request.state.request_id`` if not already set. Echoes it back
  to the client as ``X-Request-ID`` on every response so customers can
  quote it in support tickets and we can grep CloudWatch for it.

* If an inbound ``X-Request-ID`` header is present and well-formed
  (UUID or alphanumeric+dash, <= 64 chars), we propagate it rather than
  generating a new one. This lets upstream callers (load balancers,
  partner platforms) carry their correlation id through.

* On exception inside the request, we log status=500 and detail=<class>
  then re-raise so the existing FastAPI exception handlers still serve
  the JSON body. The exception itself is NOT swallowed.

* Skips ``/health`` and ``/ready`` (high-frequency probes; logging
  them at INFO would flood CloudWatch and obscure real events).

* Uses Python's stdlib ``logging`` with the dedicated logger name
  ``luciel.request`` so CloudWatch Metric Filters / Insights can route
  on it cleanly.

* Per repository discipline: no broad excepts that swallow, no HTTP
  exceptions raised from here, no mutation of request bodies, no
  reading of request.body() (which would consume the stream and
  break downstream handlers).
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Optional

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware


logger = logging.getLogger("luciel.request")


# Paths we deliberately do NOT log. ALB health-checks /health every
# few seconds and the deploy-gate probe hits /ready on the same
# cadence -- logging them at INFO would dwarf real-traffic events
# in CloudWatch and triple our log storage cost.
_LOG_SKIP_PATHS = frozenset(
    {
        "/health",
        "/ready",
    }
)


# Whitelist for inbound X-Request-ID to avoid log-injection.
# Any value that doesn't match gets discarded and we generate a fresh
# uuid4 instead. Conservative -- only chars that can't break a JSON
# log line or a CloudWatch Insights query.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def _coerce_request_id(raw: Optional[str]) -> str:
    """Return a safe request_id -- inbound if valid, else new uuid4."""
    if raw and _REQUEST_ID_RE.match(raw):
        return raw
    return uuid.uuid4().hex


def _client_ip(request: Request) -> Optional[str]:
    """Best-effort client IP. Trusts X-Forwarded-For first hop.

    The ALB always overwrites X-Forwarded-For (it does not trust
    client-supplied values), and the first hop in the resulting list
    is the real client IP. Outside the ALB (local dev, tests), we
    fall back to request.client.host.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # Take only the first IP; the rest are proxies.
        first = xff.split(",", 1)[0].strip()
        if first:
            return first
    client = request.client
    return client.host if client else None


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Emit one structured log line per HTTP request.

    Mount OUTERMOST so it runs first on the way in (assigns request_id
    early enough for inner middlewares to log against it) and last on
    the way out (sees the final status code, including any 5xx raised
    inside route handlers).
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Assign / propagate request_id BEFORE any inner middleware
        # runs -- if SessionCookieAuth or ApiKeyAuth want to log
        # something correlated, they can read request.state.request_id.
        request_id = _coerce_request_id(
            request.headers.get("x-request-id")
        )
        request.state.request_id = request_id

        # Skip-list short-circuit: still set request_id (so the
        # response carries the header) but do not emit a log line.
        if path in _LOG_SKIP_PATHS:
            try:
                response: Response = await call_next(request)
            except Exception:
                raise
            response.headers["X-Request-ID"] = request_id
            return response

        start = time.monotonic()
        status: int = 500
        detail: Optional[str] = None

        try:
            response = await call_next(request)
            status = response.status_code
            return response
        except Exception as exc:  # noqa: BLE001 -- intentional log & reraise
            # Capture the exception class name (NOT str(exc), which can
            # contain SQL fragments / connection strings / row data).
            # Re-raise so FastAPI's exception handlers still produce
            # the JSON body the client expects.
            detail = type(exc).__name__
            status = 500
            raise
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)

            # Pull whatever auth middlewares populated. All four are
            # set in request.state by ApiKeyAuthMiddleware /
            # SessionCookieAuthMiddleware when auth succeeds; None
            # otherwise. We tolerate AttributeError defensively for
            # the unauth'd path that never visited an auth middleware.
            state = request.state
            user_id = getattr(state, "user_id", None)
            tenant_id = getattr(state, "tenant_id", None)
            auth_method = getattr(state, "auth_method", None)

            # On a 4xx/5xx without an exception, lift the response
            # body's "detail" field IF it's a small string. Don't
            # peek at the body for 2xx/3xx -- normal traffic is
            # noise.
            if status >= 400 and detail is None:
                # FastAPI doesn't give us trivial access to the
                # response body here without re-buffering. Leave
                # detail unset; the route handler is expected to
                # log its own context if it wants more than the
                # status code captured.
                pass

            payload = {
                "evt": "http_request",
                "request_id": request_id,
                "method": request.method,
                "route": path,
                "status": status,
                "duration_ms": duration_ms,
                "user_id": str(user_id) if user_id is not None else None,
                "tenant_id": (
                    str(tenant_id) if tenant_id is not None else None
                ),
                "auth_method": auth_method,
                "client_ip": _client_ip(request),
                "detail": detail,
            }

            # Choose level based on status. 5xx / unhandled exception
            # is ERROR; 4xx is WARNING; otherwise INFO. CloudWatch
            # metric filters can pivot on level + evt.
            if status >= 500 or detail is not None:
                level = logging.ERROR
            elif status >= 400:
                level = logging.WARNING
            else:
                level = logging.INFO

            # json.dumps with default=str protects us from non-JSON
            # values that may sneak in (e.g. a UUID instance that
            # bypassed the str() coercion above). ensure_ascii=False
            # keeps non-ASCII paths readable in CloudWatch.
            try:
                msg = json.dumps(payload, ensure_ascii=False, default=str)
            except Exception:  # pragma: no cover -- defensive
                # If even this fails, fall back to a stringified dict.
                # Better a degraded log line than a missing one.
                msg = repr(payload)

            logger.log(level, msg)

            # Echo the request_id on the response so customers can
            # quote it in support tickets. Only when a response was
            # successfully produced -- on raised exceptions, FastAPI's
            # default handler builds the response without touching us
            # so we can't attach the header here.
            if status < 500 or detail is None:
                # The response variable exists if call_next returned
                # without raising. Guard via locals() to keep mypy and
                # the runtime happy on the exception path.
                resp = locals().get("response")
                if resp is not None:
                    resp.headers["X-Request-ID"] = request_id


__all__ = ["RequestLoggingMiddleware"]
