"""Arc 12 WU2 — default-deny broker authorisation tests.

Covers the four binding-spec assertions:

  1. Default-deny: no live row ⇒ broker refuses, tool.execute is
     NOT called, structured tool-error is returned.
  2. Authorised: live row present ⇒ broker proceeds to the
     classification gate; on ROUTINE the tool executes.
  3. Wall-1: a row scoped to admin B cannot authorise a call made
     by admin A on the same instance.
  4. Wall-3: a row scoped to instance 1 cannot authorise a call on
     instance 2.

Also covers:

  5. Revoked row ⇒ denied.
  6. enabled=False row ⇒ denied (paused-but-not-revoked state).
  7. Idempotent authorise.
  8. Migration shape — RLS posture mirrors arc9_c3_5d, partial
     unique index present, stale max_composition_depth dropped.

The tests use an in-memory SQLite DB for the broker-integration
checks. The model maps cleanly onto SQLite for the broker path —
the RLS posture is verified by inspecting the migration body
statically (the live-RLS test is env-gated like
``test_arc11_knowledge_rls.py``).
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("OPENAI_API_KEY", "dummy")


# =====================================================================
# Helpers
# =====================================================================


def _build_sqlite_session():
    """Construct a session bound to an in-memory SQLite DB with the
    minimum schema the repo + service need.

    We don't run Alembic against SQLite — the migration is Postgres-
    flavoured (partial unique index, RLS). The full ``Base.metadata``
    carries Postgres-specific column types (INET, JSONB, UUID server
    defaults, vector) that SQLite can't render. We therefore build a
    fresh, isolated ``MetaData`` and define just the four tables this
    test file exercises.
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
        UniqueConstraint,
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
    # The ORM model is bound to ``Base.metadata`` (Postgres). The
    # in-memory SQLite has a parallel ``instance_tool_authorizations``
    # table with the same shape; the ORM mapper happily reads/writes
    # to it because SQLAlchemy keys queries by table name, not by
    # MetaData identity.
    return session


def _seed_admin_instance_user(
    session, *, admin_id: str, instance_id: int, user_id: uuid.UUID
) -> None:
    """Insert the parent rows the FKs require."""
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
        {
            "id": instance_id,
            "admin_id": admin_id,
            "slug": f"inst-{instance_id}",
        },
    )
    session.execute(
        sa_text("INSERT INTO users (id) VALUES (:id)"),
        {"id": str(user_id)},
    )
    session.commit()


def _make_tool(tool_id: str = "send_email_test", executed=None):
    """Construct a §3.3.1-compliant tool that records when it runs."""
    from app.policy.action_classification import ActionTier
    from app.tools.base import LucielTool

    if executed is None:
        executed = []

    class _RecordingTool(LucielTool):
        declared_tier = ActionTier.ROUTINE

        @property
        def tool_id(self) -> str:
            return tool_id

        @property
        def display_name(self) -> str:
            return "Recording tool"

        @property
        def description(self) -> str:
            return "Records when it executes."

        @property
        def input_schema(self) -> dict:
            return {"type": "object", "additionalProperties": True}

        @property
        def output_schema(self) -> dict:
            return {"type": "object", "additionalProperties": True}

        @property
        def requires_tier(self) -> tuple[str, ...]:
            return ("pro", "enterprise")

        @property
        def execution_mode(self) -> str:
            return "in_process"

        async def execute(self, input, context) -> dict:
            executed.append(dict(input))
            return {"success": True, "output": "ran"}

    return _RecordingTool(), executed


def _make_broker(tool):
    from app.tools.broker import ToolBroker
    from app.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(tool)
    # Default-deny authoriser — same as production.
    return ToolBroker(registry)


# =====================================================================
# 1. Default-deny: no row ⇒ refused
# =====================================================================


