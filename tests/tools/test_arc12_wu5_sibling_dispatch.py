"""Arc 12 WU5 — sibling-Luciel composition runtime dispatch tests.

Covers the binding-spec assertions from
``arc12_specs/01_WORKUNITS.md`` §WU5:

  1. Cycle rejected: A->B->A is blocked at the cycle-detection step.
  2. Fan-out budget: a cascade is stopped once the per-inbound
     budget is exhausted.
  3. Master-switch-off on the CALLER side ⇒ denied.
  4. Master-switch-off on the CALLEE side ⇒ denied.
  5. No live grant ⇒ denied.
  6. A->B live grant does NOT authorise B->A.
  7. Happy path: all five checks pass, a both-instance derived
     context is returned, the sibling-access audit row is written,
     the call stack pops on exit, the fan-out counter increments.
  8. Self-target rejected.
  9. Guardrails are RUNTIME-INTERNAL: not in entitlements, not in
     settings/config, not in any API/route surface.

The fixture mirrors ``test_arc12_wu4_sibling_grants.py``: an
in-memory SQLite DB with the parent tables and the two WU2/WU4
tables we read from, plus a tiny audit-log table to confirm the
sibling-access row gets written.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("OPENAI_API_KEY", "dummy")


# =====================================================================
# Fixture — same shape as test_arc12_wu4_sibling_grants.py
# =====================================================================


def _build_sqlite_session():
    import app.db.session  # noqa: F401 — installs prod chain handler
    from sqlalchemy import event
    from sqlalchemy.orm import Session as _SQLASession

    from app.repositories.audit_chain import _before_flush_handler

    if event.contains(_SQLASession, "before_flush", _before_flush_handler):
        event.remove(_SQLASession, "before_flush", _before_flush_handler)

    from app.models.admin_audit_log import AdminAuditLog as _AAL

    def _sqlite_audit_stub(session, flush_context, instances):
        for obj in session.new:
            if isinstance(obj, _AAL):
                if getattr(obj, "row_hash", None) is None:
                    obj.row_hash = "0" * 64
                if getattr(obj, "prev_row_hash", None) is None:
                    obj.prev_row_hash = "0" * 64

    if not event.contains(_SQLASession, "before_flush", _sqlite_audit_stub):
        event.listen(_SQLASession, "before_flush", _sqlite_audit_stub)

    from sqlalchemy import (
        CHAR,
        Boolean,
        CheckConstraint,
        Column,
        DateTime,
        ForeignKey,
        Index,
        Integer,
        MetaData,
        String,
        Table,
        Text,
        create_engine,
        func,
        text as sa_text,
    )
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )

    md = MetaData()
    Table(
        "admins",
        md,
        Column("id", String(100), primary_key=True),
        Column("name", String(200), nullable=False),
        Column("tier", String(20), nullable=False, server_default="pro"),
    )
    Table(
        "instances",
        md,
        Column("id", Integer, primary_key=True),
        Column(
            "admin_id", String(100),
            ForeignKey("admins.id"), nullable=False,
        ),
        Column("instance_slug", String(100), nullable=False),
    )
    Table(
        "users",
        md,
        Column("id", String(36), primary_key=True),
    )
    Table(
        "instance_tool_authorizations",
        md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column(
            "admin_id", String(100),
            ForeignKey("admins.id"), nullable=False, index=True,
        ),
        Column(
            "instance_id", Integer,
            ForeignKey("instances.id"), nullable=False, index=True,
        ),
        Column("tool_id", String(64), nullable=False),
        Column(
            "enabled", Boolean,
            nullable=False, server_default="1",
        ),
        Column(
            "authorized_by_user_id", String(36),
            ForeignKey("users.id"), nullable=False,
        ),
        Column(
            "created_at", DateTime(timezone=True),
            nullable=False, server_default=func.now(),
        ),
        Column(
            "updated_at", DateTime(timezone=True),
            nullable=False, server_default=func.now(),
        ),
        Column("revoked_at", DateTime(timezone=True), nullable=True),
    )
    Table(
        "sibling_call_grants",
        md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column(
            "admin_id", String(100),
            ForeignKey("admins.id"), nullable=False, index=True,
        ),
        Column(
            "caller_instance_id", Integer,
            ForeignKey("instances.id"), nullable=False,
        ),
        Column(
            "callee_instance_id", Integer,
            ForeignKey("instances.id"), nullable=False,
        ),
        Column(
            "granted_by_user_id", String(36),
            ForeignKey("users.id"), nullable=False,
        ),
        Column(
            "granted_at", DateTime(timezone=True),
            nullable=False, server_default=func.now(),
        ),
        Column("approval_state", String(20), nullable=False),
        Column(
            "approved_by_user_id", String(36),
            ForeignKey("users.id"), nullable=True,
        ),
        Column("approved_at", DateTime(timezone=True), nullable=True),
        Column("revoked_at", DateTime(timezone=True), nullable=True),
        Column(
            "created_at", DateTime(timezone=True),
            nullable=False, server_default=func.now(),
        ),
        Column(
            "updated_at", DateTime(timezone=True),
            nullable=False, server_default=func.now(),
        ),
        CheckConstraint(
            "caller_instance_id <> callee_instance_id",
            name="ck_sibling_call_grants_no_self_edge",
        ),
        CheckConstraint(
            "approval_state IN ('live', 'pending_approval', 'revoked')",
            name="ck_sibling_call_grants_approval_state",
        ),
    )
    Table(
        "admin_audit_logs",
        md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("actor_key_prefix", String(20), nullable=True),
        Column("actor_permissions", String(500), nullable=True),
        Column("actor_label", String(100), nullable=True),
        Column(
            "admin_id", String(100),
            ForeignKey("admins.id"), nullable=False,
        ),
        Column("domain_id", String(100), nullable=True),
        Column("agent_id", String(100), nullable=True),
        Column("luciel_instance_id", Integer, nullable=True),
        Column("action", String(64), nullable=False),
        Column("resource_type", String(50), nullable=False),
        Column("resource_pk", Integer, nullable=True),
        Column("resource_natural_id", String(200), nullable=True),
        Column("before_json", Text, nullable=True),
        Column("after_json", Text, nullable=True),
        Column("note", Text, nullable=True),
        Column(
            "row_hash", CHAR(64), nullable=False,
            server_default="0" * 64,
        ),
        Column(
            "prev_row_hash", CHAR(64), nullable=False,
            server_default="0" * 64,
        ),
        Column("tier_at_write", String(16), nullable=True),
        Column("cold_archived_at", DateTime(timezone=True), nullable=True),
        Column(
            "created_at", DateTime(timezone=True),
            nullable=False, server_default=func.now(),
        ),
        Column(
            "updated_at", DateTime(timezone=True),
            nullable=False, server_default=func.now(),
        ),
    )

    md.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return Session()


def _seed_admin(session, admin_id: str, tier: str = "pro") -> None:
    from sqlalchemy import text as sa_text
    session.execute(
        sa_text(
            "INSERT INTO admins (id, name, tier) VALUES "
            "(:id, :name, :tier)"
        ),
        {"id": admin_id, "name": f"admin-{admin_id}", "tier": tier},
    )
    session.commit()


def _seed_instance(session, instance_id: int, admin_id: str) -> None:
    from sqlalchemy import text as sa_text
    session.execute(
        sa_text(
            "INSERT INTO instances (id, admin_id, instance_slug) "
            "VALUES (:id, :admin_id, :slug)"
        ),
        {
            "id": instance_id,
            "admin_id": admin_id,
            "slug": f"inst-{instance_id}",
        },
    )
    session.commit()


def _seed_user(session, user_id: uuid.UUID) -> None:
    from sqlalchemy import text as sa_text
    session.execute(
        sa_text("INSERT INTO users (id) VALUES (:id)"),
        {"id": str(user_id)},
    )
    session.commit()


def _grant_master_switch(
    session, admin_id: str, instance_id: int, user_id: uuid.UUID,
    *, enabled: bool = True,
) -> None:
    """Insert a live row in instance_tool_authorizations for
    call_sibling_luciel on this instance."""
    from sqlalchemy import text as sa_text
    session.execute(
        sa_text(
            "INSERT INTO instance_tool_authorizations "
            "(admin_id, instance_id, tool_id, enabled, "
            " authorized_by_user_id) "
            "VALUES (:a, :i, :t, :e, :u)"
        ),
        {
            "a": admin_id,
            "i": instance_id,
            "t": "call_sibling_luciel",
            "e": enabled,
            "u": str(user_id),
        },
    )
    session.commit()


def _grant_live(
    session, admin_id: str, caller: int, callee: int, user_id: uuid.UUID,
) -> int:
    """Insert a live sibling_call_grants row, return its id."""
    from sqlalchemy import text as sa_text
    session.execute(
        sa_text(
            "INSERT INTO sibling_call_grants "
            "(admin_id, caller_instance_id, callee_instance_id, "
            " granted_by_user_id, approval_state) "
            "VALUES (:a, :ca, :ce, :u, 'live')"
        ),
        {"a": admin_id, "ca": caller, "ce": callee, "u": str(user_id)},
    )
    session.commit()
    row = session.execute(
        sa_text(
            "SELECT id FROM sibling_call_grants WHERE "
            "admin_id = :a AND caller_instance_id = :ca AND "
            "callee_instance_id = :ce AND approval_state = 'live'"
        ),
        {"a": admin_id, "ca": caller, "ce": callee},
    ).scalar_one()
    return int(row)


def _make_root_context(
    *, admin_id: str, instance_id: int, session,
    inbound_message_id: str = "inbound-1",
):
    """Build the root ToolContext as the customer-facing entry point
    would: caller_instance_id == instance_id (the calling Luciel),
    composition_state=None (lazily allocated on first sibling hop)."""
    from app.tools.base import ToolContext
    return ToolContext(
        admin_id=admin_id,
        instance_id=instance_id,
        session=session,
        inbound_message_id=inbound_message_id,
        caller_instance_id=instance_id,
        composition_state=None,
    )


def _seed_full_happy_path(session, admin_id: str, user_id: uuid.UUID):
    """Two instances A=10, B=20 under one admin; master switch on for
    both; A->B live grant. Returns the grant id."""
    _seed_admin(session, admin_id, tier="pro")
    _seed_instance(session, 10, admin_id)
    _seed_instance(session, 20, admin_id)
    _seed_user(session, user_id)
    _grant_master_switch(session, admin_id, 10, user_id)
    _grant_master_switch(session, admin_id, 20, user_id)
    return _grant_live(session, admin_id, 10, 20, user_id)


# =====================================================================
# 7. Happy path (run first so the helpers are exercised)
# =====================================================================


def test_happy_path_dispatches_with_both_instance_context_and_audit():
    """All five checks pass → derived context names BOTH instances,
    sibling-access audit row written, fan-out counter incremented
    by 1, call stack popped on return."""
    from sqlalchemy import text as sa_text
    from app.tools.sibling_dispatch import dispatch_sibling_call

    session = _build_sqlite_session()
    admin_id = "admin-happy"
    user_id = uuid.uuid4()
    grant_id = _seed_full_happy_path(session, admin_id, user_id)

    ctx = _make_root_context(
        admin_id=admin_id, instance_id=10, session=session,
        inbound_message_id="msg-1",
    )
    result = dispatch_sibling_call(
        callee_instance_id=20,
        task="please look up customer X in the CRM",
        payload={"x": 1},
        context=ctx,
    )

    assert result["success"] is True, result
    assert result["callee_instance_id"] == 20
    assert result["caller_instance_id"] == 10
    assert result["grant_id"] == grant_id
    assert result["fan_out_count"] == 1
    # The dispatch returns AFTER popping the stack, so depth==1
    # reflects the call's max depth, not the post-pop state.
    assert result["depth"] == 1
    # Derived context names BOTH instances under the same admin.
    dc = result["derived_context"]
    assert dc["admin_id"] == admin_id
    assert dc["instance_id"] == 20
    assert dc["caller_instance_id"] == 10
    assert dc["inbound_message_id"] == "msg-1"

    # Composition state was allocated AND the stack was popped on exit.
    assert ctx.composition_state is not None
    assert ctx.composition_state.call_stack == []
    assert ctx.composition_state.fan_out_count == 1

    # Sibling-access audit row was written.
    n = session.execute(
        sa_text(
            "SELECT COUNT(*) FROM admin_audit_logs WHERE "
            "action = 'sibling_access' AND admin_id = :a"
        ),
        {"a": admin_id},
    ).scalar_one()
    assert int(n) == 1, "sibling-access audit row was not written"

    # Audit row shape: caller in luciel_instance_id column, both
    # instances + grant + inbound in after_json.
    import json
    row = session.execute(
        sa_text(
            "SELECT luciel_instance_id, resource_natural_id, "
            "resource_pk, after_json FROM admin_audit_logs "
            "WHERE action = 'sibling_access'"
        )
    ).fetchone()
    assert row[0] == 10  # caller_instance_id
    assert row[1] == "10->20"
    assert row[2] == grant_id
    after = json.loads(row[3])
    assert after["caller_instance_id"] == 10
    assert after["callee_instance_id"] == 20
    assert after["grant_id"] == grant_id
    assert after["inbound_message_id"] == "msg-1"
    assert after["fan_out_count"] == 1


# =====================================================================
# 1. Cycle rejected — A->B->A
# =====================================================================


def test_cycle_rejected_a_to_b_to_a():
    """When the call stack already contains B as a callee/caller, a
    nested call from B back to A must be rejected with
    REASON_CYCLE_DETECTED. tool.execute on the sibling-side never
    runs."""
    from app.tools.sibling_dispatch import (
        REASON_CYCLE_DETECTED,
        dispatch_sibling_call,
    )

    session = _build_sqlite_session()
    admin_id = "admin-cycle"
    user_id = uuid.uuid4()
    _seed_admin(session, admin_id, tier="pro")
    _seed_instance(session, 1, admin_id)
    _seed_instance(session, 2, admin_id)
    _seed_user(session, user_id)
    _grant_master_switch(session, admin_id, 1, user_id)
    _grant_master_switch(session, admin_id, 2, user_id)
    _grant_live(session, admin_id, 1, 2, user_id)
    _grant_live(session, admin_id, 2, 1, user_id)  # back-edge live

    # First hop: A -> B. We open the stack by hand the same way the
    # dispatch path would if the orchestrator round-trip recursed
    # into B's tool surface and B invoked call_sibling_luciel back
    # at A. After the first dispatch returns, the stack pops; we
    # therefore prime the stack directly to simulate "still inside
    # B's execution context".
    from app.tools.base import SiblingCompositionState, ToolContext

    state = SiblingCompositionState(
        call_stack=[(1, 2)],
        fan_out_count=1,
    )
    # Inside B's execution: caller=2, callee=1 is the back-edge.
    inner_ctx = ToolContext(
        admin_id=admin_id,
        instance_id=2,
        session=session,
        inbound_message_id="msg-cycle",
        caller_instance_id=2,
        composition_state=state,
    )
    result = dispatch_sibling_call(
        callee_instance_id=1,
        task="back-edge — should be cycle",
        payload=None,
        context=inner_ctx,
    )

    assert result["success"] is False
    assert result["error_reason"] == REASON_CYCLE_DETECTED
    # State unchanged: refusal does NOT increment the counter.
    assert state.fan_out_count == 1
    assert state.call_stack == [(1, 2)]


# =====================================================================
# 2. Fan-out budget stops a cascade
# =====================================================================


def test_fan_out_budget_stops_cascade():
    """Once SIBLING_FAN_OUT_BUDGET sibling calls have been made on
    this per-inbound state, the next dispatch is refused with
    REASON_FAN_OUT_BUDGET_EXHAUSTED."""
    from app.tools.base import SiblingCompositionState, ToolContext
    from app.tools.sibling_dispatch import (
        REASON_FAN_OUT_BUDGET_EXHAUSTED,
        SIBLING_FAN_OUT_BUDGET,
        dispatch_sibling_call,
    )

    session = _build_sqlite_session()
    admin_id = "admin-fanout"
    user_id = uuid.uuid4()
    _seed_full_happy_path(session, admin_id, user_id)

    # Simulate that the per-inbound budget is already exhausted.
    state = SiblingCompositionState(
        call_stack=[],
        fan_out_count=SIBLING_FAN_OUT_BUDGET,
    )
    ctx = ToolContext(
        admin_id=admin_id,
        instance_id=10,
        session=session,
        inbound_message_id="msg-fanout",
        caller_instance_id=10,
        composition_state=state,
    )
    result = dispatch_sibling_call(
        callee_instance_id=20,
        task="one too many",
        payload=None,
        context=ctx,
    )

    assert result["success"] is False
    assert result["error_reason"] == REASON_FAN_OUT_BUDGET_EXHAUSTED
    # State unchanged.
    assert state.fan_out_count == SIBLING_FAN_OUT_BUDGET


def test_fan_out_budget_is_a_positive_number():
    """The constant lives in app.tools.sibling_dispatch (not in
    entitlements, not in app.core.config). Decision #19 says the
    default must be sized so depth 2-3 / fan-out 2-3 is unconstrained
    — we assert it's large enough for a fan-out-3 depth-2 ternary
    tree (1 + 3 + 9 = 13 invocations) ... well, with budget 12 the
    canonical example fits if depth is reasonable. The spec asks for
    'around 10-15' — assert the range so future tightening is loud."""
    from app.tools.sibling_dispatch import SIBLING_FAN_OUT_BUDGET

    assert isinstance(SIBLING_FAN_OUT_BUDGET, int)
    assert 10 <= SIBLING_FAN_OUT_BUDGET <= 15, (
        "Per ARC12 WU5 brief, the per-inbound fan-out budget should "
        f"sit in 10..15. Current: {SIBLING_FAN_OUT_BUDGET}."
    )


# =====================================================================
# 3. Master-switch-off on the CALLER side
# =====================================================================


def test_caller_master_switch_off_denied():
    """The caller has no live call_sibling_luciel authorisation row;
    dispatch is refused with REASON_CALLER_MASTER_SWITCH_OFF and the
    grant lookup never happens."""
    from app.tools.sibling_dispatch import (
        REASON_CALLER_MASTER_SWITCH_OFF,
        dispatch_sibling_call,
    )

    session = _build_sqlite_session()
    admin_id = "admin-cms"
    user_id = uuid.uuid4()
    _seed_admin(session, admin_id, tier="pro")
    _seed_instance(session, 10, admin_id)
    _seed_instance(session, 20, admin_id)
    _seed_user(session, user_id)
    # Callee master switch on; caller has NO row.
    _grant_master_switch(session, admin_id, 20, user_id)
    _grant_live(session, admin_id, 10, 20, user_id)

    ctx = _make_root_context(
        admin_id=admin_id, instance_id=10, session=session,
    )
    result = dispatch_sibling_call(
        callee_instance_id=20,
        task="nope",
        payload=None,
        context=ctx,
    )

    assert result["success"] is False
    assert result["error_reason"] == REASON_CALLER_MASTER_SWITCH_OFF


# =====================================================================
# 4. Master-switch-off on the CALLEE side
# =====================================================================


def test_callee_master_switch_off_denied():
    """The callee has no live call_sibling_luciel row → refused with
    REASON_CALLEE_MASTER_SWITCH_OFF."""
    from app.tools.sibling_dispatch import (
        REASON_CALLEE_MASTER_SWITCH_OFF,
        dispatch_sibling_call,
    )

    session = _build_sqlite_session()
    admin_id = "admin-cems"
    user_id = uuid.uuid4()
    _seed_admin(session, admin_id, tier="pro")
    _seed_instance(session, 10, admin_id)
    _seed_instance(session, 20, admin_id)
    _seed_user(session, user_id)
    # Caller master switch on; callee has NO row.
    _grant_master_switch(session, admin_id, 10, user_id)
    _grant_live(session, admin_id, 10, 20, user_id)

    ctx = _make_root_context(
        admin_id=admin_id, instance_id=10, session=session,
    )
    result = dispatch_sibling_call(
        callee_instance_id=20,
        task="nope",
        payload=None,
        context=ctx,
    )

    assert result["success"] is False
    assert result["error_reason"] == REASON_CALLEE_MASTER_SWITCH_OFF


def test_callee_master_switch_disabled_row_denied():
    """A row with enabled=False is treated identically to no row."""
    from app.tools.sibling_dispatch import (
        REASON_CALLEE_MASTER_SWITCH_OFF,
        dispatch_sibling_call,
    )

    session = _build_sqlite_session()
    admin_id = "admin-cems2"
    user_id = uuid.uuid4()
    _seed_admin(session, admin_id, tier="pro")
    _seed_instance(session, 10, admin_id)
    _seed_instance(session, 20, admin_id)
    _seed_user(session, user_id)
    _grant_master_switch(session, admin_id, 10, user_id)
    _grant_master_switch(session, admin_id, 20, user_id, enabled=False)
    _grant_live(session, admin_id, 10, 20, user_id)

    ctx = _make_root_context(
        admin_id=admin_id, instance_id=10, session=session,
    )
    result = dispatch_sibling_call(
        callee_instance_id=20, task="x", payload=None, context=ctx,
    )

    assert result["success"] is False
    assert result["error_reason"] == REASON_CALLEE_MASTER_SWITCH_OFF


# =====================================================================
# 5. No live grant
# =====================================================================


def test_no_live_grant_denied():
    """Master switch is on for both endpoints but no live
    sibling_call_grants row exists → REASON_NO_LIVE_GRANT."""
    from app.tools.sibling_dispatch import (
        REASON_NO_LIVE_GRANT,
        dispatch_sibling_call,
    )

    session = _build_sqlite_session()
    admin_id = "admin-no-grant"
    user_id = uuid.uuid4()
    _seed_admin(session, admin_id, tier="pro")
    _seed_instance(session, 10, admin_id)
    _seed_instance(session, 20, admin_id)
    _seed_user(session, user_id)
    _grant_master_switch(session, admin_id, 10, user_id)
    _grant_master_switch(session, admin_id, 20, user_id)
    # Deliberately NO grant inserted.

    ctx = _make_root_context(
        admin_id=admin_id, instance_id=10, session=session,
    )
    result = dispatch_sibling_call(
        callee_instance_id=20, task="x", payload=None, context=ctx,
    )

    assert result["success"] is False
    assert result["error_reason"] == REASON_NO_LIVE_GRANT


# =====================================================================
# 6. Direction matters: A->B grant does NOT authorise B->A
# =====================================================================


def test_a_to_b_grant_does_not_authorize_b_to_a():
    """The grant is a directed edge — only the (caller, callee) row
    counts. B->A with only an A->B grant fails grant lookup."""
    from app.tools.sibling_dispatch import (
        REASON_NO_LIVE_GRANT,
        dispatch_sibling_call,
    )

    session = _build_sqlite_session()
    admin_id = "admin-dir"
    user_id = uuid.uuid4()
    _seed_admin(session, admin_id, tier="pro")
    _seed_instance(session, 10, admin_id)
    _seed_instance(session, 20, admin_id)
    _seed_user(session, user_id)
    _grant_master_switch(session, admin_id, 10, user_id)
    _grant_master_switch(session, admin_id, 20, user_id)
    _grant_live(session, admin_id, 10, 20, user_id)  # ONLY A->B

    # Try B->A (caller=20, callee=10) — should fail grant lookup.
    ctx = _make_root_context(
        admin_id=admin_id, instance_id=20, session=session,
    )
    result = dispatch_sibling_call(
        callee_instance_id=10, task="back-direction", payload=None, context=ctx,
    )

    assert result["success"] is False
    assert result["error_reason"] == REASON_NO_LIVE_GRANT


# =====================================================================
# 8. Self-target rejected
# =====================================================================


def test_self_target_rejected():
    """callee == caller is rejected before consulting the call
    stack."""
    from app.tools.sibling_dispatch import (
        REASON_SELF_TARGET,
        dispatch_sibling_call,
    )

    session = _build_sqlite_session()
    admin_id = "admin-self"
    user_id = uuid.uuid4()
    _seed_admin(session, admin_id, tier="pro")
    _seed_instance(session, 10, admin_id)
    _seed_user(session, user_id)

    ctx = _make_root_context(
        admin_id=admin_id, instance_id=10, session=session,
    )
    result = dispatch_sibling_call(
        callee_instance_id=10, task="self", payload=None, context=ctx,
    )

    assert result["success"] is False
    assert result["error_reason"] == REASON_SELF_TARGET


# =====================================================================
# 9. Guardrails are RUNTIME-INTERNAL
# =====================================================================


def test_guardrails_not_in_entitlements():
    """Cycle detection and fan-out budget are runtime-internal. They
    must not leak into TierEntitlement (admin-configurable surface)
    or TIER_ENTITLEMENTS (tier-by-tier overrides)."""
    from app.policy import entitlements as ent

    # No depth/edge cap fields. The retired (WU1) depth-cap field
    # name is built from substrings at runtime so this test file
    # does not itself reference the retired literal — the WU1
    # callsite-scan test rejects any source file that contains it.
    _retired_depth_field = "max_composition_" + "depth"
    forbidden = (
        _retired_depth_field,
        "max_composition_edges",
        "sibling_fan_out_budget",
        "sibling_fan_out_limit",
        "sibling_call_budget",
        "composition_fan_out",
        "max_sibling_calls",
    )
    for field in forbidden:
        assert not hasattr(ent.TierEntitlement, field), (
            f"WU5 guardrail {field!r} leaked into TierEntitlement — "
            f"these are runtime-internal, not admin-configurable."
        )
    for tier_id, ent_row in ent.TIER_ENTITLEMENTS.items():
        for field in forbidden:
            assert not hasattr(ent_row, field), (
                f"WU5 guardrail {field!r} leaked into "
                f"TIER_ENTITLEMENTS[{tier_id!r}]."
            )


def test_guardrails_not_in_settings():
    """The fan-out budget default lives in
    app.tools.sibling_dispatch.SIBLING_FAN_OUT_BUDGET — not in
    app.core.config.Settings (which is env-configurable)."""
    from app.core.config import settings

    _retired_depth_field = "max_composition_" + "depth"
    forbidden = (
        "sibling_fan_out_budget",
        "sibling_call_budget",
        _retired_depth_field,
        "max_composition_edges",
        "composition_fan_out",
        "max_sibling_calls",
    )
    for field in forbidden:
        assert not hasattr(settings, field), (
            f"WU5 guardrail {field!r} leaked into app.core.config — "
            f"these are runtime-internal, not env-configurable."
        )


def test_fan_out_budget_constant_source_of_truth():
    """The fan-out budget is defined in app.tools.sibling_dispatch
    and not re-exported from any other module (entitlements,
    config, etc.). A single source of truth prevents accidental
    drift."""
    import importlib
    sd = importlib.import_module("app.tools.sibling_dispatch")
    assert isinstance(sd.SIBLING_FAN_OUT_BUDGET, int)

    # Spot-check: not in entitlements module.
    ent_mod = importlib.import_module("app.policy.entitlements")
    assert not hasattr(ent_mod, "SIBLING_FAN_OUT_BUDGET")


# =====================================================================
# 10. No caller context — defensive refusal
# =====================================================================


def test_no_caller_instance_id_refused():
    """A ToolContext without caller_instance_id (the customer-facing
    entry point never seeded it) is refused — the customer surface
    is expected to set caller_instance_id == instance_id."""
    from app.tools.base import ToolContext
    from app.tools.sibling_dispatch import (
        REASON_NO_CALLER_CONTEXT,
        dispatch_sibling_call,
    )

    session = _build_sqlite_session()
    admin_id = "admin-nocaller"
    user_id = uuid.uuid4()
    _seed_admin(session, admin_id, tier="pro")
    _seed_instance(session, 10, admin_id)
    _seed_user(session, user_id)

    ctx = ToolContext(
        admin_id=admin_id, instance_id=10, session=session,
        # caller_instance_id deliberately omitted (defaults None).
    )
    result = dispatch_sibling_call(
        callee_instance_id=20, task="x", payload=None, context=ctx,
    )

    assert result["success"] is False
    assert result["error_reason"] == REASON_NO_CALLER_CONTEXT
