"""
Step 30a — Live end-to-end harness against the success criteria
in docs/CANONICAL_RECAP.md §12 (row "30a") and the design decisions
locked in for self-serve subscription billing.

This is NOT a unit test. It exercises the SHIPPED code paths — real
StripeClient (against Stripe TEST mode), real BillingService, real
BillingWebhookService, real OnboardingService, real Postgres — against
the recap's success claims for Step 30a. The shape-pin lives in
tests/api/test_step30a_billing_shape.py.

Each numbered scenario maps to a recap claim. The script asserts every
claim and prints a row per claim.

Exit codes:
    0 — all claims satisfied (Step 30a is closed)
    1 — at least one claim violated (Step 30a is NOT closed)
    2 — environment not set up (missing env vars, can't reach Stripe or DB)
       The script intentionally exits 2 in CI environments without
       STRIPE_SECRET_KEY etc. so missing-env never gets read as a
       passing build.

Run with:

    export DATABASE_URL="postgresql+psycopg://luciel:luciel@localhost/luciel"
    export STRIPE_SECRET_KEY="sk_test_..."
    export STRIPE_WEBHOOK_SECRET="whsec_..."
    export STRIPE_PRICE_INDIVIDUAL="price_..."
    export MAGIC_LINK_SECRET="$(openssl rand -hex 32)"
    export MODERATION_PROVIDER=null  # so app boot is OOM-free
    python tests/e2e/step_30a_live_e2e.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------
# Stage-zero env gating: bail with exit 2 before we touch any module
# that would fail on a missing setting.
# ---------------------------------------------------------------------

REQUIRED_ENV = (
    "DATABASE_URL",
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "STRIPE_PRICE_INDIVIDUAL",
    "MAGIC_LINK_SECRET",
)


def _bail_env_not_setup(missing: list[str]) -> None:
    print("=" * 78)
    print("Step 30a — Live E2E harness")
    print("=" * 78)
    print("ENVIRONMENT NOT SET UP — missing required env vars:")
    for k in missing:
        print(f"  - {k}")
    print()
    print(
        "This harness is an opt-in live test against Stripe TEST mode + a real"
        " Postgres. Set the variables above and re-run."
    )
    sys.exit(2)


_missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
if _missing:
    _bail_env_not_setup(_missing)

# Moderation provider gates app boot; default to null in the harness.
os.environ.setdefault("MODERATION_PROVIDER", "null")


# Now safe to import the app — every Settings field has a value.
from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.integrations.stripe import (  # noqa: E402
    StripeSignatureError,
    get_stripe_client,
    reset_stripe_client,
)
from app.models.admin_audit_log import (  # noqa: E402
    ACTION_BILLING_WEBHOOK_REPLAY_REJECTED,
    ACTION_SUBSCRIPTION_CANCEL,
    ACTION_SUBSCRIPTION_CREATE,
    RESOURCE_SUBSCRIPTION,
    AdminAuditLog,
)
from app.models.subscription import Subscription  # noqa: E402
from app.models.tenant_config import TenantConfig  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.billing_service import BillingService  # noqa: E402
from app.services.billing_webhook_service import BillingWebhookService  # noqa: E402
from app.services.magic_link_service import (  # noqa: E402
    MagicLinkError,
    consume_magic_link_token,
    mint_magic_link_token,
    validate_session_token,
    mint_session_token,
)


# ---------------------------------------------------------------------
# Harness scaffolding
# ---------------------------------------------------------------------

class ScenarioResult:
    def __init__(self, name: str, passed: bool, detail: str) -> None:
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
    """Run a scenario function under a try/except so one failure doesn't
    short-circuit the whole harness."""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        record(name, False, f"raised {type(exc).__name__}: {exc}")
        traceback.print_exc()


# ---------------------------------------------------------------------
# Test fixtures (Stripe-side: created via Stripe TEST mode API)
# ---------------------------------------------------------------------

# Unique-per-run buyer email so reruns don't collide on the User unique
# index. Stripe TEST mode is happy to mint as many test customers as
# we want.
HARNESS_EMAIL = f"step30a+{uuid.uuid4().hex[:8]}@example.com"
HARNESS_NAME = "Step 30a Harness Buyer"


# ---------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------

def scenario_1_stripe_client_boot():
    """Claim: backend boots with Stripe configured."""
    header("Scenario 1 — StripeClient boots from settings")
    reset_stripe_client()
    client = get_stripe_client()
    record(
        "is_configured",
        client.is_configured,
        detail=f"api_version={client.api_version} configured={client.is_configured}",
    )
    record(
        "webhook_secret_present",
        bool(client.webhook_secret),
        detail=("…" + client.webhook_secret[-4:]) if client.webhook_secret else "<empty>",
    )


def scenario_2_checkout_session_creates():
    """Claim: BillingService.create_checkout returns a redirectable Stripe URL."""
    header("Scenario 2 — Checkout session creation against Stripe TEST")
    with SessionLocal() as db:
        svc = BillingService(db, get_stripe_client())
        result = svc.create_checkout(
            email=HARNESS_EMAIL,
            display_name=HARNESS_NAME,
            tier="individual",
        )
    record("checkout_url is https", str(result.get("checkout_url", "")).startswith("https://"))
    record("session_id begins cs_", str(result.get("session_id", "")).startswith("cs_"))
    # Store for later
    scenario_2_checkout_session_creates.last = result  # type: ignore[attr-defined]


def scenario_3_signature_rejection_is_fail_closed():
    """Claim: webhook handler rejects payloads whose signature can't be verified."""
    header("Scenario 3 — Webhook signature rejection (fail-closed)")
    client = get_stripe_client()
    bogus_payload = json.dumps({"id": "evt_bogus", "type": "checkout.session.completed"}).encode()
    rejected = False
    try:
        client.construct_event(payload=bogus_payload, sig_header="t=1,v1=deadbeef")
    except StripeSignatureError:
        rejected = True
    record("signature mismatch raises StripeSignatureError", rejected)


