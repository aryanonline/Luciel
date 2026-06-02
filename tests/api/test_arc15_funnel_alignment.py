"""Arc 15 — Customer-Journey funnel alignment (route-level contract).

Pins the two route-level behaviour changes introduced by
``funnel(billing): align to Customer Journey`` so a future refactor
cannot silently regress the funnel back to anonymous paid-at-signup
Pro or self-serve Enterprise.

CONTRACTS GUARDED (VANTAGEMIND_CUSTOMER_JOURNEY_v1 Phase 2):

  Pro = AUTHENTICATED free→upgrade
    POST /api/v1/billing/checkout with NO session cookie must return
    401 (the route's ``_resolve_cookied_user`` guard). There is no
    anonymous paid-at-signup Pro path: the marketing Pricing CTA must
    route the user through Free signup → dashboard "Upgrade to Pro".

  Enterprise = PROCUREMENT-led (no self-serve)
    POST /api/v1/billing/checkout AND POST /api/v1/billing/upgrade with
    tier/target_tier == "enterprise" must return 422 with
    ``detail.reason == "contact_sales"`` as the primary (and only)
    outcome — before any cookie/DB work and before any Stripe session
    is created.

These run without a live Postgres: the Enterprise guard short-circuits
before the route touches the DB, and the anonymous-Pro guard rejects on
the missing cookie before any DB call. No Stripe round-trip occurs.
"""
from __future__ import annotations

import os

# Mirror the import-time env mitigation used by the sibling billing
# tests; must precede any ``from app...`` import.
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://stub:stub@localhost:5432/stub"
)


def _client():
    from fastapi.testclient import TestClient

    from app.main import app
    return TestClient(app)


# ---------------------------------------------------------------------
# Enterprise is procurement-only on BOTH self-serve checkout surfaces.
# ---------------------------------------------------------------------

class TestEnterpriseIsContactSalesOnly:

    def test_checkout_enterprise_returns_422_contact_sales(self):
        resp = _client().post(
            "/api/v1/billing/checkout",
            json={"tier": "enterprise", "billing_cadence": "monthly"},
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert detail["reason"] == "contact_sales", detail

    def test_checkout_enterprise_annual_also_contact_sales(self):
        # Cadence is irrelevant: Enterprise is never self-served.
        resp = _client().post(
            "/api/v1/billing/checkout",
            json={"tier": "enterprise", "billing_cadence": "annual"},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["reason"] == "contact_sales"

    def test_upgrade_enterprise_returns_422_contact_sales(self):
        # The authenticated in-dashboard upgrade surface is gated too,
        # ahead of the cookie/DB resolution, so this needs no auth fixture.
        resp = _client().post(
            "/api/v1/billing/upgrade",
            json={"target_tier": "enterprise", "billing_cadence": "annual"},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["reason"] == "contact_sales"

    def test_upgrade_enterprise_monthly_also_contact_sales(self):
        resp = _client().post(
            "/api/v1/billing/upgrade",
            json={"target_tier": "enterprise", "billing_cadence": "monthly"},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["reason"] == "contact_sales"


# ---------------------------------------------------------------------
# Pro checkout requires an authenticated (cookied) Free admin.
# ---------------------------------------------------------------------

class TestProCheckoutRequiresAuth:

    def test_anonymous_pro_checkout_returns_401(self):
        # No session cookie -> _resolve_cookied_user raises 401. This is
        # the death of the anonymous paid-at-signup Pro path.
        resp = _client().post(
            "/api/v1/billing/checkout",
            json={"tier": "pro", "billing_cadence": "monthly"},
        )
        assert resp.status_code == 401, resp.text

    def test_anonymous_pro_checkout_with_legacy_body_still_401(self):
        # A stale client that still posts email/display_name must NOT be
        # able to bypass auth: identity comes from the cookie, not the
        # body, so the route ignores these and still 401s anonymously.
        resp = _client().post(
            "/api/v1/billing/checkout",
            json={
                "tier": "pro",
                "billing_cadence": "monthly",
                "email": "anon@example.com",
                "display_name": "Anon",
            },
        )
        assert resp.status_code == 401, resp.text

    def test_anonymous_pro_checkout_defaults_tier_pro_and_401s(self):
        # tier defaults to "pro" when omitted; an empty body therefore
        # still routes to the Pro auth guard (NOT a 422), proving the
        # body no longer requires email/display_name.
        resp = _client().post("/api/v1/billing/checkout", json={})
        assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------
# Authenticated Pro checkout reaches the unchanged pilot service path.
# ---------------------------------------------------------------------

class TestAuthenticatedProCheckoutReachesService:

    def test_cookied_pro_checkout_delegates_to_service(self, monkeypatch):
        """With a (stubbed) cookied user, a Pro checkout flows into
        ``BillingService.create_checkout`` unchanged — preserving the
        $100/90-day first-time pilot. We stub the cookie resolver and
        the service so no DB or Stripe round-trip is needed and assert
        the route passes the cookied identity through."""
        from app.api.v1 import billing as billing_module

        class _FakeUser:
            id = "free-abc123"
            email = "marcus@example.com"
            display_name = "Marcus"
            active = True

        monkeypatch.setattr(
            billing_module, "_resolve_cookied_user",
            lambda *, db, session_cookie: _FakeUser(),
        )

        captured: dict = {}

        class _FakeService:
            def __init__(self, *a, **k):
                pass

            def create_checkout(self, **kwargs):
                captured.update(kwargs)
                return {
                    "checkout_url": "https://checkout.stripe.com/test",
                    "session_id": "cs_test_funnel",
                }

        monkeypatch.setattr(billing_module, "_service", lambda db: _FakeService())

        resp = _client().post(
            "/api/v1/billing/checkout",
            json={"tier": "pro", "billing_cadence": "monthly"},
            cookies={"luciel_session": "stub-cookie"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["session_id"] == "cs_test_funnel"
        # Identity came from the cookied user, not the (absent) body.
        assert captured["email"] == "marcus@example.com"
        assert captured["display_name"] == "Marcus"
        assert captured["tier"] == "pro"
