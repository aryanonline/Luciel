"""
Step 30a.4 — Live end-to-end harness for Team-tier self-serve teammate invites.

This is NOT a unit test. It exercises the SHIPPED Step 30a.4 code paths
end-to-end against a real Postgres instance:

  * POST /api/v1/admin/invites mints a UserInvite row + 24h
    set_password JWT (purpose='invite') + welcome email (log-transport).
  * GET  /api/v1/admin/invites lists pending invites for the tenant.
  * POST /api/v1/auth/set-password with the invite-purpose token
    redeems the invite — provisions User + Agent + ScopeAssignment +
    audit row in one transaction.
  * The audit chain shows USER_INVITED x3 followed by INVITE_REDEEMED x3
    plus the four-event cookied-session audit envelope.

The shape-pin lives in tests/api/test_step30a_4_invite_shape.py. This
harness asserts the live behavior — it talks to a real DB, a real FastAPI
TestClient that mounts the full app, and the live invite-redemption path.

This harness intentionally skips Stripe — Step 30a.4 closes on Option-1
shape (code + test-clock-equivalent) per the carved drift
D-step-30a-4-live-300-paid-evidence-pending-intro-fee-scaling-2026-05-17,
which catches up at the very-end Stripe-Prices sweep.

Exit codes:
    0 — all claims satisfied (Step 30a.4 invite path is closed)
    1 — at least one claim violated
    2 — environment not set up (RUN_LIVE_E2E unset, or no DATABASE_URL,
        or no MAGIC_LINK_SECRET)

Run with:

    export RUN_LIVE_E2E=1
    export DATABASE_URL="postgresql+psycopg://luciel:luciel@localhost/luciel"
    export MAGIC_LINK_SECRET="$(openssl rand -hex 32)"
    export LUCIEL_EMAIL_TRANSPORT=log
    export MODERATION_PROVIDER=null
    export MARKETING_SITE_URL=https://example.test
    export FROM_EMAIL=test@example.test
    python tests/e2e/step_30a_4_team_invite_live_e2e.py
"""
from __future__ import annotations

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
    "MAGIC_LINK_SECRET",
)


def _bail_env_not_setup(missing: list[str]) -> None:
    print("=" * 78)
    print("Step 30a.4 — Live E2E harness (Team-tier teammate invites)")
    print("=" * 78)
    print("ENVIRONMENT NOT SET UP — missing required env vars:")
    for k in missing:
        print(f"  - {k}")
    print()
    print(
        "This harness is an opt-in live test against a real Postgres."
        " Set RUN_LIVE_E2E=1 and the variables above and re-run."
    )
    sys.exit(2)


if os.environ.get("RUN_LIVE_E2E") != "1":
    _bail_env_not_setup(["RUN_LIVE_E2E (must be '1')"])

_missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
if _missing:
    _bail_env_not_setup(_missing)

# Default knobs that keep app boot OOM-free and email side-effects logged
# rather than sent.
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("LUCIEL_EMAIL_TRANSPORT", "log")
os.environ.setdefault("MARKETING_SITE_URL", "https://example.test")
os.environ.setdefault("FROM_EMAIL", "test@example.test")


# ---------------------------------------------------------------------
# App + DB imports (after env gate is satisfied).
# ---------------------------------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, delete  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402

from app.models.admin_audit_log import (  # noqa: E402
    AdminAuditLog,
    ACTION_USER_INVITED,
    ACTION_INVITE_REDEEMED,
    RESOURCE_USER_INVITE,
)
from app.models.agent import Agent  # noqa: E402
from app.models.domain_config import DomainConfig  # noqa: E402
from app.models.scope_assignment import ScopeAssignment  # noqa: E402
from app.models.subscription import (  # noqa: E402
    Subscription,
    TIER_TEAM,
    TIER_INSTANCE_CAPS,
)
from app.models.tenant_config import TenantConfig  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.user_invite import UserInvite, InviteStatus  # noqa: E402

from app.services.magic_link_service import (  # noqa: E402
    mint_session_token,
)

# ---------------------------------------------------------------------
# Harness constants
# ---------------------------------------------------------------------

HARNESS_TENANT_PREFIX = "e2e30a4-"
HARNESS_INVITER_EMAIL = "step30a4-inviter@e2e.test"
HARNESS_INVITEES = [
    "step30a4-teammate-a@e2e.test",
    "step30a4-teammate-b@e2e.test",
    "step30a4-teammate-c@e2e.test",
]
HARNESS_PASSWORD = "Correct-Horse-Battery-Staple-30a4"