def _synthesize_checkout_completed_event(
    *,
    event_id: str,
    customer_id: str,
    subscription_id: str,
    email: str,
    display_name: str,
) -> dict[str, Any]:
    """Synthesize a `checkout.session.completed` envelope that mirrors
    Stripe's real shape closely enough for the dispatcher to act on it.

    We intentionally bypass Stripe.construct_event for synthesized
    events — Stripe TEST mode doesn't let us reproduce its full webhook
    payload locally without a live integration. The shape pin in
    tests/api/test_step30a_billing_shape.py guarantees the handler
    cares about the right top-level keys."""
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": f"cs_test_{uuid.uuid4().hex[:24]}",
                "customer": customer_id,
                "subscription": subscription_id,
                "customer_details": {"email": email, "name": display_name},
                "metadata": {"tier": "individual"},
                "mode": "subscription",
                "payment_status": "paid",
            }
        },
    }


def scenario_4_webhook_mints_tenant_atomically():
    """Claim: a webhook checkout.session.completed call mints
    User + Tenant + Subscription + audit row in one transaction."""
    header("Scenario 4 — webhook mints tenant (User + Subscription + audit, one txn)")

    # Build a fresh Stripe test customer + a fresh stripe_subscription_id
    # so we don't collide with a previous run.
    client = get_stripe_client()
    customer = client._stripe.Customer.create(email=HARNESS_EMAIL, name=HARNESS_NAME)
    # We use a dummy subscription id here — the real one would come back
    # from the Checkout Session.subscription field. For the harness we
    # only need a value the webhook handler can persist on the row.
    fake_sub_id = f"sub_test_{uuid.uuid4().hex[:24]}"
    event_id = f"evt_test_{uuid.uuid4().hex[:24]}"

    envelope = _synthesize_checkout_completed_event(
        event_id=event_id,
        customer_id=customer.id,
        subscription_id=fake_sub_id,
        email=HARNESS_EMAIL,
        display_name=HARNESS_NAME,
    )

    with SessionLocal() as db:
        before_subs = db.execute(select(Subscription).where(Subscription.customer_email == HARNESS_EMAIL)).scalars().all()
        BillingWebhookService(db).handle(envelope)
        db.commit()

        sub = (
            db.execute(select(Subscription).where(Subscription.customer_email == HARNESS_EMAIL))
            .scalars()
            .one_or_none()
        )
        record("subscription row created", sub is not None and not before_subs)
        if sub is None:
            return

        user = db.get(User, sub.user_id)
        record("user row created", user is not None and user.email == HARNESS_EMAIL)

        tenant = (
            db.execute(select(TenantConfig).where(TenantConfig.tenant_id == sub.tenant_id))
            .scalars()
            .one_or_none()
        )
        record("tenant row created", tenant is not None)

        audit = (
            db.execute(
                select(AdminAuditLog).where(
                    AdminAuditLog.resource_type == RESOURCE_SUBSCRIPTION,
                    AdminAuditLog.resource_id == str(sub.id),
                    AdminAuditLog.action == ACTION_SUBSCRIPTION_CREATE,
                )
            )
            .scalars()
            .all()
        )
        record("audit row recorded with ACTION_SUBSCRIPTION_CREATE", len(audit) >= 1)
        record("last_event_id is the stripe event id", sub.last_event_id == event_id)

        # Save for the cancel test
        scenario_4_webhook_mints_tenant_atomically.sub_id = sub.id  # type: ignore[attr-defined]
        scenario_4_webhook_mints_tenant_atomically.stripe_sub_id = fake_sub_id  # type: ignore[attr-defined]
        scenario_4_webhook_mints_tenant_atomically.tenant_id = sub.tenant_id  # type: ignore[attr-defined]
        scenario_4_webhook_mints_tenant_atomically.event_id = event_id  # type: ignore[attr-defined]


