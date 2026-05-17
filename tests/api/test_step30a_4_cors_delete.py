"""
Step 30a.4 hot-fix — CORS preflight DELETE method contract test.

D-cors-delete-method-blocked-2026-05-17:
  After Step 30a.4 deployed, the /app/team Revoke button (the FIRST
  browser-callable DELETE in the SPA) failed silently with the generic
  "Couldn't revoke invite. Try again." toast. Root cause: app/main.py's
  CORSMiddleware was configured with
  ``allow_methods=["GET", "POST", "OPTIONS"]`` -- DELETE was missing.
  The browser's preflight (OPTIONS with
  Access-Control-Request-Method: DELETE) received the wrong
  access-control-allow-methods header back and refused to send the
  actual DELETE; fetch() then threw a network TypeError, which the
  SPA's request<T>() wrapper does NOT translate into AdminApiError,
  so the catch in onRevoke fell through to the generic toast.

  This test pins the fix (DELETE added to allow_methods) so a future
  middleware reorder or a "let's narrow the surface area" refactor
  cannot silently regress us.

Pattern E: net-new file, no edits to the existing
test_step30a_2_pilot_cors.py contract file.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


ALLOWED_ORIGIN = "https://www.vantagemind.ai"


def test_preflight_delete_invite_returns_delete_in_allow_methods() -> None:
    """Regression for the 2026-05-17 /app/team Revoke silent failure."""
    client = TestClient(app)
    response = client.options(
        # The exact path the Revoke button preflights against. Path
        # need not exist as a real route for the preflight; CORSMiddleware
        # answers preflight without forwarding downstream.
        "/api/v1/admin/invites/00000000-0000-0000-0000-000000000000",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "DELETE",
        },
    )
    assert response.status_code == 200, (
        f"DELETE preflight from {ALLOWED_ORIGIN} returned "
        f"{response.status_code}; expected 200. Headers: "
        f"{dict(response.headers)}"
    )

    headers = {k.lower(): v for k, v in response.headers.items()}

    # The cookied SPA needs the origin echoed back so the browser will
    # allow the cross-origin DELETE to proceed AND will deliver the
    # session cookie with it (allow_credentials=true requires explicit
    # origin echo).
    assert headers.get("access-control-allow-origin") == ALLOWED_ORIGIN, (
        f"Expected ACAO to echo {ALLOWED_ORIGIN}; "
        f"got {headers.get('access-control-allow-origin')!r}"
    )
    assert headers.get("access-control-allow-credentials") == "true", (
        f"Expected access-control-allow-credentials: true; got "
        f"{headers.get('access-control-allow-credentials')!r}"
    )

    # The critical assertion: DELETE must be advertised in the methods
    # list. Without this the browser short-circuits and the SPA shows
    # the generic 'Couldn't revoke' toast.
    allow_methods = headers.get("access-control-allow-methods", "")
    assert "DELETE" in allow_methods, (
        f"Expected DELETE in access-control-allow-methods; got "
        f"{allow_methods!r}. This is the exact misconfiguration that "
        f"broke /app/team Revoke on 2026-05-17."
    )


def test_preflight_patch_returns_patch_in_allow_methods() -> None:
    """Forward-cover: PATCH is added pre-emptively for cookied admin UIs.

    /admin/tenants/{id} and /admin/domains/{tenant}/{domain} are PATCH
    routes. They are not wired into the cookied SPA today, but Step 30a.5
    (Company admin) is expected to wire them in. Pin the contract now
    so we don't ship the same silent-failure shape a second time.
    """
    client = TestClient(app)
    response = client.options(
        "/api/v1/admin/tenants/some-tenant",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "PATCH",
        },
    )
    assert response.status_code == 200
    headers = {k.lower(): v for k, v in response.headers.items()}
    allow_methods = headers.get("access-control-allow-methods", "")
    assert "PATCH" in allow_methods, (
        f"Expected PATCH in access-control-allow-methods; got "
        f"{allow_methods!r}"
    )
