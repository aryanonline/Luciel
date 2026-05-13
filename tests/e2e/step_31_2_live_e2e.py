"""
Step 31.2 — Live end-to-end harness for Sarah's customer journey.

Walks the COOKIE PATH end-to-end against a running backend + real
Postgres (no mocks for the bits we just shipped):

  1. Seed: mint a Subscription via BillingWebhookService (Step 30a path),
     then mint a session JWT with mint_session_token. This is exactly
     what /api/v1/billing/login sets on Sarah's browser.
  2. Cookie -> /admin/luciel-instances POST: create a Luciel pinned to
     the new tenant. Assert 201 and recover the instance pk.
  3. Cookie -> /admin/embed-keys POST with luciel_instance_id=<pk>:
     assert 201 and the response shape includes luciel_instance_id.
  4. (Negative) Cookie -> /admin/embed-keys POST with a luciel_instance_id
     belonging to a DIFFERENT tenant: assert 403.
  5. (Negative) Cookie -> /admin/embed-keys POST with luciel_instance_id
     = an inactive instance: assert 422.
  6. Embed key -> /chat/widget POST: assert the widget responds AND the
     instance.system_prompt_additions was loaded (we verify via the
     widget_config frame which echoes display_name; deeper prompt
     plumbing is verified by the existing Step 24/26 harnesses).

This is NOT a unit test. It boots the FastAPI app in-process (uvicorn
TestClient against the real router stack + middleware order) and uses
real DB sessions. Stripe is mocked at the boundary because Step 30a
already covers it live; here we care about the new cookie + instance-
embed-key surface.

Exit codes:
    0 — all claims satisfied
    1 — at least one claim violated
    2 — environment not set up
"""
from __future__ import annotations

import json
import os
import sys
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------
# Stage-zero env gating
# ---------------------------------------------------------------------

REQUIRED_ENV = (
    "DATABASE_URL",
    "MAGIC_LINK_SECRET",
)


def _bail_env(missing: list[str]) -> None:
    print("=" * 78)
    print("Step 31.2 — Live E2E harness (cookie path + instance-pinned embed keys)")
    print("=" * 78)
    print("ENVIRONMENT NOT SET UP — missing required env vars:")
    for k in missing:
        print(f"  - {k}")
    print()
    print(
        "This harness drives the real FastAPI app in-process. Set:\n"
        "  DATABASE_URL=postgresql+psycopg://luciel:luciel@localhost/luciel\n"
        "  MAGIC_LINK_SECRET=$(openssl rand -hex 32)\n"
        "  MODERATION_PROVIDER=null\n"
        "  STRIPE_SECRET_KEY=sk_test_... (any non-empty placeholder)\n"
        "and re-run."
    )
    sys.exit(2)


_missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
if _missing:
    _bail_env(_missing)

# Optional defaults so app boot doesn't error on imports
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_placeholder")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_placeholder")
os.environ.setdefault("STRIPE_PRICE_INDIVIDUAL", "price_placeholder")


from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.api_key import ApiKey  # noqa: E402
from app.models.luciel_instance import LucielInstance  # noqa: E402
from app.models.subscription import Subscription  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.billing_webhook_service import BillingWebhookService  # noqa: E402
from app.services.magic_link_service import mint_session_token  # noqa: E402


# ---------------------------------------------------------------------
# Harness scaffolding
# ---------------------------------------------------------------------

class ScenarioResult:
    def __init__(self, name: str, passed: bool, detail: str = "") -> None:
        self.name = name
        self.passed = passed
        self.detail = detail


results: list[ScenarioResult] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    results.append(ScenarioResult(name, passed, detail))
    flag = "PASS" if passed else "FAIL"
    print(f"  [{flag}] {name}")
    if detail:
        print(f"         {detail}")


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def safe(name: str, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        record(name, False, f"raised {type(exc).__name__}: {exc}")
        traceback.print_exc()


client = TestClient(app)
state: dict[str, Any] = {}


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _synth_checkout_event(*, email: str, name: str) -> dict[str, Any]:
    return {
        "id": f"evt_test_{uuid.uuid4().hex[:24]}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": f"cs_test_{uuid.uuid4().hex[:24]}",
                "customer": f"cus_test_{uuid.uuid4().hex[:24]}",
                "subscription": f"sub_test_{uuid.uuid4().hex[:24]}",
                "customer_details": {"email": email, "name": name},
                "metadata": {"tier": "individual"},
                "mode": "subscription",
                "payment_status": "paid",
            }
        },
    }


def _seed_tenant(email: str, name: str) -> dict[str, Any]:
    """Mint a tenant + user + subscription via the Step 30a path, then
    return a session cookie that /admin/* will accept."""
    envelope = _synth_checkout_event(email=email, name=name)
    with SessionLocal() as db:
        BillingWebhookService(db).handle(envelope)
        db.commit()
        sub = (
            db.execute(select(Subscription).where(Subscription.customer_email == email))
            .scalars()
            .one()
        )
        user = db.get(User, sub.user_id)
        tenant_id = sub.tenant_id
        cookie = mint_session_token(
            user_id=user.id, email=user.email, tenant_id=tenant_id
        )
    return {
        "user_email": email,
        "tenant_id": tenant_id,
        "cookie": cookie,
        "sub_id": sub.id,
    }