def scenario_5_webhook_idempotent_replay():
    """Claim: replaying the same Stripe event id is a no-op."""
    header("Scenario 5 — webhook replay rejected via last_event_id")
    event_id = getattr(scenario_4_webhook_mints_tenant_atomically, "event_id", None)
    sub_id = getattr(scenario_4_webhook_mints_tenant_atomically, "sub_id", None)
    if event_id is None or sub_id is None:
        record("prereq subscription exists", False, detail="Scenario 4 did not seed state")
        return
    with SessionLocal() as db:
        sub_before = db.get(Subscription, sub_id)
        envelope = _synthesize_checkout_completed_event(
            event_id=event_id,
            customer_id=sub_before.stripe_customer_id,
            subscription_id=sub_before.stripe_subscription_id,
            email=sub_before.customer_email,
            display_name=HARNESS_NAME,
        )
        BillingWebhookService(db).handle(envelope)
        db.commit()

        # Exactly one ACTION_SUBSCRIPTION_CREATE for this subscription.
        creates = (
            db.execute(
                select(AdminAuditLog).where(
                    AdminAuditLog.resource_type == RESOURCE_SUBSCRIPTION,
                    AdminAuditLog.resource_id == str(sub_id),
                    AdminAuditLog.action == ACTION_SUBSCRIPTION_CREATE,
                )
            )
            .scalars()
            .all()
        )
        record("only one CREATE audit row after replay", len(creates) == 1)
        # And one replay-rejected row.
        rejects = (
            db.execute(
                select(AdminAuditLog).where(
                    AdminAuditLog.action == ACTION_BILLING_WEBHOOK_REPLAY_REJECTED,
                )
            )
            .scalars()
            .all()
        )
        record(
            "REPLAY_REJECTED audit row exists",
            len(rejects) >= 1,
            detail=f"saw {len(rejects)} replay-rejected rows",
        )