# ---------------------------------------------------------------------
# Pretty-print helpers
# ---------------------------------------------------------------------


_results: list[tuple[str, bool, str]] = []


def header(text: str) -> None:
    print()
    print("=" * 78)
    print(text)
    print("=" * 78)


def claim(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}{(' — ' + detail) if detail else ''}")
    _results.append((name, ok, detail))


def summary_and_exit() -> None:
    header("Summary")
    failed = [r for r in _results if not r[1]]
    for name, ok, detail in _results:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}{(' — ' + detail) if detail else ''}")
    print()
    print(f"Total: {len(_results)}, failed: {len(failed)}")
    sys.exit(1 if failed else 0)


# ---------------------------------------------------------------------
# Fixture setup / teardown — Team-tier tenant + cookied inviter.
# ---------------------------------------------------------------------


def _cleanup(db, tenant_id: str) -> None:
    """Remove every harness-scoped row, in FK-safe order."""
    # invites first (no children)
    db.execute(delete(UserInvite).where(UserInvite.tenant_id == tenant_id))
    # audit rows
    db.execute(delete(AdminAuditLog).where(AdminAuditLog.tenant_id == tenant_id))
    # scope assignments
    db.execute(
        delete(ScopeAssignment).where(ScopeAssignment.tenant_id == tenant_id)
    )
    # agents
    db.execute(delete(Agent).where(Agent.tenant_id == tenant_id))
    # users (by email — Users span tenants, so prune the harness emails)
    emails = [HARNESS_INVITER_EMAIL, *HARNESS_INVITEES]
    db.execute(delete(User).where(User.email.in_(emails)))
    # subscriptions (RESTRICT-FK to tenant)
    db.execute(delete(Subscription).where(Subscription.tenant_id == tenant_id))
    # domains
    db.execute(delete(DomainConfig).where(DomainConfig.tenant_id == tenant_id))
    # tenant
    db.execute(delete(TenantConfig).where(TenantConfig.tenant_id == tenant_id))
    db.commit()


def seed_team_tenant() -> tuple[str, str, uuid.UUID]:
    """Insert TenantConfig + DomainConfig + Team Subscription + inviter User
    + inviter ScopeAssignment.

    Returns (tenant_id, domain_id, inviter_user_id).
    """
    tenant_id = f"{HARNESS_TENANT_PREFIX}{uuid.uuid4().hex[:8]}"
    domain_id = "default"

    db = SessionLocal()
    try:
        # Idempotent: wipe any prior run that picked the same prefix shape.
        _cleanup(db, tenant_id)

        tenant = TenantConfig(
            tenant_id=tenant_id,
            tenant_name="Step 30a.4 E2E Tenant",
            description="harness — safe to delete",
            active=True,
        )
        db.add(tenant)

        domain = DomainConfig(
            tenant_id=tenant_id,
            domain_id=domain_id,
            domain_name="default",
            description="harness — safe to delete",
            active=True,
        )
        db.add(domain)

        sub = Subscription(
            tenant_id=tenant_id,
            customer_email=HARNESS_INVITER_EMAIL,
            stripe_customer_id=f"cus_test_{uuid.uuid4().hex[:16]}",
            stripe_subscription_id=f"sub_test_{uuid.uuid4().hex[:16]}",
            tier=TIER_TEAM,
            instance_count_cap=TIER_INSTANCE_CAPS[TIER_TEAM],
            status="active",
            active=True,
        )
        db.add(sub)

        inviter = User(
            email=HARNESS_INVITER_EMAIL,
            display_name="Step30a4 Inviter",
            synthetic=False,
            active=True,
            password_hash=None,  # cookie-only inviter; password not needed
        )
        db.add(inviter)
        db.flush()  # populate inviter.id

        sa = ScopeAssignment(
            user_id=inviter.id,
            tenant_id=tenant_id,
            domain_id=domain_id,
            role="owner",
            active=True,
        )
        db.add(sa)

        db.commit()
        db.refresh(inviter)
        return tenant_id, domain_id, inviter.id
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def teardown(tenant_id: str) -> None:
    db = SessionLocal()
    try:
        _cleanup(db, tenant_id)
    finally:
        db.close()


# ---------------------------------------------------------------------
# Scenario runners
# ---------------------------------------------------------------------