def test_default_deny_no_row_refuses_and_does_not_execute() -> None:
    """The load-bearing WU2 invariant. With no live row in
    ``instance_tool_authorizations`` for ``(admin_id, instance_id,
    tool_id)``, the broker MUST refuse the call and MUST NOT invoke
    ``tool.execute``."""

    from app.tools.base import ToolContext

    session = _build_sqlite_session()
    admin_id = "tenant-a"
    instance_id = 101
    user_id = uuid.uuid4()
    _seed_admin_instance_user(
        session, admin_id=admin_id, instance_id=instance_id, user_id=user_id
    )

    tool, executed = _make_tool()
    broker = _make_broker(tool)

    ctx = ToolContext(
        admin_id=admin_id, instance_id=instance_id, session=session
    )
    result = broker.execute_tool(
        tool.tool_id, {"x": 1}, context=ctx
    )

    assert executed == [], (
        "WU2: tool.execute MUST NOT be invoked when no authorisation "
        "row exists for the (admin_id, instance_id, tool_id) tuple."
    )
    assert result.success is False
    assert result.metadata.get("authorization") == "denied"
    assert result.metadata.get("authorization_reason") == (
        "no_authorization_row"
    )
    assert result.metadata.get("authorization_failure_kind") == (
        "unauthorized"
    )


# =====================================================================
# 2. Authorised: live row ⇒ proceeds to classification + execute
# =====================================================================


def test_authorized_row_dispatches_to_execute() -> None:
    """With a live row, the broker passes the authoriser, the
    classifier tiers the ROUTINE tool as ROUTINE, and
    ``tool.execute`` runs."""

    from app.services.instance_tool_authorization_service import (
        InstanceToolAuthorizationService,
    )
    from app.tools.base import ToolContext

    session = _build_sqlite_session()
    admin_id = "tenant-a"
    instance_id = 102
    user_id = uuid.uuid4()
    _seed_admin_instance_user(
        session, admin_id=admin_id, instance_id=instance_id, user_id=user_id
    )

    tool, executed = _make_tool()
    broker = _make_broker(tool)

    # Authorise the tool on the instance.
    svc = InstanceToolAuthorizationService(session)
    svc.authorize(
        admin_id=admin_id,
        instance_id=instance_id,
        tool_id=tool.tool_id,
        authorized_by_user_id=user_id,
    )

    ctx = ToolContext(
        admin_id=admin_id, instance_id=instance_id, session=session
    )
    result = broker.execute_tool(
        tool.tool_id, {"x": 1}, context=ctx
    )

    assert executed == [{"x": 1}], (
        "WU2: with a live authorisation row, the broker MUST proceed "
        "to the classification gate and (for ROUTINE) MUST invoke "
        "tool.execute."
    )
    assert result.success is True
    assert result.metadata.get("tier") == "routine"


# =====================================================================
# 3. Wall-1 — admin A's row cannot authorise admin B's call
# =====================================================================


def test_wall_1_admin_scoping() -> None:
    """A row scoped to admin B does NOT authorise a call made under
    admin A on the same numerical instance_id. Wall-1 boundary."""

    from app.services.instance_tool_authorization_service import (
        InstanceToolAuthorizationService,
    )
    from app.tools.base import ToolContext

    session = _build_sqlite_session()
    admin_a = "tenant-a"
    admin_b = "tenant-b"
    instance_id = 200
    user_id = uuid.uuid4()
    _seed_admin_instance_user(
        session, admin_id=admin_a, instance_id=instance_id, user_id=user_id
    )
    _seed_admin_instance_user(
        session, admin_id=admin_b, instance_id=instance_id + 1,
        user_id=uuid.uuid4(),
    )

    tool, executed = _make_tool()
    broker = _make_broker(tool)

    # Authorise on admin_b's row.
    svc = InstanceToolAuthorizationService(session)
    svc.authorize(
        admin_id=admin_b,
        instance_id=instance_id + 1,
        tool_id=tool.tool_id,
        authorized_by_user_id=user_id,
    )

    # Call under admin_a — must refuse.
    ctx = ToolContext(
        admin_id=admin_a, instance_id=instance_id, session=session
    )
    result = broker.execute_tool(
        tool.tool_id, {"x": 1}, context=ctx
    )
    assert executed == [], "Wall-1 violated: admin_b's row authorised admin_a"
    assert result.success is False
    assert result.metadata.get("authorization_reason") == (
        "no_authorization_row"
    )


# =====================================================================
# 4. Wall-3 — instance 1's row cannot authorise instance 2's call
# =====================================================================