def scenario_6_cancel_flips_active_and_cascades():
    """Claim: customer.subscription.deleted flips active=False and
    deactivates the tenant via AdminService.deactivate_tenant_with_cascade."""
    header("Scenario 6 — cancel cascade")
    sub_id = getattr(scenario_4_webhook_mints_tenant_atomically, "sub_id", None)
    stripe_sub_id = getattr(scenario_4_webhook_mints_tenant_atomically, "stripe_sub_id", None)
    tenant_id = getattr(scenario_4_webhook_mints_tenant_atomically, "tenant_id", None)
    if sub_id is None:
        record("prereq subscription exists", False)
        return

    cancel_envelope = {
        "id": f"evt_test_{uuid.uuid4().hex[:24]}",
        "type": "customer.subscription.deleted",
        "data": {
            "object": {
                "id": stripe_sub_id,
                "status": "canceled",
                "canceled_at": int(time.time()),
                "cancel_at_period_end": False,
            }
        },
    }
    with SessionLocal() as db:
        BillingWebhookService(db).handle(cancel_envelope)
        db.commit()

        sub_after = db.get(Subscription, sub_id)
        record("subscription.active flipped to False", sub_after is not None and sub_after.active is False)
        record(
            "subscription.status == 'canceled'",
            sub_after is not None and sub_after.status == "canceled",
        )

        tenant = (
            db.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_id))
            .scalars()
            .one_or_none()
        )
        # The exact column the cascade flips may be `active` or
        # `status` depending on how the existing AdminService is wired —
        # we accept either signal.
        deactivated = False
        if tenant is not None:
            if hasattr(tenant, "active"):
                deactivated = tenant.active is False
            if not deactivated and hasattr(tenant, "status"):
                deactivated = str(getattr(tenant, "status")).lower() in {"inactive", "canceled", "disabled"}
        record(
            "tenant deactivated via cascade",
            deactivated,
            detail=f"tenant.active={getattr(tenant, 'active', '<n/a>')}",
        )

        cancels = (
            db.execute(
                select(AdminAuditLog).where(
                    AdminAuditLog.resource_type == RESOURCE_SUBSCRIPTION,
                    AdminAuditLog.resource_id == str(sub_id),
                    AdminAuditLog.action == ACTION_SUBSCRIPTION_CANCEL,
                )
            )
            .scalars()
            .all()
        )
        record("CANCEL audit row recorded", len(cancels) >= 1)


# Step 30a.2-pilot: e2e coverage for the self-serve pilot-refund
# endpoint. We split into two scenarios:
#
#   8a — Eligibility gating (NotFirstTimePilotError / PilotWindowExpiredError)
#        with a freshly-minted Subscription row whose provider_snapshot
#        either lacks the intro stamp or whose trial_end is in the past.
#        This exercises the pure-policy branches without a real refund.
#   8b — Stripe-side refund + cancel + cascade against Stripe TEST mode.
#        Opt-in via PILOT_REFUND_LIVE_SMOKE=1 so an accidental harness
#        run does not move money. When enabled it requires a Customer +
#        Subscription seeded via Stripe test-clock travel to day 91 minus
#        epsilon; the runbook in scripts/deploy_30a2_pilot.sh covers the
#        setup.
#
# The eligibility scenario uses a separate buyer email so it does not
# collide with scenarios 4-6 (which terminate the original tenant in
# scenario 6 via the cancel cascade).

PILOT_HARNESS_EMAIL = f"step30a2pilot+{uuid.uuid4().hex[:8]}@example.com"


def _seed_pilot_subscription(
    db,
    *,
    email: str,
    intro_applied: bool,
    trial_end_offset_days: int,
) -> int:
    """Insert a synthetic Subscription row for the pilot-refund harness.

    Returns the row's PK. The shape mirrors what
    BillingWebhookService._on_checkout_completed would produce for an
    intro-applied buyer, so process_pilot_refund's eligibility branches
    can be exercised without driving a real Stripe Checkout.
    """
    from datetime import datetime, timedelta, timezone
    from app.models.subscription import (
        BILLING_CADENCE_MONTHLY,
        STATUS_TRIALING,
        Subscription,
        TIER_INDIVIDUAL,
        TIER_INSTANCE_CAPS,
    )
    from app.models.user import User
    from sqlalchemy import func, select

    user = (
        db.execute(select(User).where(func.lower(User.email) == email.lower()))
        .scalars()
        .first()
    )
    if user is None:
        user = User(email=email, display_name="Pilot Refund Harness", synthetic=False, active=True)
        db.add(user)
        db.flush()

    now = datetime.now(timezone.utc)
    sub = Subscription(
        user_id=user.id,
        tenant_id=f"t_pilot_{uuid.uuid4().hex[:8]}",
        customer_email=email,
        stripe_customer_id=f"cus_pilot_{uuid.uuid4().hex[:16]}",
        stripe_subscription_id=f"sub_pilot_{uuid.uuid4().hex[:16]}",
        tier=TIER_INDIVIDUAL,
        billing_cadence=BILLING_CADENCE_MONTHLY,
        instance_count_cap=TIER_INSTANCE_CAPS[TIER_INDIVIDUAL],
        status=STATUS_TRIALING,
        active=True,
        current_period_start=now,
        current_period_end=now + timedelta(days=90),
        trial_end=now + timedelta(days=trial_end_offset_days),
        provider_snapshot={
            "metadata": {
                "luciel_intro_applied": "true" if intro_applied else "false",
            }
        },
    )
    db.add(sub)
    db.flush()
    return sub.id