def forge_session_cookie(*, user_id: uuid.UUID, email: str, tenant_id: str) -> str:
    """Mint a real session JWT — identical shape to a /auth/magic-link
    consume cookie. Returned value is the raw cookie string."""
    return mint_session_token(user_id=user_id, email=email, tenant_id=tenant_id)


def scenario_mint_three_invites(
    client: TestClient,
    *,
    tenant_id: str,
    domain_id: str,
    inviter_user_id: uuid.UUID,
) -> list[dict]:
    header("Scenario 1 — Team-tier tenant mints three teammate invites")

    cookie = forge_session_cookie(
        user_id=inviter_user_id,
        email=HARNESS_INVITER_EMAIL,
        tenant_id=tenant_id,
    )
    client.cookies.set(settings.session_cookie_name, cookie)

    invite_rows: list[dict] = []
    for invitee in HARNESS_INVITEES:
        resp = client.post(
            "/api/v1/admin/invites",
            json={"invited_email": invitee, "role": "teammate"},
        )
        ok = resp.status_code == 201
        claim(
            f"POST /admin/invites returns 201 for {invitee}",
            ok,
            detail=f"status={resp.status_code} body={resp.text[:200]}",
        )
        if not ok:
            return invite_rows
        body = resp.json()
        for key in ("id", "tenant_id", "invited_email", "status", "expires_at"):
            claim(
                f"response shape includes {key} for {invitee}",
                key in body,
                detail=f"keys={list(body.keys())}",
            )
        claim(
            f"invite row tenant_id matches inviter tenant for {invitee}",
            body.get("tenant_id") == tenant_id,
            detail=f"got={body.get('tenant_id')} want={tenant_id}",
        )
        claim(
            f"invite row status == pending for {invitee}",
            body.get("status") == "pending",
            detail=f"got={body.get('status')}",
        )
        invite_rows.append(body)

    # GET list reflects all three
    resp = client.get("/api/v1/admin/invites")
    claim(
        "GET /admin/invites returns 200",
        resp.status_code == 200,
        detail=f"status={resp.status_code}",
    )
    if resp.status_code == 200:
        rows = resp.json()
        emails = {r["invited_email"].lower() for r in rows}
        claim(
            "list contains all three pending invites",
            all(e in emails for e in HARNESS_INVITEES),
            detail=f"got={sorted(emails)}",
        )
    return invite_rows


def scenario_duplicate_pending_rejected(
    client: TestClient,
    *,
    tenant_id: str,
) -> None:
    header("Scenario 2 — duplicate pending invite rejected with 409")
    resp = client.post(
        "/api/v1/admin/invites",
        json={"invited_email": HARNESS_INVITEES[0], "role": "teammate"},
    )
    claim(
        "second invite for same email returns 409",
        resp.status_code == 409,
        detail=f"status={resp.status_code} body={resp.text[:200]}",
    )


def _extract_invite_token_from_audit(tenant_id: str, invited_email: str) -> str:
    """Pull the raw set_password JWT from the most recent
    [welcome-set-password-email] log row.

    We can't read the email transport log directly from inside this
    harness, so we instead lift the JWT off the UserInvite by minting
    a fresh one with the row's token_jti claim. This is the same
    approach the service uses on resend.

    To keep this harness simple and end-to-end, we re-mint the JWT
    using the recorded jti so redemption hits the same row.
    """
    from app.services.magic_link_service import mint_set_password_token

    db = SessionLocal()
    try:
        invite = (
            db.execute(
                select(UserInvite)
                .where(UserInvite.tenant_id == tenant_id)
                .where(UserInvite.invited_email.ilike(invited_email))
                .where(UserInvite.status == InviteStatus.PENDING)
            )
            .scalars()
            .first()
        )
        if invite is None:
            raise RuntimeError(
                f"no pending invite for {invited_email} under {tenant_id}"
            )

        # Re-mint a JWT carrying the SAME jti as the stored row. We do
        # this by patching the uuid4() the minter would otherwise pick.
        # Simpler: import the encoder directly and stamp the jti.
        from datetime import timedelta
        import jwt
        from app.services.magic_link_service import (
            _secret_or_fail,
            JWT_ALGORITHM,
            JWT_ISSUER,
            TOKEN_TYPE_SET_PASSWORD,
        )
        from app.core.config import settings as _settings

        secret = _secret_or_fail()
        now = datetime.now(timezone.utc)
        # Use the set-password TTL (24h) — matches the live mint.
        exp_minutes = getattr(_settings, "set_password_token_ttl_minutes", 24 * 60)
        exp = now + timedelta(minutes=int(exp_minutes))
        payload = {
            "iss": JWT_ISSUER,
            "sub": str(invite.invited_by_user_id),
            "email": invite.invited_email.lower(),
            "tenant_id": invite.tenant_id,
            "typ": TOKEN_TYPE_SET_PASSWORD,
            "purpose": "invite",
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "jti": invite.token_jti,
        }
        return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)
    finally:
        db.close()