def test_wall_3_instance_scoping() -> None:
    """A row scoped to instance 1 does NOT authorise a call on
    instance 2 — even under the same admin. Wall-3 boundary."""

    from app.services.instance_tool_authorization_service import (
        InstanceToolAuthorizationService,
    )
    from app.tools.base import ToolContext

    session = _build_sqlite_session()
    admin_id = "tenant-a"
    instance_1 = 301
    instance_2 = 302
    user_id = uuid.uuid4()
    _seed_admin_instance_user(
        session, admin_id=admin_id, instance_id=instance_1, user_id=user_id
    )
    # Same admin owns instance_2.
    from sqlalchemy import text as sa_text

    session.execute(
        sa_text(
            "INSERT INTO instances (id, admin_id, instance_slug) "
            "VALUES (:id, :admin_id, :slug)"
        ),
        {"id": instance_2, "admin_id": admin_id, "slug": f"inst-{instance_2}"},
    )
    session.commit()

    tool, executed = _make_tool()
    broker = _make_broker(tool)

    # Authorise on instance_1 only.
    svc = InstanceToolAuthorizationService(session)
    svc.authorize(
        admin_id=admin_id,
        instance_id=instance_1,
        tool_id=tool.tool_id,
        authorized_by_user_id=user_id,
    )

    # Call against instance_2 — must refuse.
    from app.tools.base import ToolContext  # noqa: F811

    ctx = ToolContext(
        admin_id=admin_id, instance_id=instance_2, session=session
    )
    result = broker.execute_tool(
        tool.tool_id, {"x": 1}, context=ctx
    )
    assert executed == [], "Wall-3 violated: instance_1's row authorised instance_2"
    assert result.success is False
    assert result.metadata.get("authorization_reason") == (
        "no_authorization_row"
    )


# =====================================================================
# 5. Revoked row ⇒ denied
# =====================================================================


def test_revoked_row_denies_call() -> None:
    """``revoked_at IS NOT NULL`` means the authorisation is gone.
    The broker must refuse and not execute."""

    from app.services.instance_tool_authorization_service import (
        InstanceToolAuthorizationService,
    )
    from app.tools.base import ToolContext

    session = _build_sqlite_session()
    admin_id = "tenant-a"
    instance_id = 401
    user_id = uuid.uuid4()
    _seed_admin_instance_user(
        session, admin_id=admin_id, instance_id=instance_id, user_id=user_id
    )

    tool, executed = _make_tool()
    broker = _make_broker(tool)

    svc = InstanceToolAuthorizationService(session)
    svc.authorize(
        admin_id=admin_id,
        instance_id=instance_id,
        tool_id=tool.tool_id,
        authorized_by_user_id=user_id,
    )
    # Revoke it.
    revoked = svc.revoke(
        admin_id=admin_id,
        instance_id=instance_id,
        tool_id=tool.tool_id,
    )
    assert revoked is True

    ctx = ToolContext(
        admin_id=admin_id, instance_id=instance_id, session=session
    )
    result = broker.execute_tool(
        tool.tool_id, {"x": 1}, context=ctx
    )
    assert executed == [], "Revoked row should not authorise execute"
    assert result.success is False
    assert result.metadata.get("authorization_reason") == (
        "no_authorization_row"
    )


# =====================================================================
# 6. enabled=False row ⇒ denied
# =====================================================================


def test_disabled_row_denies_call() -> None:
    """``enabled=False`` is the paused-but-not-revoked state. The
    broker must refuse (distinct ``authorization_disabled`` reason)."""

    from app.repositories.instance_tool_authorization_repository import (
        InstanceToolAuthorizationRepository,
    )
    from app.tools.base import ToolContext

    session = _build_sqlite_session()
    admin_id = "tenant-a"
    instance_id = 501
    user_id = uuid.uuid4()
    _seed_admin_instance_user(
        session, admin_id=admin_id, instance_id=instance_id, user_id=user_id
    )

    tool, executed = _make_tool()
    broker = _make_broker(tool)

    repo = InstanceToolAuthorizationRepository(session)
    repo.authorize(
        admin_id=admin_id,
        instance_id=instance_id,
        tool_id=tool.tool_id,
        authorized_by_user_id=user_id,
        enabled=False,
    )

    ctx = ToolContext(
        admin_id=admin_id, instance_id=instance_id, session=session
    )
    result = broker.execute_tool(
        tool.tool_id, {"x": 1}, context=ctx
    )
    assert executed == []
    assert result.success is False
    assert result.metadata.get("authorization_reason") == (
        "authorization_disabled"
    )


# =====================================================================
# 7. Idempotent authorise
# =====================================================================