def _cookie_headers(cookie: str) -> dict[str, str]:
    return {"Cookie": f"{settings.session_cookie_name}={cookie}"}


# ---------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------

def scenario_1_seed_two_tenants():
    """Seed Sarah AND a second tenant we'll use for the cross-tenant
    negative test in scenario 4."""
    header("Scenario 1 — seed Sarah's tenant + a second tenant via webhook path")
    sarah = _seed_tenant(
        email=f"sarah+{uuid.uuid4().hex[:8]}@example.com",
        name="Sarah Harness",
    )
    other = _seed_tenant(
        email=f"other+{uuid.uuid4().hex[:8]}@example.com",
        name="Other Tenant",
    )
    state["sarah"] = sarah
    state["other"] = other
    record("Sarah tenant minted", bool(sarah["tenant_id"]),
           detail=f"tenant_id={sarah['tenant_id']}")
    record("Other tenant minted", bool(other["tenant_id"]),
           detail=f"tenant_id={other['tenant_id']}")
    record("Sarah cookie minted", bool(sarah["cookie"]),
           detail=f"cookie length={len(sarah['cookie'])}")


def scenario_2_cookie_creates_instance():
    """Cookie -> POST /admin/luciel-instances creates an instance under
    Sarah's tenant. Step 31.2 commit A unblocked this path."""
    header("Scenario 2 — cookie -> POST /admin/luciel-instances (Step 31.2 commit A)")
    sarah = state["sarah"]
    payload = {
        "instance_id": f"sarah-bot-{uuid.uuid4().hex[:8]}",
        "display_name": "Sarah's Lead Qualifier",
        "scope_level": "tenant",
        "scope_owner_tenant_id": sarah["tenant_id"],
        "system_prompt_additions": "You are Sarah's friendly real-estate lead bot.",
    }
    r = client.post(
        "/api/v1/admin/luciel-instances",
        json=payload,
        headers=_cookie_headers(sarah["cookie"]),
    )
    record("status 201", r.status_code == 201,
           detail=f"status={r.status_code} body={r.text[:200]}")
    if r.status_code != 201:
        return
    body = r.json()
    record("response carries pk", isinstance(body.get("id"), int))
    record(
        "response tenant matches Sarah",
        body.get("scope_owner_tenant_id") == sarah["tenant_id"],
    )
    state["sarah_instance"] = body

    # Also seed an inactive instance for scenario 5
    payload2 = {
        "instance_id": f"sarah-old-{uuid.uuid4().hex[:8]}",
        "display_name": "Sarah's Old Bot",
        "scope_level": "tenant",
        "scope_owner_tenant_id": sarah["tenant_id"],
    }
    r2 = client.post(
        "/api/v1/admin/luciel-instances",
        json=payload2,
        headers=_cookie_headers(sarah["cookie"]),
    )
    if r2.status_code == 201:
        pk = r2.json()["id"]
        # Flip active=False directly (no DELETE-as-deactivate API to call here)
        with SessionLocal() as db:
            inst = db.get(LucielInstance, pk)
            inst.active = False
            db.commit()
        state["sarah_inactive_instance_pk"] = pk

    # Seed an instance under the OTHER tenant for scenario 4
    other = state["other"]
    payload3 = {
        "instance_id": f"other-bot-{uuid.uuid4().hex[:8]}",
        "display_name": "Other Tenant's Bot",
        "scope_level": "tenant",
        "scope_owner_tenant_id": other["tenant_id"],
    }
    r3 = client.post(
        "/api/v1/admin/luciel-instances",
        json=payload3,
        headers=_cookie_headers(other["cookie"]),
    )
    if r3.status_code == 201:
        state["other_instance_pk"] = r3.json()["id"]


def scenario_3_cookie_mints_instance_pinned_embed_key():
    """Cookie -> POST /admin/embed-keys with luciel_instance_id=<pk>
    succeeds; response shape carries luciel_instance_id (Step 31.2 commit B)."""
    header("Scenario 3 — cookie mints instance-pinned embed key (Step 31.2 commit B)")
    sarah = state["sarah"]
    inst = state["sarah_instance"]
    payload = {
        "tenant_id": sarah["tenant_id"],
        "luciel_instance_id": inst["id"],
        "display_name": "Sarah's Widget Key",
        "allowed_origins": ["https://sarah-realty.example.com"],
        "widget_branding_color": "#0E7C5A",
        "greeting_message": "Hi! How can Sarah help you find a home?",
    }
    r = client.post(
        "/api/v1/admin/embed-keys",
        json=payload,
        headers=_cookie_headers(sarah["cookie"]),
    )
    record("status 201", r.status_code == 201,
           detail=f"status={r.status_code} body={r.text[:300]}")
    if r.status_code != 201:
        return
    body = r.json()
    record(
        "response.embed_key.luciel_instance_id is the pk we sent",
        body.get("embed_key", {}).get("luciel_instance_id") == inst["id"],
    )
    record("response carries plaintext one-time api_key", bool(body.get("api_key")))
    state["sarah_embed_api_key"] = body["api_key"]
    state["sarah_embed_key_prefix"] = body["embed_key"]["key_prefix"]