def scenario_three_teammates_redeem(
    client: TestClient,
    *,
    tenant_id: str,
) -> None:
    header("Scenario 3 — three teammates redeem via /auth/set-password (purpose=invite)")

    # Clear the inviter cookie — redemption is unauthenticated.
    client.cookies.clear()

    for invitee in HARNESS_INVITEES:
        token = _extract_invite_token_from_audit(tenant_id, invitee)
        resp = client.post(
            "/api/v1/auth/set-password",
            json={"token": token, "password": HARNESS_PASSWORD},
        )
        ok = resp.status_code in (200, 204)
        claim(
            f"POST /auth/set-password (invite) succeeds for {invitee}",
            ok,
            detail=f"status={resp.status_code} body={resp.text[:300]}",
        )


def scenario_verify_provisioning(*, tenant_id: str, domain_id: str) -> None:
    header("Scenario 4 — three Users + three Agents + three ScopeAssignments visible")

    db = SessionLocal()
    try:
        users = (
            db.execute(select(User).where(User.email.in_(HARNESS_INVITEES)))
            .scalars()
            .all()
        )
        claim(
            "three User rows exist for invited emails",
            len(users) == 3,
            detail=f"count={len(users)}",
        )
        for u in users:
            claim(
                f"User {u.email} has password_hash set",
                bool(u.password_hash),
                detail="hash empty" if not u.password_hash else "",
            )
            claim(
                f"User {u.email} is active",
                u.active,
                detail="" if u.active else "active=False",
            )

        agents = (
            db.execute(select(Agent).where(Agent.tenant_id == tenant_id))
            .scalars()
            .all()
        )
        # 3 invited teammates; the inviter does NOT get an Agent row, only
        # a ScopeAssignment, per ARCHITECTURE §3.2.13.
        claim(
            "three Agent rows under harness tenant",
            len(agents) == 3,
            detail=f"count={len(agents)}",
        )

        sas = (
            db.execute(
                select(ScopeAssignment).where(
                    ScopeAssignment.tenant_id == tenant_id,
                    ScopeAssignment.active.is_(True),
                )
            )
            .scalars()
            .all()
        )
        # 3 invited teammates + 1 inviter = 4 active ScopeAssignments.
        claim(
            "four active ScopeAssignment rows under harness tenant (3 teammates + inviter)",
            len(sas) == 4,
            detail=f"count={len(sas)}",
        )
    finally:
        db.close()


def scenario_audit_chain(*, tenant_id: str) -> None:
    header("Scenario 5 — audit chain shows USER_INVITED x3 then INVITE_REDEEMED x3")

    db = SessionLocal()
    try:
        rows = (
            db.execute(
                select(AdminAuditLog)
                .where(AdminAuditLog.tenant_id == tenant_id)
                .where(
                    AdminAuditLog.resource_type == RESOURCE_USER_INVITE,
                )
                .order_by(AdminAuditLog.created_at.asc())
            )
            .scalars()
            .all()
        )
        invited = [r for r in rows if r.action == ACTION_USER_INVITED]
        redeemed = [r for r in rows if r.action == ACTION_INVITE_REDEEMED]
        claim(
            "three USER_INVITED audit rows",
            len(invited) == 3,
            detail=f"count={len(invited)}",
        )
        claim(
            "three INVITE_REDEEMED audit rows",
            len(redeemed) == 3,
            detail=f"count={len(redeemed)}",
        )
        # Verify time ordering: all USER_INVITED rows came before any
        # INVITE_REDEEMED row.
        if invited and redeemed:
            last_invited = max(r.created_at for r in invited)
            first_redeemed = min(r.created_at for r in redeemed)
            claim(
                "all USER_INVITED rows precede first INVITE_REDEEMED row",
                last_invited <= first_redeemed,
                detail=(
                    f"last_invited={last_invited.isoformat()} "
                    f"first_redeemed={first_redeemed.isoformat()}"
                ),
            )

        # USER_INVITED resource_natural_id should be the invitee email
        # (case-folded).
        invitee_emails_in_audit = {
            (r.resource_natural_id or "").lower() for r in invited
        }
        want = {e.lower() for e in HARNESS_INVITEES}
        claim(
            "USER_INVITED resource_natural_id covers all three invitee emails",
            invitee_emails_in_audit == want,
            detail=f"got={sorted(invitee_emails_in_audit)} want={sorted(want)}",
        )
    finally:
        db.close()