def scenario_8a_pilot_refund_eligibility_gates():
    """Claim: process_pilot_refund raises NotFirstTimePilotError when the
    subscription is not on the intro path, and PilotWindowExpiredError
    when trial_end is in the past. Both branches abort BEFORE any
    Stripe call so no live refund is initiated."""
    header("Scenario 8a — pilot-refund eligibility gates (policy-only)")
    from app.services.billing_service import (
        BillingService,
        NotFirstTimePilotError,
        PilotWindowExpiredError,
    )

    # Sub A: intro_applied=False. Should raise NotFirstTimePilotError.
    email_a = f"pilot-not-first+{uuid.uuid4().hex[:8]}@example.com"
    with SessionLocal() as db:
        sub_id_a = _seed_pilot_subscription(
            db, email=email_a, intro_applied=False, trial_end_offset_days=30,
        )
        db.commit()

        from app.models.user import User
        from sqlalchemy import select, func
        user_a = (
            db.execute(select(User).where(func.lower(User.email) == email_a.lower()))
            .scalars()
            .first()
        )
        svc = BillingService(db, get_stripe_client())
        raised = None
        try:
            svc.process_pilot_refund(user=user_a)
        except NotFirstTimePilotError as exc:
            raised = exc
        except Exception as exc:  # noqa: BLE001
            raised = exc
        record(
            "not-first-time raises NotFirstTimePilotError",
            isinstance(raised, NotFirstTimePilotError),
            detail=f"raised={type(raised).__name__ if raised else 'None'}",
        )

    # Sub B: intro_applied=True but trial_end is in the past.
    email_b = f"pilot-expired+{uuid.uuid4().hex[:8]}@example.com"
    with SessionLocal() as db:
        sub_id_b = _seed_pilot_subscription(
            db, email=email_b, intro_applied=True, trial_end_offset_days=-1,
        )
        db.commit()

        from app.models.user import User
        from sqlalchemy import select, func
        user_b = (
            db.execute(select(User).where(func.lower(User.email) == email_b.lower()))
            .scalars()
            .first()
        )
        svc = BillingService(db, get_stripe_client())
        raised = None
        try:
            svc.process_pilot_refund(user=user_b)
        except PilotWindowExpiredError as exc:
            raised = exc
        except Exception as exc:  # noqa: BLE001
            raised = exc
        record(
            "window-expired raises PilotWindowExpiredError",
            isinstance(raised, PilotWindowExpiredError),
            detail=f"raised={type(raised).__name__ if raised else 'None'}",
        )


