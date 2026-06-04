"""Rescan ENT — Enterprise personality second-admin approval (Vision §7).

Behavioural coverage of the tier-conditional personality-change approval
workflow. Mirrors the test convention in
``test_arc12b_admin_custom_roles_routes.py`` (and the sibling-grant
tests): route functions are invoked DIRECTLY with a fake Request and a
tenant-scoped Postgres session, so RLS + audit + alembic head are all
real.

What this proves
----------------
  * Free / Pro: PUT applies immediately, approval_state stays 'live',
    no pending change is staged.
  * Enterprise: PUT does NOT mutate the live personality_* columns; it
    stages the proposal in 'pending_approval' and records the submitter.
  * Approve by a DIFFERENT admin applies the staged proposal to live and
    flips back to 'live'.
  * Self-approval is forbidden (the submitter cannot approve their own
    proposal) → 409.
  * Reject discards the proposal; live config is untouched.
  * Audit events are emitted on submit / approve / reject.

The four-walls auth (admin scope, configure-channels permission, active
instance, tier resolution) is covered by the Arc 15 WU3 AST tests; here
the fake request carries ``platform_admin`` so we exercise the state
machine itself rather than re-testing the auth gates.
"""
from __future__ import annotations

import os
import types
import uuid
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.models.admin_audit_log import (
    ACTION_PERSONALITY_APPROVED,
    ACTION_PERSONALITY_REJECTED,
    ACTION_PERSONALITY_SUBMITTED,
    ACTION_PERSONALITY_UPDATED,
)

_DB_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    "psycopg" not in _DB_URL,
    reason="Personality-approval API tests require Postgres DATABASE_URL.",
)


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture(scope="module")
def engine():
    e = create_engine(_DB_URL)
    yield e
    e.dispose()


@pytest.fixture(autouse=True)
def _restore_prod_audit_chain_handler():
    """Re-install the production audit-chain before_flush listener in
    case an earlier SQLite-only test module swapped in a stub (same
    rationale as the Arc 12b API test fixture)."""
    from sqlalchemy import event
    from sqlalchemy.orm import Session as _SQLASession

    from app.repositories.audit_chain import (
        _before_flush_handler,
        install_audit_chain_event,
    )

    install_audit_chain_event()
    try:
        clslevel = _SQLASession.dispatch.before_flush._clslevel
        for cls, fns in list(clslevel.items()):
            for fn in list(fns):
                if fn is not _before_flush_handler:
                    try:
                        event.remove(cls, "before_flush", fn)
                    except Exception:
                        pass
    except Exception:
        pass
    yield


@pytest.fixture
def two_users(engine):
    """Two distinct Users — a submitter and a second admin (approver)."""
    ids = []
    with engine.begin() as conn:
        for _ in range(2):
            uid = conn.execute(
                text(
                    "INSERT INTO users (id, email, display_name) "
                    "VALUES (gen_random_uuid(), :em, 'pa-test') RETURNING id"
                ),
                {"em": f"pa-{uuid.uuid4().hex[:8]}@example.test"},
            ).scalar_one()
            ids.append(uid)
    yield ids
    with engine.begin() as conn:
        for uid in ids:
            try:
                conn.execute(
                    text("DELETE FROM users WHERE id = :uid"), {"uid": uid}
                )
            except Exception:
                pass


@contextmanager
def _admin_with_instance(engine, *, tier: str):
    """Seed an admin + one instance at ``tier``; yield (admin_id,
    instance_id). Cleaned up on exit."""
    aid = f"pa-{tier}-{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO admins (id, name, tier, tier_source, active) "
                "VALUES (:aid, 'pa-test', :tier, 'manual', true)"
            ),
            {"aid": aid, "tier": tier},
        )
        iid = conn.execute(
            text(
                "INSERT INTO instances (admin_id, instance_slug, display_name) "
                "VALUES (:aid, :slug, 'PA Test') RETURNING id"
            ),
            {"aid": aid, "slug": f"inst-{uuid.uuid4().hex[:8]}"},
        ).scalar_one()
    try:
        yield aid, iid
    finally:
        with engine.begin() as conn:
            conn.execute(
                text(f"SELECT set_config('app.admin_id', '{aid}', true)")
            )
            conn.execute(
                text("DELETE FROM admin_audit_logs WHERE admin_id = :aid"),
                {"aid": aid},
            )
            conn.execute(
                text("DELETE FROM instances WHERE admin_id = :aid"),
                {"aid": aid},
            )
            conn.execute(
                text("DELETE FROM admins WHERE id = :aid"), {"aid": aid}
            )