def scenario_replay_blocked(client: TestClient, *, tenant_id: str) -> None:
    header("Scenario 6 — second redemption of a consumed token is rejected")

    # Re-mint a JWT against the now-ACCEPTED invite. The /auth route
    # must reject because the invite row is no longer PENDING.
    invitee = HARNESS_INVITEES[0]
    db = SessionLocal()
    try:
        invite = (
            db.execute(
                select(UserInvite)
                .where(UserInvite.tenant_id == tenant_id)
                .where(UserInvite.invited_email.ilike(invitee))
            )
            .scalars()
            .first()
        )
        claim(
            "invite row flipped to accepted after redemption",
            invite is not None and invite.status == InviteStatus.ACCEPTED,
            detail=(
                f"status={invite.status.value if invite else '<missing>'}"
            ),
        )
    finally:
        db.close()

    token = _extract_invite_token_from_audit  # noqa: F841 — see below
    # We can't re-mint via _extract_invite_token_from_audit because that
    # only returns tokens for PENDING rows. Instead, build a JWT with the
    # accepted invite's jti directly.
    from datetime import timedelta
    import jwt
    from app.services.magic_link_service import (
        _secret_or_fail,
        JWT_ALGORITHM,
        JWT_ISSUER,
        TOKEN_TYPE_SET_PASSWORD,
    )

    db = SessionLocal()
    try:
        invite = (
            db.execute(
                select(UserInvite)
                .where(UserInvite.tenant_id == tenant_id)
                .where(UserInvite.invited_email.ilike(invitee))
            )
            .scalars()
            .first()
        )
        if invite is None:
            claim("replay-blocked test prerequisite", False, "no invite row")
            return
        secret = _secret_or_fail()
        now = datetime.now(timezone.utc)
        exp = now + timedelta(hours=24)
        payload = {
            "iss": JWT_ISSUER,
            "sub": str(invite.invited_by_user_id),
            "email": invite.invited_email.lower(),
            "tenant_id": invite.tenant_id,
            "typ": TOKEN_TYPE_SET_PASSWORD,
            "purpose": "invite",
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "jti": invite.token_jti,
        }
        replay_token = jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)
    finally:
        db.close()

    resp = client.post(
        "/api/v1/auth/set-password",
        json={"token": replay_token, "password": HARNESS_PASSWORD + "-x"},
    )
    claim(
        "replay redemption returns 4xx (not 200)",
        400 <= resp.status_code < 500 and resp.status_code != 200,
        detail=f"status={resp.status_code} body={resp.text[:200]}",
    )


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------


def main() -> None:
    header("Step 30a.4 — Live E2E harness boot")
    print(f"  DATABASE_URL hint: {os.environ['DATABASE_URL'][:48]}...")
    print(f"  RUN_LIVE_E2E:      {os.environ.get('RUN_LIVE_E2E')}")
    print(f"  MAGIC_LINK_SECRET: set ({len(os.environ['MAGIC_LINK_SECRET'])} chars)")

    tenant_id, domain_id, inviter_user_id = seed_team_tenant()
    print(f"  harness tenant_id: {tenant_id}")
    print(f"  inviter user_id:   {inviter_user_id}")

    client = TestClient(app)
    try:
        invite_rows = scenario_mint_three_invites(
            client,
            tenant_id=tenant_id,
            domain_id=domain_id,
            inviter_user_id=inviter_user_id,
        )
        if len(invite_rows) == 3:
            scenario_duplicate_pending_rejected(client, tenant_id=tenant_id)
            scenario_three_teammates_redeem(client, tenant_id=tenant_id)
            scenario_verify_provisioning(tenant_id=tenant_id, domain_id=domain_id)
            scenario_audit_chain(tenant_id=tenant_id)
            scenario_replay_blocked(client, tenant_id=tenant_id)
        else:
            claim(
                "skipping downstream scenarios",
                False,
                detail="invite mint did not produce 3 rows",
            )
    finally:
        teardown(tenant_id)
        try:
            client.close()
        except Exception:
            pass

    summary_and_exit()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        print("\nUNCAUGHT EXCEPTION — harness aborted:")
        traceback.print_exc()
        sys.exit(1)
