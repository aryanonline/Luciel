"""
Step 30a.2-pilot Commit 3d — CORS preflight contract tests.

D-cors-middleware-missing-on-checkout-preflight-2026-05-15:
  The first cross-origin POST in the app
  (POST /api/v1/billing/checkout with Content-Type: application/json from
  https://www.vantagemind.ai) triggered a CORS preflight that returned
  405 Method Not Allowed because no CORSMiddleware was installed in
  app/main.py. The browser then refused to send the actual POST. These
  tests pin the contract so a future bare deploy or a middleware reorder
  cannot silently regress us.

Why these three cases are the right shape:

  test_preflight_from_allowed_origin_returns_cors_headers
    The exact preflight the production SPA emits: OPTIONS with
    Access-Control-Request-Method=POST and an Origin header that matches
    settings.cors_allowed_origins. starlette's CORSMiddleware must answer
    200 with the matching access-control-allow-origin and
    access-control-allow-credentials: true header. Without those two
    headers, the browser short-circuits the real POST and the SPA shows
    "We couldn't reach billing". This is the regression case for the bug.

  test_preflight_from_disallowed_origin_omits_acao
    A preflight from an origin NOT in the allowlist must NOT come back
    with an access-control-allow-origin header. CORSMiddleware will still
    answer 200 (per the spec — the preflight is a normal HTTP request)
    but it must not advertise itself as cross-origin friendly to the
    rogue origin. This is the security regression case: a future
    "allow_origins=['*']" mistake would fail this test.

  test_actual_post_from_allowed_origin_still_hits_route
    Defense-in-depth: prove the middleware mount order didn't accidentally
    swallow the real POST. We send the actual JSON body from an allowed
    origin and assert the route handler ran (its 4xx/5xx/2xx for any
    validation reason is fine — the point is the request reached the
    handler, not the preflight short-circuit). If middleware order
    broke, we would see a 405 here too.

Pattern E: net-new file, no existing test edits, no schema/migration.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

# Importing app at module-load is fine — CORSMiddleware is mounted at
# app construction time so by the time TestClient(app) is built the
# middleware chain is finalised. We intentionally do NOT monkeypatch
# settings.cors_allowed_origins here because we want to test the
# production-default allowlist exactly as it ships.
from app.main import app


ALLOWED_ORIGIN = "https://www.vantagemind.ai"
DISALLOWED_ORIGIN = "https://evil.example"


def _preflight(client: TestClient, *, origin: str) -> tuple[int, dict[str, str]]:
    """Send a CORS preflight matching the production SPA's checkout submit.

    The SPA's fetch() with credentials: "include" and Content-Type:
    application/json triggers exactly this preflight shape.
    """
    response = client.options(
        "/api/v1/billing/checkout",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    # Header keys are case-insensitive but starlette lower-cases them on
    # the response side; normalise so test assertions are portable.
    headers = {k.lower(): v for k, v in response.headers.items()}
    return response.status_code, headers


def test_preflight_from_allowed_origin_returns_cors_headers() -> None:
    """Regression for the 2026-05-15 production CORS 405."""
    client = TestClient(app)
    status, headers = _preflight(client, origin=ALLOWED_ORIGIN)

    # starlette's CORSMiddleware answers preflight directly with 200.
    assert status == 200, (
        f"Preflight from {ALLOWED_ORIGIN} returned {status}; "
        f"expected 200. Headers: {headers}"
    )

    # The exact echoed origin (not a wildcard) is required because we
    # use allow_credentials=True; per the CORS spec a wildcard origin
    # is forbidden when credentials are allowed.
    assert headers.get("access-control-allow-origin") == ALLOWED_ORIGIN, (
        "Expected access-control-allow-origin to echo the request Origin "
        f"({ALLOWED_ORIGIN}); got {headers.get('access-control-allow-origin')!r}"
    )

    # The SPA sends credentials: "include" on every billing call so the
    # Step 30a session cookie reaches /me, /portal, /logout, /pilot-refund.
    # Without ACAC: true the browser will accept the response but strip
    # cookies, breaking every cookie-gated route.
    assert headers.get("access-control-allow-credentials") == "true", (
        "Expected access-control-allow-credentials: true; got "
        f"{headers.get('access-control-allow-credentials')!r}"
    )

    # The methods header must include POST (the actual method the SPA
    # is preflighting). starlette joins multiple methods with ", ".
    allow_methods = headers.get("access-control-allow-methods", "")
    assert "POST" in allow_methods, (
        f"Expected POST in access-control-allow-methods; got {allow_methods!r}"
    )


def test_preflight_from_disallowed_origin_omits_acao() -> None:
    """Rogue origin must not be granted cross-origin access."""
    client = TestClient(app)
    status, headers = _preflight(client, origin=DISALLOWED_ORIGIN)

    # Preflight from any origin still gets an HTTP response (CORS is
    # advisory at the browser, not a server-side block). starlette's
    # CORSMiddleware historically answers 400 for disallowed origins
    # but the contract we care about is "no ACAO header for this origin".
    assert status in (200, 400), (
        f"Preflight from {DISALLOWED_ORIGIN} returned {status}; "
        f"expected 200 or 400. Headers: {headers}"
    )

    # The critical assertion: the disallowed origin must NOT be echoed
    # back. If it is, the browser would happily run cross-origin
    # requests from evil.example against api.vantagemind.ai.
    acao = headers.get("access-control-allow-origin")
    assert acao != DISALLOWED_ORIGIN, (
        "Disallowed origin was echoed back in access-control-allow-origin; "
        "the CORS allowlist is misconfigured."
    )
    # And we must never accidentally widen to a wildcard while credentials
    # are allowed (would be a spec violation in any case).
    assert acao != "*", (
        "access-control-allow-origin: * is forbidden when "
        "allow_credentials=True; the allowlist is misconfigured."
    )


def test_actual_post_from_allowed_origin_still_hits_route() -> None:
    """Defense in depth: middleware order did not swallow the real POST.

    We submit a deliberately invalid body so the route's pydantic schema
    raises 422. The point is NOT that 422 is the right code for a real
    customer; the point is that 422 came from the route handler (i.e.
    the request traversed every middleware and reached FastAPI's
    validation layer). If the CORSMiddleware were mis-ordered such that
    it ate the real request, we'd see 405 or 400 from the middleware
    instead of 422 from the schema.
    """
    client = TestClient(app)
    response = client.post(
        "/api/v1/billing/checkout",
        json={},  # empty body -> pydantic schema 422
        headers={"Origin": ALLOWED_ORIGIN, "Content-Type": "application/json"},
    )
    # 422 = pydantic schema rejected the missing fields, proving the
    # request reached the route handler. Anything 4xx/5xx that ISN'T
    # 405 / 400-from-CORS proves the same thing; we pin 422 because
    # that is the deterministic schema response for an empty body.
    assert response.status_code == 422, (
        f"Expected 422 from the route's pydantic schema after CORS "
        f"middleware traversal; got {response.status_code}. "
        f"Body: {response.text}"
    )

    # And the response on the real POST should also carry the ACAO
    # header so the browser doesn't strip the body before the SPA can
    # read err.status / err.body. Without this, the SPA's
    # BillingApiError path can't surface the backend's detail string.
    response_headers = {k.lower(): v for k, v in response.headers.items()}
    assert response_headers.get("access-control-allow-origin") == ALLOWED_ORIGIN, (
        "Actual POST response is missing access-control-allow-origin; "
        "the SPA's BillingApiError would be unable to read the backend's "
        "detail string and the user would see the generic billing toast."
    )