def _fake_request(*, admin_id: str, actor_user_id: uuid.UUID):
    # platform_admin bypasses the four-walls auth gates so the test
    # exercises the approval state machine, not the (separately tested)
    # auth layer.
    state = types.SimpleNamespace(
        admin_id=admin_id,
        permissions=["platform_admin"],
        scope_assignments=[],
        actor_user_id=actor_user_id,
        luciel_instance_id=None,
        role=None,
        key_prefix=None,
        actor_label=None,
    )
    return types.SimpleNamespace(state=state)


def _audit_ctx(request):
    from app.repositories.admin_audit_repository import AuditContext

    return AuditContext.from_request(request)


@contextmanager
def _tenant_session(engine, admin_id: str):
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = SessionLocal()
    try:
        s.execute(text(f"SET LOCAL app.admin_id = '{admin_id}'"))
        yield s
    finally:
        s.close()


def _instance_service(db: Session):
    from app.services.instance_service import InstanceService

    return InstanceService(db)


def _update_body(preset="warm_concierge", axes=None, business_context="hello"):
    from app.schemas.personality import PersonalityConfigUpdate

    return PersonalityConfigUpdate(
        personality_preset=preset,
        personality_axes=axes,
        business_context=business_context,
    )


def _audit_actions(engine, admin_id: str) -> list[str]:
    with engine.begin() as conn:
        conn.execute(
            text(f"SELECT set_config('app.admin_id', '{admin_id}', true)")
        )
        rows = conn.execute(
            text(
                "SELECT action FROM admin_audit_logs WHERE admin_id = :aid "
                "ORDER BY id"
            ),
            {"aid": admin_id},
        ).scalars().all()
    return list(rows)


# =====================================================================
# Router mounting
# =====================================================================


def test_router_mounted():
    from app.api.router import api_router

    paths = {r.path for r in api_router.routes}
    base = "/admin/instances/{instance_id}/personality"
    assert base in paths
    assert f"{base}/approve" in paths
    assert f"{base}/reject" in paths


# =====================================================================
# Free / Pro — apply immediately, no pending state
# =====================================================================


@pytest.mark.parametrize("tier", ["free", "pro"])
def test_free_pro_apply_immediately(engine, two_users, tier):
    from app.api.v1.admin_personality import put_personality_config

    submitter, _ = two_users
    with _admin_with_instance(engine, tier=tier) as (admin_id, iid):
        req = _fake_request(admin_id=admin_id, actor_user_id=submitter)
        with _tenant_session(engine, admin_id) as s:
            resp = put_personality_config(
                request=req,
                instance_id=iid,
                body=_update_body(business_context="immediate-apply"),
                db=s,
                instance_service=_instance_service(s),
                audit_ctx=_audit_ctx(req),
            )
        assert resp.approval_state == "live"
        assert resp.pending_personality_preset is None
        assert resp.business_context == "immediate-apply"

        # Live row reflects the change immediately.
        with _tenant_session(engine, admin_id) as s:
            row = s.execute(
                text(
                    "SELECT business_context, personality_approval_state, "
                    "pending_business_context FROM instances WHERE id = :id"
                ),
                {"id": iid},
            ).one()
        assert row.business_context == "immediate-apply"
        assert row.personality_approval_state == "live"
        assert row.pending_business_context is None

        assert ACTION_PERSONALITY_UPDATED in _audit_actions(engine, admin_id)
        assert ACTION_PERSONALITY_SUBMITTED not in _audit_actions(
            engine, admin_id
        )


# =====================================================================
# Enterprise — PUT stages pending; live UNCHANGED
# =====================================================================