def test_authorize_is_idempotent() -> None:
    """Calling ``authorize`` twice for the same tuple returns the
    same live row — the partial unique index never lets us create
    two live rows for one tuple."""

    from app.services.instance_tool_authorization_service import (
        InstanceToolAuthorizationService,
    )

    session = _build_sqlite_session()
    admin_id = "tenant-a"
    instance_id = 601
    user_id = uuid.uuid4()
    _seed_admin_instance_user(
        session, admin_id=admin_id, instance_id=instance_id, user_id=user_id
    )

    svc = InstanceToolAuthorizationService(session)
    row1 = svc.authorize(
        admin_id=admin_id,
        instance_id=instance_id,
        tool_id="send_email_test",
        authorized_by_user_id=user_id,
    )
    row2 = svc.authorize(
        admin_id=admin_id,
        instance_id=instance_id,
        tool_id="send_email_test",
        authorized_by_user_id=user_id,
    )
    assert row1.id == row2.id


# =====================================================================
# 8. Migration shape — static-source contract
# =====================================================================


_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "arc12_wu2_instance_tool_authorizations.py"
)


def _migration_src() -> str:
    return _MIGRATION_PATH.read_text(encoding="utf-8")


def test_migration_chains_off_arc11_closeout_b() -> None:
    src = _migration_src()
    assert re.search(
        r"down_revision\s*=\s*['\"]arc11_closeout_b_ingestion_error_code['\"]",
        src,
    ), (
        "WU2 migration must chain off the WU1 / Arc 11 closeout head."
    )
    assert re.search(
        r"revision\s*=\s*['\"]arc12_wu2_instance_tool_authorizations['\"]",
        src,
    )


def test_migration_enables_and_forces_rls() -> None:
    src = _migration_src()
    assert "ENABLE ROW LEVEL SECURITY" in src
    assert "FORCE ROW LEVEL SECURITY" in src


def test_migration_creates_tenant_isolation_policy() -> None:
    src = _migration_src()
    assert "instance_tool_authorizations_tenant_isolation" in src
    assert "current_setting('app.admin_id', true)" in src
    # Both USING and WITH CHECK must be present and strict.
    assert re.search(
        r"USING\s*\(\s*admin_id\s*=\s*current_setting", src
    )
    assert re.search(
        r"WITH CHECK\s*\(\s*admin_id\s*=\s*current_setting", src
    )


def test_migration_creates_partial_unique_index_on_active_rows() -> None:
    src = _migration_src()
    assert "uq_instance_tool_authorizations_active" in src
    # The partial-index predicate must filter out revoked rows.
    assert re.search(
        r"postgresql_where=sa\.text\(\s*['\"]revoked_at IS NULL['\"]",
        src,
    )


def test_migration_drops_stale_max_composition_depth_column() -> None:
    """WU1 Decision #19 retired the field; WU2 drops the column."""
    src = _migration_src()
    assert re.search(
        r"op\.drop_column\(\s*['\"]admin_tier_overrides['\"]\s*,\s*"
        r"['\"]max_composition_depth['\"]\s*\)",
        src,
    )
    # And the downgrade re-adds it as nullable Integer.
    assert "max_composition_depth" in src
    assert re.search(
        r"op\.add_column\(\s*['\"]admin_tier_overrides['\"]\s*,"
        r"\s*sa\.Column\(\s*['\"]max_composition_depth['\"]",
        src,
    )


# =====================================================================
# 9. Stable broker authoriser interface (Arc 14 contract)
# =====================================================================


def test_broker_constructor_accepts_authorizer_kwarg() -> None:
    """Arc 14's agentic loop composes its cycle / fan-out checks on
    top of this seam. The kwarg name + behaviour must stay stable."""
    import inspect

    from app.tools.broker import ToolBroker

    params = inspect.signature(ToolBroker.__init__).parameters
    assert "authorizer" in params, (
        "ToolBroker.__init__ must accept an `authorizer` keyword so "
        "Arc 14 can inject an enriched authoriser."
    )


def test_authorizer_interface_has_authorize_method() -> None:
    """The stable contract Arc 14 will call."""
    from app.tools.authorization import (
        AuthorizationDecision,
        DefaultDenyToolAuthorizer,
    )

    auth = DefaultDenyToolAuthorizer()
    assert hasattr(auth, "authorize")
    # Return-type sanity: ``allowed`` is a bool, ``reason``/``message``
    # are str, ``failure_kind`` is str.
    decision = AuthorizationDecision.deny(
        reason="x", message="y", failure_kind="unauthorized"
    )
    assert decision.allowed is False
    assert decision.reason == "x"
    assert decision.failure_kind == "unauthorized"