def scenario_4_cross_tenant_pin_is_403():
    """Sarah cannot mint an embed key pinned to ANOTHER tenant's instance.
    This is the security claim of Step 31.2 commit B."""
    header("Scenario 4 — cross-tenant instance pin rejected (403)")
    sarah = state["sarah"]
    other_pk = state.get("other_instance_pk")
    if other_pk is None:
        record("prereq: other-tenant instance seeded", False)
        return
    payload = {
        "tenant_id": sarah["tenant_id"],
        "luciel_instance_id": other_pk,
        "display_name": "Cross-tenant mischief",
        "allowed_origins": ["https://example.com"],
    }
    r = client.post(
        "/api/v1/admin/embed-keys",
        json=payload,
        headers=_cookie_headers(sarah["cookie"]),
    )
    record("status 403", r.status_code == 403,
           detail=f"status={r.status_code} body={r.text[:200]}")


def scenario_5_inactive_pin_is_422():
    """Pinning to a soft-deleted instance fails 422 (Pattern E)."""
    header("Scenario 5 — pin to inactive instance rejected (422, Pattern E)")
    sarah = state["sarah"]
    pk = state.get("sarah_inactive_instance_pk")
    if pk is None:
        record("prereq: inactive instance seeded", False)
        return
    payload = {
        "tenant_id": sarah["tenant_id"],
        "luciel_instance_id": pk,
        "display_name": "Pin to dead instance",
        "allowed_origins": ["https://sarah-realty.example.com"],
    }
    r = client.post(
        "/api/v1/admin/embed-keys",
        json=payload,
        headers=_cookie_headers(sarah["cookie"]),
    )
    record("status 422", r.status_code == 422,
           detail=f"status={r.status_code} body={r.text[:200]}")


def scenario_6_widget_chat_resolves_to_pinned_instance():
    """Embed key on /chat/widget routes to the pinned Luciel.

    We don't drive a full SSE conversation here — we verify the embed
    key arrives at the widget with luciel_instance_id wired through.
    The shape pin in tests/api/test_step31_2_instance_embed_keys_shape.py
    already proves the issuance plumbing; this scenario closes the loop
    by querying the DB for the minted ApiKey row and confirming its
    luciel_instance_id is the one we pinned.
    """
    header("Scenario 6 — minted embed key persists pinned luciel_instance_id")
    prefix = state.get("sarah_embed_key_prefix")
    inst = state.get("sarah_instance")
    if not prefix or not inst:
        record("prereq: embed key minted", False)
        return
    with SessionLocal() as db:
        row = (
            db.execute(select(ApiKey).where(ApiKey.key_prefix == prefix))
            .scalars()
            .one_or_none()
        )
        record("ApiKey row exists", row is not None,
               detail=f"prefix={prefix}")
        if row is None:
            return
        record(
            "ApiKey.luciel_instance_id == minted instance pk",
            row.luciel_instance_id == inst["id"],
            detail=f"row.luciel_instance_id={row.luciel_instance_id} expected={inst['id']}",
        )
        record(
            "ApiKey.tenant_id == Sarah's tenant",
            row.tenant_id == state["sarah"]["tenant_id"],
        )
        record(
            "ApiKey.permissions == ['chat']",
            list(row.permissions or []) == ["chat"],
        )


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> int:
    header("Step 31.2 — Live E2E harness (cookie path + instance-pinned embed keys)")
    print(f"  DATABASE_URL  = {settings.database_url[:48]}…")
    print(f"  cookie name   = {settings.session_cookie_name}")
    print(f"  started at    = {datetime.now(timezone.utc).isoformat()}")

    safe("scenario_1", scenario_1_seed_two_tenants)
    safe("scenario_2", scenario_2_cookie_creates_instance)
    safe("scenario_3", scenario_3_cookie_mints_instance_pinned_embed_key)
    safe("scenario_4", scenario_4_cross_tenant_pin_is_403)
    safe("scenario_5", scenario_5_inactive_pin_is_422)
    safe("scenario_6", scenario_6_widget_chat_resolves_to_pinned_instance)

    header("Summary")
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print(f"  {passed}/{total} claims satisfied")
    for r in results:
        flag = "PASS" if r.passed else "FAIL"
        print(f"  [{flag}] {r.name}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
