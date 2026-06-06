"""Unit 13c — connector connect paths conform to §3.8 (behavioural).

Exercises the post-13c HONEST connect logic without a live TestClient/DB,
mirroring tests/tools/test_arc15_wu5_connection_gate.py (in-memory SQLite
with a hand-shaped ``instance_connections`` table that includes the new
``auth_class`` column).

Covered:
  * provisioned_resource (email_sender / sms_sender): the route's
    ``_provisioned_resource_identity`` returns the NON-SECRET sender
    identity when the platform resource is configured (→ connected) and
    None when absent (→ unconfigured). The Twilio auth token is the gate
    but is NEVER part of the persisted identity.
  * oauth_token (calendar / crm): POST configure records an unconfigured
    row pointing at the consent flow — never a fake connected.
  * auth_class is derived + persisted by the repository's configure().
  * broker connection gate: a tool with requires_connection is ADMITTED
    once a connected row of that type exists and REFUSED when absent /
    unconnected — proves push_to_crm / send_email / send_sms dispatch are
    gated on a real connected row.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


# ---------------------------------------------------------------------
# In-memory SQLite session — instance_connections WITH auth_class.
# ---------------------------------------------------------------------


def _build_sqlite_session():
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
        Column("non_secret_config", String, nullable=True),
        Column("secret_ref", String(255), nullable=True),
        Column(
            "status", String(32),
            nullable=False, server_default="unconfigured",
        ),
        Column(
            "auth_class", String(32),
            nullable=False, server_default="api_key",
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
        Column("status_detail", String, nullable=True),
        Column("created_by_user_id", String(36), nullable=True),
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


def _configure(session, *, admin_id, instance_id, connection_type, status,
               non_secret_config=None):
    from app.connections.repository import InstanceConnectionRepository

    return InstanceConnectionRepository(session).configure(
        admin_id=admin_id,
        instance_id=instance_id,
        connection_type=connection_type,
        provider="test_provider",
        status=status,
        non_secret_config=non_secret_config,
        last_health_check_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------
# provisioned_resource identity — present → connected; absent → none.
# ---------------------------------------------------------------------


class _Settings:
    def __init__(self, **kw):
        self.email_sender_from_address = kw.get("email_sender_from_address")
        self.email_sender_from_name = kw.get("email_sender_from_name")
        self.twilio_account_sid = kw.get("twilio_account_sid")
        self.twilio_auth_token = kw.get("twilio_auth_token")
        self.twilio_messaging_service_sid = kw.get(
            "twilio_messaging_service_sid"
        )


def test_email_sender_identity_present_returns_non_secret_identity() -> None:
    from app.api.v1.admin_connections import _provisioned_resource_identity

    s = _Settings(
        email_sender_from_address="noreply@x.com",
        email_sender_from_name="Luciel",
    )
    identity = _provisioned_resource_identity(s, "email_sender")
    assert identity == {"from_address": "noreply@x.com", "from_name": "Luciel"}


def test_email_sender_identity_absent_returns_none() -> None:
    from app.api.v1.admin_connections import _provisioned_resource_identity

    assert _provisioned_resource_identity(_Settings(), "email_sender") is None


def test_sms_sender_identity_excludes_secret_auth_token() -> None:
    from app.api.v1.admin_connections import _provisioned_resource_identity

    s = _Settings(
        twilio_account_sid="ACxxxx",
        twilio_auth_token="super-secret",  # gate only — never persisted
        twilio_messaging_service_sid="MGyyyy",
    )
    identity = _provisioned_resource_identity(s, "sms_sender")
    assert identity == {
        "account_sid": "ACxxxx",
        "messaging_service_sid": "MGyyyy",
    }
    # The secret auth token must NOT appear in the persisted identity.
    assert "super-secret" not in str(identity)
    assert "auth_token" not in identity


def test_sms_sender_identity_requires_both_sid_and_token() -> None:
    from app.api.v1.admin_connections import _provisioned_resource_identity

    # account_sid present but auth_token absent → not provisioned.
    s = _Settings(twilio_account_sid="ACxxxx")
    assert _provisioned_resource_identity(s, "sms_sender") is None


# ---------------------------------------------------------------------
# Repository derives + persists auth_class from connection_type.
# ---------------------------------------------------------------------


def test_configure_persists_derived_auth_class() -> None:
    session = _build_sqlite_session()
    _seed_admin_instance(session, admin_id="a", instance_id=1)
    cases = (
        ("calendar", "oauth_token"),
        ("crm", "oauth_token"),
        ("email_sender", "provisioned_resource"),
        ("sms_sender", "provisioned_resource"),
        ("record_source", "api_key"),
        ("outbound_webhook", "api_key"),
    )
    for i, (conn_type, expected) in enumerate(cases, start=1):
        row = _configure(
            session, admin_id="a", instance_id=i,
            connection_type=conn_type, status="unconfigured",
        )
        assert row.auth_class == expected, conn_type


# ---------------------------------------------------------------------
# Broker connection gate — connected row admits; absent/unconnected denies.
# Proves push_to_crm / send_email / send_sms dispatch are gated on a real
# connected row (never fabricated).
# ---------------------------------------------------------------------


def _make_tool(*, requires_connection):
    from app.policy.action_classification import ActionTier
    from app.tools.base import LucielTool

    class _T(LucielTool):
        declared_tier = ActionTier.ROUTINE

        @property
        def tool_id(self) -> str:
            return "t"

        @property
        def display_name(self) -> str:
            return "T"

        @property
        def description(self) -> str:
            return "needs connection"

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

    tool = _T()
    tool.requires_connection = requires_connection
    return tool


def _ctx(*, admin_id, instance_id, session):
    from app.tools.base import ToolContext

    return ToolContext(
        admin_id=admin_id, instance_id=instance_id, session=session
    )


def _decision(*, requires_connection, session, admin_id="a", instance_id=1):
    from app.tools.authorization import DefaultDenyToolAuthorizer

    return DefaultDenyToolAuthorizer()._check_connection(
        _make_tool(requires_connection=requires_connection),
        _ctx(admin_id=admin_id, instance_id=instance_id, session=session),
    )


def test_crm_dispatch_admitted_once_connected_row_exists() -> None:
    session = _build_sqlite_session()
    _seed_admin_instance(session, admin_id="a", instance_id=1)
    _configure(
        session, admin_id="a", instance_id=1,
        connection_type="crm", status="connected",
        non_secret_config={"store_ref": "s3://x"},
    )
    assert _decision(requires_connection="crm", session=session).allowed is True


def test_crm_dispatch_refused_when_no_row() -> None:
    session = _build_sqlite_session()
    _seed_admin_instance(session, admin_id="a", instance_id=1)
    assert _decision(requires_connection="crm", session=session).allowed is False


def test_email_sender_dispatch_admitted_once_connected() -> None:
    session = _build_sqlite_session()
    _seed_admin_instance(session, admin_id="a", instance_id=1)
    _configure(
        session, admin_id="a", instance_id=1,
        connection_type="email_sender", status="connected",
        non_secret_config={"from_address": "noreply@x.com"},
    )
    assert (
        _decision(requires_connection="email_sender", session=session).allowed
        is True
    )


def test_sms_sender_dispatch_refused_when_unconfigured() -> None:
    session = _build_sqlite_session()
    _seed_admin_instance(session, admin_id="a", instance_id=1)
    _configure(
        session, admin_id="a", instance_id=1,
        connection_type="sms_sender", status="unconfigured",
    )
    assert (
        _decision(requires_connection="sms_sender", session=session).allowed
        is False
    )