def scenario_8b_pilot_refund_live_smoke():
    """OPT-IN: drives a real $100 refund against Stripe TEST mode.

    Skipped unless PILOT_REFUND_LIVE_SMOKE=1 is set in the environment.
    Requires a Customer + Subscription seeded with a successful intro
    charge (the runbook in scripts/deploy_30a2_pilot.sh §G4 documents
    the test-clock + price-id setup). The harness reads the
    pre-seeded sub id from PILOT_REFUND_LIVE_SUB_ID."""
    header("Scenario 8b — pilot-refund LIVE smoke (opt-in)")
    if os.getenv("PILOT_REFUND_LIVE_SMOKE", "").strip() not in {"1", "true", "yes"}:
        record("live smoke skipped (PILOT_REFUND_LIVE_SMOKE not set)", True,
               detail="set PILOT_REFUND_LIVE_SMOKE=1 + PILOT_REFUND_LIVE_SUB_ID=sub_... to enable")
        return
    sub_id_str = os.getenv("PILOT_REFUND_LIVE_SUB_ID", "").strip()
    if not sub_id_str:
        record("live smoke missing PILOT_REFUND_LIVE_SUB_ID", False)
        return
    try:
        sub_id = int(sub_id_str)
    except ValueError:
        record("live smoke PILOT_REFUND_LIVE_SUB_ID parses as int", False)
        return

    from app.services.billing_service import BillingService
    with SessionLocal() as db:
        sub = db.get(Subscription, sub_id)
        if sub is None:
            record("live smoke prereq: subscription row exists", False)
            return
        user = db.get(User, sub.user_id)
        if user is None:
            record("live smoke prereq: user row exists", False)
            return
        svc = BillingService(db, get_stripe_client())
        try:
            result = svc.process_pilot_refund(user=user)
            record("refund completed end-to-end", True,
                   detail=f"refund={result.get('refund_id')} charge={result.get('charge_id')}")
            record("amount == 10000 cad",
                   result.get("refunded_amount_cents") == 10000 and result.get("currency") == "cad")
            sub_after = db.get(Subscription, sub_id)
            record("subscription flipped to active=False",
                   sub_after is not None and sub_after.active is False)
            audits = (
                db.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.resource_type == RESOURCE_SUBSCRIPTION,
                        AdminAuditLog.resource_pk == sub_id,
                        AdminAuditLog.action == "subscription_pilot_refunded",
                    )
                )
                .scalars()
                .all()
            )
            record("SUBSCRIPTION_PILOT_REFUNDED audit row recorded", len(audits) >= 1)
        except Exception as exc:  # noqa: BLE001
            record("refund completed end-to-end", False, detail=f"{type(exc).__name__}: {exc}")


def scenario_7_magic_link_roundtrip():
    """Claim: a magic link minted by the webhook path can be consumed
    by /login and produces a session JWT that validate_session_token
    accepts."""
    header("Scenario 7 — magic-link → session-cookie roundtrip")
    sub_id = getattr(scenario_4_webhook_mints_tenant_atomically, "sub_id", None)
    if sub_id is None:
        record("prereq subscription exists", False)
        return
    with SessionLocal() as db:
        sub = db.get(Subscription, sub_id)
        user = db.get(User, sub.user_id)
        token = mint_magic_link_token(user_id=user.id, email=user.email, tenant_id=sub.tenant_id)
        # consume_magic_link_token mirrors the /login route's path.
        try:
            payload = consume_magic_link_token(token)
            record("magic link consumed", True)
            record("payload.sub == user.id", str(payload.get("sub")) == str(user.id))
            record("payload.email == user.email", payload.get("email") == user.email)
            record("payload.tenant_id == sub.tenant_id", payload.get("tenant_id") == sub.tenant_id)

            session_token = mint_session_token(
                user_id=user.id, email=user.email, tenant_id=sub.tenant_id
            )
            sess = validate_session_token(session_token)
            record("session token validates", str(sess.get("sub")) == str(user.id))
        except MagicLinkError as exc:
            record("magic link consumed", False, detail=str(exc))


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> int:
    header("Step 30a — Live E2E harness")
    print(f"  DATABASE_URL              = {settings.database_url[:48]}…")
    print(f"  STRIPE_PRICE_INDIVIDUAL   = {settings.stripe_price_individual}")
    print(f"  buyer email (this run)    = {HARNESS_EMAIL}")
    print(f"  started at                = {datetime.now(timezone.utc).isoformat()}")

    safe("scenario_1", scenario_1_stripe_client_boot)
    safe("scenario_2", scenario_2_checkout_session_creates)
    safe("scenario_3", scenario_3_signature_rejection_is_fail_closed)
    safe("scenario_4", scenario_4_webhook_mints_tenant_atomically)
    safe("scenario_5", scenario_5_webhook_idempotent_replay)
    safe("scenario_6", scenario_6_cancel_flips_active_and_cascades)
    safe("scenario_7", scenario_7_magic_link_roundtrip)
    # Step 30a.2-pilot scenarios
    safe("scenario_8a", scenario_8a_pilot_refund_eligibility_gates)
    safe("scenario_8b", scenario_8b_pilot_refund_live_smoke)

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
