"""Arc 12 WU3 — v1 tool-catalog tests.

Covers the WU3 binding-spec assertions:

  1. All 8 catalog tools are registered with the correct tool_id.
  2. Each declares requires_tier=("pro","enterprise") (per §3.3.2 —
     v1 catalog is NOT available on free).
  3. send_email requires_channels={"email"}; send_sms
     requires_channels={"sms"}; the other 6 = frozenset().
  4. execution_mode is "in_process" for all 8 EXCEPT
     bring_your_own_webhook = "subprocess" (Decision #5).
  5. Each tool's input/output JSON Schema validates a representative
     payload via app/tools/schema.py (the WU1 validator).
  6. Interim-body tools (send_email, send_sms, book_appointment,
     lookup_record, schedule_callback, push_to_crm,
     call_sibling_luciel, bring_your_own_webhook) return the
     structured "not yet available" dict and perform NO side effect.
  7. A tool dispatched without an authorisation row is default-denied
     by the WU2 broker — confirmed against the real default-deny
     authoriser path.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("OPENAI_API_KEY", "dummy")


# =====================================================================
# Catalog inventory
# =====================================================================


_CATALOG_TOOL_IDS = {
    "book_appointment",
    "send_email",
    "send_sms",
    "lookup_record",
    "schedule_callback",
    "push_to_crm",
    # call_sibling_luciel removed (Unit 1 excision — multi-Luciel/sibling deferred).
    "bring_your_own_webhook",
}


# Representative payloads — chosen to satisfy each tool's
# input_schema. Pinning them here doubles as documentation of the
# expected admin/LLM-author'd call shape.
_REPRESENTATIVE_INPUTS = {
    "book_appointment": {
        "starts_at": "2026-06-01T15:00:00Z",
        "duration_minutes": 30,
        "attendee_name": "Jane Doe",
        "attendee_contact": "jane@example.com",
    },
    "send_email": {
        "to": "jane@example.com",
        "subject": "Your appointment",
        "body": "Confirming for 3pm Monday.",
    },
    "send_sms": {
        "to": "+15555550100",
        "body": "Confirming your callback at 3pm.",
    },
    "lookup_record": {
        "record_id": "rec_123",
    },
    "schedule_callback": {
        "callback_at": "2026-06-01T15:00:00Z",
        "contact": "+15555550100",
        "topic": "Loan pre-approval",
    },
    "push_to_crm": {
        "record_type": "lead",
        "payload": {"name": "Jane Doe", "interest": "downtown loft"},
    },
    # call_sibling_luciel removed (Unit 1 excision).
    "bring_your_own_webhook": {
        # WU6 wired the real subprocess body — endpoint_id is the
        # ``byo_webhook_endpoints.id`` row PK (integer). Pre-WU6 this
        # was a free string; the interim body has been retired.
        "endpoint_id": 42,
        "payload": {"name": "Jane Doe"},
    },
}


# =====================================================================
# Helpers
# =====================================================================


def _registry():
    from app.tools.registry import ToolRegistry

    return ToolRegistry()


def _catalog_tools():
    reg = _registry()
    return {tid: reg.get(tid) for tid in _CATALOG_TOOL_IDS}


# =====================================================================
# 1. All 8 catalog tools are registered
# =====================================================================


def test_all_seven_catalog_tools_are_registered() -> None:
    # call_sibling_luciel removed (Unit 1 excision); 7 tools remain.
    reg = _registry()
    present = {t.tool_id for t in reg.list_tools()}
    missing = _CATALOG_TOOL_IDS - present
    assert not missing, (
        f"WU3: catalog tools missing from registry: {missing!r}"
    )


# =====================================================================
# 2. requires_tier = ("pro","enterprise") on every catalog tool
# =====================================================================


@pytest.mark.parametrize("tool_id", sorted(_CATALOG_TOOL_IDS))
def test_catalog_tool_requires_tier_pro_enterprise(tool_id: str) -> None:
    # Enterprise removed (Unit 1 excision); tools require pro (enterprise string
    # may still appear in the tuple but no enterprise users exist).
    tool = _registry().get(tool_id)
    assert tool is not None, f"tool {tool_id!r} not registered"
    assert "pro" in tool.requires_tier, (
        f"WU3: {tool_id} requires_tier must include 'pro'; "
        f"got {tool.requires_tier!r}"
    )


# =====================================================================
# 3. requires_channels — only send_email + send_sms declare a channel
# =====================================================================


_EXPECTED_CHANNELS = {
    "book_appointment": frozenset(),
    "send_email": frozenset({"email"}),
    "send_sms": frozenset({"sms"}),
    "lookup_record": frozenset(),
    "schedule_callback": frozenset(),
    "push_to_crm": frozenset(),
    # call_sibling_luciel removed (Unit 1 excision).
    "bring_your_own_webhook": frozenset(),
}


@pytest.mark.parametrize("tool_id", sorted(_CATALOG_TOOL_IDS))
def test_catalog_tool_requires_channels(tool_id: str) -> None:
    tool = _registry().get(tool_id)
    expected = _EXPECTED_CHANNELS[tool_id]
    assert tool.requires_channels == expected, (
        f"WU3: {tool_id} requires_channels must be {expected!r}; "
        f"got {tool.requires_channels!r}"
    )


# =====================================================================
# 4. execution_mode — subprocess only for bring_your_own_webhook
# =====================================================================


_EXPECTED_EXECUTION_MODE = {
    "book_appointment": "in_process",
    "send_email": "in_process",
    "send_sms": "in_process",
    "lookup_record": "in_process",
    "schedule_callback": "in_process",
    "push_to_crm": "in_process",
    # call_sibling_luciel removed (Unit 1 excision).
    "bring_your_own_webhook": "subprocess",
}


@pytest.mark.parametrize("tool_id", sorted(_CATALOG_TOOL_IDS))
def test_catalog_tool_execution_mode(tool_id: str) -> None:
    tool = _registry().get(tool_id)
    expected = _EXPECTED_EXECUTION_MODE[tool_id]
    assert tool.execution_mode == expected, (
        f"WU3: {tool_id} execution_mode must be {expected!r}; "
        f"got {tool.execution_mode!r}"
    )


# =====================================================================
# 5. declared_tier — every catalog tool sets an ActionTier
# =====================================================================


def test_every_catalog_tool_declares_an_action_tier() -> None:
    """Step 30c: every catalog tool must declare an action-classification
    tier so the broker's fail-closed classifier does not bump it to
    APPROVAL_REQUIRED by default. The WU1 base class defaults to None
    so an omission is loud."""
    from app.policy.action_classification import ActionTier

    for tool_id in sorted(_CATALOG_TOOL_IDS):
        tool = _registry().get(tool_id)
        assert isinstance(tool.declared_tier, ActionTier), (
            f"WU3: {tool_id} must declare an ActionTier; got "
            f"{tool.declared_tier!r}"
        )


# =====================================================================
# 6. Input/output schemas validate representative payloads
# =====================================================================


@pytest.mark.parametrize("tool_id", sorted(_CATALOG_TOOL_IDS))
def test_catalog_tool_input_schema_validates_representative_payload(
    tool_id: str,
) -> None:
    from app.tools.schema import validate_schema

    tool = _registry().get(tool_id)
    payload = _REPRESENTATIVE_INPUTS[tool_id]
    # Must not raise.
    validate_schema(payload, tool.input_schema)


@pytest.mark.parametrize("tool_id", sorted(_CATALOG_TOOL_IDS))
def test_catalog_tool_output_schema_validates_interim_dict(
    tool_id: str,
) -> None:
    """The interim "not yet available" dict each catalog tool returns
    must itself validate against the tool's output_schema. This is
    the steady-state contract — the interim body is only the
    execute() body, not the schema."""

    from app.tools.base import ToolContext
    from app.tools.schema import validate_schema

    tool = _registry().get(tool_id)
    ctx = ToolContext(admin_id="adm_1", instance_id=1)
    out = asyncio.run(
        tool.execute(_REPRESENTATIVE_INPUTS[tool_id], ctx)
    )
    assert isinstance(out, dict)
    validate_schema(out, tool.output_schema)


# =====================================================================
# 7. Interim-body tools return "not yet available" with no side effect
# =====================================================================


_INTERIM_TOOLS = {
    "book_appointment": "ARC13",
    # send_email / send_sms: Arc 17 connectors SHIPPED the full
    # DEPLOY-GATED LIVE send path. They no longer satisfy the interim-body
    # invariant: with no creds the UNCONFIGURED path still returns
    # success=False / not_yet_available=True (honest, no network), but
    # owning_arc is now ARC17 and the configured + live-switch-on path
    # performs a real send. See tests/tools/test_arc17_connectors.py.
    # lookup_record: Arc 17 SHIPPED — the live record-source body reads
    # the configured connection's store_ref (local/S3 RecordSource) and
    # returns LIVE rows. It no longer satisfies the interim-body
    # invariant (success=False / not_yet_available) so it is excluded
    # here. See tests/tools/test_lookup_record.py for the live coverage.
    "schedule_callback": "ARC13",
    # push_to_crm: Arc 17 connectors SHIPPED the native HubSpot/Salesforce
    # OAuth dispatch path (DEPLOY-GATED). Excluded for the same reason as
    # send_email/send_sms. See tests/tools/test_arc17_connectors.py.
    # call_sibling_luciel: Arc 12 WU5 SHIPPED — guardrails + audit are
    # real; only the LLM round-trip remains as an Arc-14 seam INSIDE
    # the happy path. The tool no longer satisfies the "interim body"
    # invariant (success=False / no side effect) so it is excluded
    # from this parametrisation. See
    # test_arc12_wu5_sibling_dispatch.py for the real coverage.
    #
    # bring_your_own_webhook: Arc 12 WU6 SHIPPED — the real
    # subprocess sandbox is wired (input/output schema, egress
    # allowlist, retry+backoff, per-endpoint circuit breaker, audit
    # row). The real body returns structured success/failure rather
    # than a ``not_yet_available`` envelope; see
    # test_arc12_wu6_byo_sandbox.py for the WU6 coverage.
}


@pytest.mark.parametrize("tool_id", sorted(_INTERIM_TOOLS))
def test_catalog_tool_interim_body_returns_not_yet_available(
    tool_id: str,
) -> None:
    """Per the 00_MASTER "interim-body rule": every catalog tool
    whose real implementation belongs to a later arc must return a
    structured dict naming the owning arc and must perform NO side
    effect."""

    from app.tools.base import ToolContext

    tool = _registry().get(tool_id)
    ctx = ToolContext(admin_id="adm_1", instance_id=1)
    out = asyncio.run(
        tool.execute(_REPRESENTATIVE_INPUTS[tool_id], ctx)
    )

    assert isinstance(out, dict)
    assert out.get("success") is False, (
        f"WU3 interim body for {tool_id}: success must be False so "
        f"chat_service surfaces it as a non-side-effecting refusal."
    )
    assert out.get("not_yet_available") is True, (
        f"WU3 interim body for {tool_id}: must carry "
        f"not_yet_available=True so the runtime layer can branch "
        f"on it."
    )
    assert out.get("owning_arc") == _INTERIM_TOOLS[tool_id], (
        f"WU3 interim body for {tool_id}: owning_arc must be "
        f"{_INTERIM_TOOLS[tool_id]!r}; got {out.get('owning_arc')!r}"
    )


# =====================================================================
# 8. TODO(<ARC>) breadcrumbs are greppable in every interim body
# =====================================================================


def test_interim_bodies_carry_greppable_todo_arc_comments() -> None:
    """The 00_MASTER interim-body rule requires a greppable
    ``TODO(<ARC>)`` comment naming the owning arc anchor. This test
    fails if a maintainer removes a TODO when wiring the real body
    in the wrong file (or vice-versa)."""

    import pathlib

    impl_dir = pathlib.Path("app/tools/implementations")
    file_to_todo = {
        "book_appointment_tool.py": "TODO(ARC13)",
        # send_email / send_sms / push_to_crm: Arc 17 connectors shipped
        # the real DEPLOY-GATED LIVE bodies; the files no longer carry a
        # TODO(ARC13) / TODO(ARC12_WU6) breadcrumb. See
        # tests/tools/test_arc17_connectors.py.
        # lookup_record: Arc 17 shipped the live record-source body; the
        # file no longer carries a TODO(ARC-UNASSIGNED) breadcrumb. See
        # tests/tools/test_lookup_record.py.
        "schedule_callback_tool.py": "TODO(ARC13)",
        # call_sibling_luciel: Arc 12 WU5 shipped — guardrails/audit
        # are real. The only remaining seam is the Arc-14 orchestrator
        # round-trip, which lives in app/tools/sibling_dispatch.py
        # (the dispatch module the tool delegates to). See the
        # TODO(ARC14) breadcrumb there.
        #
        # bring_your_own_webhook: Arc 12 WU6 shipped — the real
        # subprocess sandbox is wired and the file no longer carries
        # a TODO(ARC12_WU6).
    }
    for fname, todo_marker in file_to_todo.items():
        src = (impl_dir / fname).read_text()
        assert todo_marker in src, (
            f"WU3: {fname} must carry a greppable {todo_marker} "
            "comment per the 00_MASTER interim-body rule."
        )


# =====================================================================
# 9. Default-deny integration: dispatch without an authorisation row
#    refuses every catalog tool. (Integrates with WU2.)
# =====================================================================


def _build_sqlite_session_with_auth_table():
    """Construct a session + the minimum tables WU2's repository needs.

    Mirrors the harness in test_arc12_wu2_authorization.py so this
    test exercises the *real* DefaultDenyToolAuthorizer path.
    """
    from sqlalchemy import (
        Boolean,
        Column,
        DateTime,
        ForeignKey,
        Integer,
        MetaData,
        String,
        Table,
        create_engine,
        func,
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
    )
    Table(
        "instances",
        md,
        Column("id", Integer, primary_key=True),
        Column(
            "admin_id", String(100),
            ForeignKey("admins.id"),
            nullable=False,
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
    md.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()

    from sqlalchemy import text as sa_text
    session.execute(
        sa_text("INSERT INTO admins (id, name) VALUES (:id, :name)"),
        {"id": "tenant-a", "name": "admin-a"},
    )
    session.execute(
        sa_text(
            "INSERT INTO instances (id, admin_id, instance_slug) "
            "VALUES (:id, :admin_id, :slug)"
        ),
        {"id": 7, "admin_id": "tenant-a", "slug": "inst-7"},
    )
    session.execute(
        sa_text("INSERT INTO users (id) VALUES (:id)"),
        {"id": str(uuid.uuid4())},
    )
    session.commit()
    return session


@pytest.mark.parametrize("tool_id", sorted(_CATALOG_TOOL_IDS))
def test_catalog_tool_default_denied_without_authorization_row(
    tool_id: str,
) -> None:
    """WU2 ⇄ WU3 integration. The default-deny authoriser refuses
    every catalog tool until an authorisation row is written for
    (admin_id, instance_id, tool_id). The classifier is never
    consulted; the tool body never runs."""

    from app.tools.base import ToolContext
    from app.tools.broker import ToolBroker
    from app.tools.registry import ToolRegistry

    session = _build_sqlite_session_with_auth_table()
    registry = ToolRegistry()
    broker = ToolBroker(registry)  # default-deny authoriser

    ctx = ToolContext(
        admin_id="tenant-a",
        instance_id=7,
        session=session,
    )
    result = broker.execute_tool(
        tool_id,
        _REPRESENTATIVE_INPUTS[tool_id],
        context=ctx,
    )

    assert result.success is False, (
        f"WU2: catalog tool {tool_id!r} must be default-denied with "
        f"no authorisation row."
    )
    assert result.metadata.get("authorization") == "denied"
    assert result.metadata.get("authorization_reason") == (
        "no_authorization_row"
    )
    assert result.metadata.get("authorization_failure_kind") == (
        "unauthorized"
    )