def test_enterprise_put_creates_pending_live_unchanged(engine, two_users):
    from app.api.v1.admin_personality import put_personality_config

    submitter, _ = two_users
    with _admin_with_instance(engine, tier="enterprise") as (admin_id, iid):
        # Capture the live config before the change.
        with _tenant_session(engine, admin_id) as s:
            before = s.execute(
                text(
                    "SELECT personality_preset, business_context "
                    "FROM instances WHERE id = :id"
                ),
                {"id": iid},
            ).one()

        req = _fake_request(admin_id=admin_id, actor_user_id=submitter)
        with _tenant_session(engine, admin_id) as s:
            resp = put_personality_config(
                request=req,
                instance_id=iid,
                body=_update_body(
                    preset="professional_advisor",
                    business_context="proposed-change",
                ),
                db=s,
                instance_service=_instance_service(s),
                audit_ctx=_audit_ctx(req),
            )

        # Response reports pending; live fields still the OLD values.
        assert resp.approval_state == "pending_approval"
        assert resp.pending_personality_preset == "professional_advisor"
        assert resp.pending_business_context == "proposed-change"
        assert resp.personality_preset == before.personality_preset
        assert resp.business_context == before.business_context
        assert resp.personality_submitted_by_user_id == str(submitter)

        # DB: live columns UNCHANGED; pending columns staged.
        with _tenant_session(engine, admin_id) as s:
            row = s.execute(
                text(
                    "SELECT personality_preset, business_context, "
                    "personality_approval_state, pending_personality_preset, "
                    "pending_business_context, personality_submitted_by_user_id "
                    "FROM instances WHERE id = :id"
                ),
                {"id": iid},
            ).one()
        assert row.personality_preset == before.personality_preset
        assert row.business_context == before.business_context
        assert row.personality_approval_state == "pending_approval"
        assert row.pending_personality_preset == "professional_advisor"
        assert row.pending_business_context == "proposed-change"
        assert str(row.personality_submitted_by_user_id) == str(submitter)

        assert ACTION_PERSONALITY_SUBMITTED in _audit_actions(engine, admin_id)


# =====================================================================
# Approve by a DIFFERENT admin applies the change
# =====================================================================


def test_enterprise_approve_by_different_admin_applies(engine, two_users):
    from app.api.v1.admin_personality import (
        approve_personality_config,
        put_personality_config,
    )

    submitter, approver = two_users
    with _admin_with_instance(engine, tier="enterprise") as (admin_id, iid):
        # Submit.
        req_sub = _fake_request(admin_id=admin_id, actor_user_id=submitter)
        with _tenant_session(engine, admin_id) as s:
            put_personality_config(
                request=req_sub,
                instance_id=iid,
                body=_update_body(
                    preset="professional_advisor",
                    business_context="approved-content",
                ),
                db=s,
                instance_service=_instance_service(s),
                audit_ctx=_audit_ctx(req_sub),
            )

        # Approve by the OTHER user.
        req_app = _fake_request(admin_id=admin_id, actor_user_id=approver)
        with _tenant_session(engine, admin_id) as s:
            resp = approve_personality_config(
                request=req_app,
                instance_id=iid,
                db=s,
                instance_service=_instance_service(s),
                audit_ctx=_audit_ctx(req_app),
            )

        assert resp.approval_state == "live"
        assert resp.personality_preset == "professional_advisor"
        assert resp.business_context == "approved-content"
        assert resp.pending_personality_preset is None
        assert resp.personality_approved_by_user_id == str(approver)

        # DB: live columns now carry the proposal; pending cleared.
        with _tenant_session(engine, admin_id) as s:
            row = s.execute(
                text(
                    "SELECT personality_preset, business_context, "
                    "personality_approval_state, pending_personality_preset, "
                    "personality_approved_by_user_id FROM instances "
                    "WHERE id = :id"
                ),
                {"id": iid},
            ).one()
        assert row.personality_preset == "professional_advisor"
        assert row.business_context == "approved-content"
        assert row.personality_approval_state == "live"
        assert row.pending_personality_preset is None
        assert str(row.personality_approved_by_user_id) == str(approver)

        actions = _audit_actions(engine, admin_id)
        assert ACTION_PERSONALITY_SUBMITTED in actions
        assert ACTION_PERSONALITY_APPROVED in actions


# =====================================================================
# Self-approval forbidden
# =====================================================================


