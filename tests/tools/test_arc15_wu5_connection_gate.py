"""Arc 15 WU5 — connection-gate (3rd dispatch gate) tests.

Covers the binding assertions from spec §99-101:

  1. connected     → gate admits (allowed=True).
  2. unconfigured  → structured deny, failure_kind=connection_not_configured.
  3. expired       → structured deny, failure_kind=connection_not_configured.
  4. error         → structured deny, failure_kind=connection_not_configured.
  5. no row        → structured deny, failure_kind=connection_not_configured.
  6. requires_connection is None → gate is SKIPPED (allowed=True), and is
     skipped even when there is no DB session at all (the connectionless
     tool never touches the connection lookup).
  7. requires_connection set but no DB session → load-bearing refusal
     (never a silent allow), failure_kind=connection_not_configured.

The gate is exercised in isolation against an in-memory SQLite
``instance_connections`` table. We do NOT run Alembic against SQLite —
the migration is Postgres-flavoured (enum types, partial unique index,
RLS); the RLS posture is covered by the migration-shape test. The ORM
mapper keys by table name, so the SQLite table with the same shape is
read/written by ``InstanceConnectionRepository`` transparently.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("OPENAI_API_KEY", "dummy")


# =====================================================================
# Helpers
# =====================================================================


def _build_sqlite_session():
    """In-memory SQLite session with a parallel ``instance_connections``
    table shaped to match the ORM model (enum columns rendered as TEXT).
    """
    from sqlalchemy import (
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
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
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
        Column("admin_id", String(100), ForeignKey("admins.id"), nullable=False),
        Column("instance_slug", String(100), nullable=False),
    )
    Table(
        "instance_connections",
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
        Column("connection_type", String(32), nullable=False),
        Column("provider", String(64), nullable=False),
        Column("config_json", String, nullable=True),
        Column("credential_ref", String(255), nullable=True),
        Column(
            "status", String(32),
            nullable=False, server_default="unconfigured",
        ),
        Column("last_health_check_at", DateTime(timezone=True), nullable=True),
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
    return Session()


def _seed_admin_instance(session, *, admin_id: str, instance_id: int) -> None:
    from sqlalchemy import text as sa_text

    session.execute(
        sa_text("INSERT INTO admins (id, name) VALUES (:id, :name)"),
        {"id": admin_id, "name": f"admin-{admin_id}"},
    )
    session.execute(
        sa_text(
            "INSERT INTO instances (id, admin_id, instance_slug) "
            "VALUES (:id, :admin_id, :slug)"
        ),
        {"id": instance_id, "admin_id": admin_id, "slug": f"inst-{instance_id}"},
    )
    session.commit()


def _seed_connection(
    session, *, admin_id: str, instance_id: int, connection_type: str, status: str
) -> None:
    from app.repositories.instance_connection_repository import (
        InstanceConnectionRepository,
    )

    InstanceConnectionRepository(session).configure(
        admin_id=admin_id,
        instance_id=instance_id,
        connection_type=connection_type,
        provider="test_provider",
        status=status,
        config_json={"store_ref": "s3://x"} if status == "connected" else None,
        last_health_check_at=datetime.now(timezone.utc),
    )


def _make_connection_tool(*, requires_connection):
    """A §3.3.1 tool that declares ``requires_connection``."""
    from app.policy.action_classification import ActionTier
    from app.tools.base import LucielTool

    class _ConnTool(LucielTool):
        declared_tier = ActionTier.ROUTINE

        @property
        def tool_id(self) -> str:
            return "conn_tool"

        @property
        def display_name(self) -> str:
            return "Conn tool"

        @property
        def description(self) -> str:
            return "Needs a connection."

        @property
        def input_schema(self) -> dict:
            return {"type": "object", "additionalProperties": True}

        @property
        def output_schema(self) -> dict:
            return {"type": "object", "additionalProperties": True}

        @property
        def requires_tier(self) -> tuple[str, ...]:
            return ("free", "pro", "enterprise")

        @property
        def execution_mode(self) -> str:
            return "in_process"

        async def execute(self, input, context) -> dict:  # pragma: no cover
            return {"ok": True}

    tool = _ConnTool()
    tool.requires_connection = requires_connection
    return tool


def _authorizer():
    from app.tools.authorization import DefaultDenyToolAuthorizer

    return DefaultDenyToolAuthorizer()


def _ctx(*, admin_id, instance_id, session):
    from app.tools.base import ToolContext

    return ToolContext(
        admin_id=admin_id, instance_id=instance_id, session=session
    )


# =====================================================================
# Tests — the gate in isolation via _check_connection
# =====================================================================


def test_connected_admits() -> None:
    session = _build_sqlite_session()
    _seed_admin_instance(session, admin_id="a", instance_id=1)
    _seed_connection(
        session, admin_id="a", instance_id=1,
        connection_type="record_source", status="connected",
    )
    tool = _make_connection_tool(requires_connection="record_source")
    decision = _authorizer()._check_connection(
        tool, _ctx(admin_id="a", instance_id=1, session=session)
    )
    assert decision.allowed is True


def test_unconfigured_denies() -> None:
    session = _build_sqlite_session()
    _seed_admin_instance(session, admin_id="a", instance_id=1)
    _seed_connection(
        session, admin_id="a", instance_id=1,
        connection_type="calendar", status="unconfigured",
    )
    tool = _make_connection_tool(requires_connection="calendar")
    decision = _authorizer()._check_connection(
        tool, _ctx(admin_id="a", instance_id=1, session=session)
    )
    assert decision.allowed is False
    assert decision.failure_kind == "connection_not_configured"


def test_expired_denies() -> None:
    session = _build_sqlite_session()
    _seed_admin_instance(session, admin_id="a", instance_id=1)
    _seed_connection(
        session, admin_id="a", instance_id=1,
        connection_type="record_source", status="expired",
    )
    tool = _make_connection_tool(requires_connection="record_source")
    decision = _authorizer()._check_connection(
        tool, _ctx(admin_id="a", instance_id=1, session=session)
    )
    assert decision.allowed is False
    assert decision.failure_kind == "connection_not_configured"
    assert decision.reason == "connection_not_connected"


def test_error_status_denies() -> None:
    session = _build_sqlite_session()
    _seed_admin_instance(session, admin_id="a", instance_id=1)
    _seed_connection(
        session, admin_id="a", instance_id=1,
        connection_type="record_source", status="error",
    )
    tool = _make_connection_tool(requires_connection="record_source")
    decision = _authorizer()._check_connection(
        tool, _ctx(admin_id="a", instance_id=1, session=session)
    )
    assert decision.allowed is False
    assert decision.failure_kind == "connection_not_configured"


def test_no_row_denies() -> None:
    session = _build_sqlite_session()
    _seed_admin_instance(session, admin_id="a", instance_id=1)
    tool = _make_connection_tool(requires_connection="record_source")
    decision = _authorizer()._check_connection(
        tool, _ctx(admin_id="a", instance_id=1, session=session)
    )
    assert decision.allowed is False
    assert decision.failure_kind == "connection_not_configured"
    assert decision.reason == "connection_not_configured"


def test_no_connection_required_skips_gate_even_without_session() -> None:
    """A tool with ``requires_connection is None`` skips the gate. It
    must not even consult the DB — so it passes with session=None."""
    tool = _make_connection_tool(requires_connection=None)
    decision = _authorizer()._check_connection(
        tool, _ctx(admin_id="a", instance_id=1, session=None)
    )
    assert decision.allowed is True


def test_requires_connection_but_no_session_refuses() -> None:
    """Load-bearing: a connection-bearing tool with no reachable DB
    session is REFUSED — never a silent allow."""
    tool = _make_connection_tool(requires_connection="record_source")
    decision = _authorizer()._check_connection(
        tool, _ctx(admin_id="a", instance_id=1, session=None)
    )
    assert decision.allowed is False
    assert decision.failure_kind == "connection_not_configured"


def test_full_authorize_chain_gate3_after_row(monkeypatch) -> None:
    """End-to-end through ``authorize``: with the row/tier/channel gates
    satisfied, gate 3 still refuses an unconfigured connection."""
    session = _build_sqlite_session()
    _seed_admin_instance(session, admin_id="a", instance_id=1)
    _seed_connection(
        session, admin_id="a", instance_id=1,
        connection_type="record_source", status="unconfigured",
    )
    tool = _make_connection_tool(requires_connection="record_source")
    authz = _authorizer()

    # Stub out the first three gates so we isolate gate 3 in authorize().
    from app.tools.authorization import AuthorizationDecision

    monkeypatch.setattr(
        authz, "_check_row", lambda t, c: AuthorizationDecision.allow()
    )
    monkeypatch.setattr(
        authz, "_check_tier", lambda t, c: AuthorizationDecision.allow()
    )
    monkeypatch.setattr(
        authz, "_check_channels", lambda t, c: AuthorizationDecision.allow()
    )

    decision = authz.authorize(
        tool, _ctx(admin_id="a", instance_id=1, session=session)
    )
    assert decision.allowed is False
    assert decision.failure_kind == "connection_not_configured"