def test_enterprise_self_approval_forbidden(engine, two_users):
    from app.api.v1.admin_personality import (
        approve_personality_config,
        put_personality_config,
    )

    submitter, _ = two_users
    with _admin_with_instance(engine, tier="enterprise") as (admin_id, iid):
        req = _fake_request(admin_id=admin_id, actor_user_id=submitter)
        with _tenant_session(engine, admin_id) as s:
            put_personality_config(
                request=req,
                instance_id=iid,
                body=_update_body(business_context="self-approve-attempt"),
                db=s,
                instance_service=_instance_service(s),
                audit_ctx=_audit_ctx(req),
            )

        # SAME user tries to approve → 409.
        with _tenant_session(engine, admin_id) as s:
            with pytest.raises(HTTPException) as exc:
                approve_personality_config(
                    request=req,
                    instance_id=iid,
                    db=s,
                    instance_service=_instance_service(s),
                    audit_ctx=_audit_ctx(req),
                )
        assert exc.value.status_code == 409
        assert "Self-approval" in exc.value.detail

        # Still pending; live unchanged.
        with _tenant_session(engine, admin_id) as s:
            state = s.execute(
                text(
                    "SELECT personality_approval_state FROM instances "
                    "WHERE id = :id"
                ),
                {"id": iid},
            ).scalar_one()
        assert state == "pending_approval"
        assert ACTION_PERSONALITY_APPROVED not in _audit_actions(
            engine, admin_id
        )


# =====================================================================
# Reject discards the proposal; live untouched
# =====================================================================


def test_enterprise_reject_discards_pending(engine, two_users):
    from app.api.v1.admin_personality import (
        put_personality_config,
        reject_personality_config,
    )

    submitter, approver = two_users
    with _admin_with_instance(engine, tier="enterprise") as (admin_id, iid):
        with _tenant_session(engine, admin_id) as s:
            before = s.execute(
                text(
                    "SELECT personality_preset, business_context "
                    "FROM instances WHERE id = :id"
                ),
                {"id": iid},
            ).one()

        req_sub = _fake_request(admin_id=admin_id, actor_user_id=submitter)
        with _tenant_session(engine, admin_id) as s:
            put_personality_config(
                request=req_sub,
                instance_id=iid,
                body=_update_body(
                    preset="professional_advisor",
                    business_context="rejected-content",
                ),
                db=s,
                instance_service=_instance_service(s),
                audit_ctx=_audit_ctx(req_sub),
            )

        req_rej = _fake_request(admin_id=admin_id, actor_user_id=approver)
        with _tenant_session(engine, admin_id) as s:
            resp = reject_personality_config(
                request=req_rej,
                instance_id=iid,
                db=s,
                instance_service=_instance_service(s),
                audit_ctx=_audit_ctx(req_rej),
            )

        assert resp.approval_state == "live"
        assert resp.pending_personality_preset is None
        # Live config is the ORIGINAL, never the rejected proposal.
        assert resp.personality_preset == before.personality_preset
        assert resp.business_context == before.business_context

        with _tenant_session(engine, admin_id) as s:
            row = s.execute(
                text(
                    "SELECT personality_preset, business_context, "
                    "personality_approval_state, pending_personality_preset, "
                    "personality_submitted_by_user_id FROM instances "
                    "WHERE id = :id"
                ),
                {"id": iid},
            ).one()
        assert row.personality_preset == before.personality_preset
        assert row.business_context == before.business_context
        assert row.personality_approval_state == "live"
        assert row.pending_personality_preset is None
        assert row.personality_submitted_by_user_id is None

        actions = _audit_actions(engine, admin_id)
        assert ACTION_PERSONALITY_REJECTED in actions


# =====================================================================
# Approve / reject with nothing pending → 409
# =====================================================================


def test_approve_without_pending_409(engine, two_users):
    from app.api.v1.admin_personality import approve_personality_config

    _, approver = two_users
    with _admin_with_instance(engine, tier="enterprise") as (admin_id, iid):
        req = _fake_request(admin_id=admin_id, actor_user_id=approver)
        with _tenant_session(engine, admin_id) as s:
            with pytest.raises(HTTPException) as exc:
                approve_personality_config(
                    request=req,
                    instance_id=iid,
                    db=s,
                    instance_service=_instance_service(s),
                    audit_ctx=_audit_ctx(req),
                )
        assert exc.value.status_code == 409


def test_reject_without_pending_409(engine, two_users):
    from app.api.v1.admin_personality import reject_personality_config

    _, approver = two_users
    with _admin_with_instance(engine, tier="enterprise") as (admin_id, iid):
        req = _fake_request(admin_id=admin_id, actor_user_id=approver)
        with _tenant_session(engine, admin_id) as s:
            with pytest.raises(HTTPException) as exc:
                reject_personality_config(
                    request=req,
                    instance_id=iid,
                    db=s,
                    instance_service=_instance_service(s),
                    audit_ctx=_audit_ctx(req),
                )
        assert exc.value.status_code == 409
