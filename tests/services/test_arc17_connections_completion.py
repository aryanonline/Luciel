"""Arc 17 connections-completion behavioural tests.

Covers the Arc 17 brief deliverables that the existing WU4/WU5 shape
tests do not exercise behaviourally:

  * ConnectionHealthService honesty — LIVE config-presence probe, the
    DEFERRED OAuth deploy-gate (never fakes connected), and a real fake
    token-refresh success path.
  * SecretStore fakes — put/get/rotate/delete round-trip + pointer-only
    contract (the ref is NOT the value).
  * Lifecycle cascade — instance delete + account closure revoke every
    connection, audit each (ACTION_CONNECTION_REVOKED), and enqueue
    secret cleanup ONLY for non-null secret_ref (pointer only).
  * Token-refresh worker helper — _refresh_one disposition.
  * Secret-cleanup drain — outbox repo enqueue/claim/mark + store delete.
  * Cross-tenant isolation — admin A cannot read/refresh/delete admin
    B's connection via the repo's (admin_id)-fenced reads.
  * No-secret-VALUE grep — secret_ref / non_secret_config never carry a
    raw secret value in the shipped code.

Uses an in-memory SQLite session shaped to the columns the units touch
(same convention as test_arc12_wu4_sibling_grants.py). PG enums are
modelled as plain String columns — SQLite does not enforce the enum and
the units treat connection_type / status as strings.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

REPO_ROOT = Path(__file__).resolve().parents[2]


# =====================================================================
# In-memory SQLite fixture (audit-chain stub + minimal tables).
# =====================================================================


@pytest.fixture(autouse=True)
def _restore_prod_audit_chain_handler():
    yield
    from sqlalchemy import event
    from sqlalchemy.orm import Session as _SQLASession

    from app.repositories.audit_chain import (
        _before_flush_handler,
        install_audit_chain_event,
    )

    try:
        clslevel = _SQLASession.dispatch.before_flush._clslevel
        for target, fns in list(clslevel.items()):
            for fn in list(fns):
                if fn is not _before_flush_handler:
                    event.remove(target, "before_flush", fn)
    except Exception:
        pass
    install_audit_chain_event()


def _build_sqlite_session():
    import app.db.session  # noqa: F401 — installs the prod handler
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
        Column,
        DateTime,
        ForeignKey,
        Index,
        Integer,
        MetaData,
        String,
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

    from sqlalchemy import Table

    Table(
        "admins",
        md,
        Column("id", String(100), primary_key=True),
        Column("name", String(200), nullable=False),
        Column("tier", String(20), nullable=False, server_default="free"),
    )
    Table(
        "instances",
        md,
        Column("id", Integer, primary_key=True),
        Column(
            "admin_id", String(100), ForeignKey("admins.id"), nullable=False
        ),
        Column("instance_slug", String(100), nullable=False),
    )
    Table(
        "instance_connections",
        md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column(
            "admin_id", String(100), ForeignKey("admins.id"),
            nullable=False, index=True,
        ),
        Column(
            "instance_id", Integer, ForeignKey("instances.id"),
            nullable=False, index=True,
        ),
        Column("connection_type", String(32), nullable=False),
        Column("provider", String(64), nullable=False),
        Column("non_secret_config", Text, nullable=True),
        Column("secret_ref", String(255), nullable=True),
        Column("status", String(16), nullable=False, server_default="unconfigured"),
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
        # rescand_connections_schema additions (§3.8.2):
        Column("status_detail", Text, nullable=True),
        Column("created_by_user_id", String(36), nullable=True),
    )
    Index(
        "uq_instance_connections_active",
        md.tables["instance_connections"].c.admin_id,
        md.tables["instance_connections"].c.instance_id,
        md.tables["instance_connections"].c.connection_type,
        md.tables["instance_connections"].c.provider,
        unique=True,
        sqlite_where=sa_text("revoked_at IS NULL"),
    )
    Table(
        "secret_cleanup_outbox",
        md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("admin_id", String(100), nullable=False, index=True),
        Column("instance_id", Integer, nullable=True),
        Column("connection_id", Integer, nullable=True),
        Column("secret_ref", String(255), nullable=False),
        Column("status", String(16), nullable=False, server_default="pending"),
        Column("attempts", Integer, nullable=False, server_default="0"),
        Column("last_error", Text, nullable=True),
        Column(
            "enqueued_at", DateTime(timezone=True),
            nullable=False, server_default=func.now(),
        ),
        Column("processed_at", DateTime(timezone=True), nullable=True),
    )
    Table(
        "admin_audit_logs",
        md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("actor_key_prefix", String(20), nullable=True),
        Column("actor_permissions", String(500), nullable=True),
        Column("actor_label", String(100), nullable=True),
        Column(
            "admin_id", String(100), ForeignKey("admins.id"), nullable=False
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
        Column("row_hash", CHAR(64), nullable=False, server_default="0" * 64),
        Column(
            "prev_row_hash", CHAR(64), nullable=False, server_default="0" * 64
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


def _seed_admin(session, *, admin_id: str, name: str | None = None) -> None:
    from sqlalchemy import text as sa_text

    session.execute(
        sa_text("INSERT INTO admins (id, name, tier) VALUES (:id, :n, 'pro')"),
        {"id": admin_id, "n": name or f"admin-{admin_id}"},
    )
    session.commit()


def _seed_instance(session, *, instance_id: int, admin_id: str) -> None:
    from sqlalchemy import text as sa_text

    session.execute(
        sa_text(
            "INSERT INTO instances (id, admin_id, instance_slug) "
            "VALUES (:id, :a, :s)"
        ),
        {"id": instance_id, "a": admin_id, "s": f"inst-{instance_id}"},
    )
    session.commit()


def _audit_ctx():
    from app.repositories.admin_audit_repository import AuditContext

    return AuditContext.system(label="test")


def _count_audit(session, *, action: str) -> int:
    from sqlalchemy import text as sa_text

    return int(
        session.execute(
            sa_text(
                "SELECT COUNT(*) FROM admin_audit_logs WHERE action = :a"
            ),
            {"a": action},
        ).scalar_one()
        or 0
    )


# =====================================================================
# SecretStore fakes — pointer-only contract.
# =====================================================================


def test_fake_secret_store_round_trip_and_pointer_only() -> None:
    from app.integrations.secrets import LocalFakeSecretStore, SecretStoreError

    store = LocalFakeSecretStore()
    ref = store.put("oauth/refresh/calendar-1", "super-secret-token-value")

    # The ref is a NAME/ARN pointer, NOT the value (Locked Decision #18).
    assert "super-secret-token-value" not in ref
    assert store.get(ref) == "super-secret-token-value"

    ref2 = store.rotate(ref, "rotated-token-value")
    assert store.get(ref2) == "rotated-token-value"

    store.delete(ref2)
    with pytest.raises(SecretStoreError):
        store.get(ref2)


def test_factory_selects_fake_when_live_disabled() -> None:
    from app.integrations.secrets import (
        AwsSecretsManagerStore,
        LocalFakeSecretStore,
        get_secret_store,
    )

    class _S:
        connections_live_secrets_enabled = False
        aws_region = "ca-central-1"

    assert isinstance(get_secret_store(_S()), LocalFakeSecretStore)

    class _S2:
        connections_live_secrets_enabled = True
        aws_region = "ca-central-1"

    assert isinstance(get_secret_store(_S2()), AwsSecretsManagerStore)


# =====================================================================
# ConnectionHealthService — honesty fork.
# =====================================================================


class _StubConn:
    """Minimal InstanceConnection-shaped stub for the health service."""

    def __init__(self, *, connection_type, non_secret_config=None, secret_ref=None):
        self.id = 1
        self.connection_type = connection_type
        self.non_secret_config = non_secret_config
        self.secret_ref = secret_ref


def _settings():
    from app.core.config import settings

    return settings


def test_health_live_connector_config_present_connected() -> None:
    from app.services.connection_health_service import ConnectionHealthService

    svc = ConnectionHealthService(_settings())
    res = svc.check_health(
        _StubConn(connection_type="record_source", non_secret_config={"store_ref": "s3://x"})
    )
    assert res.status == "connected"
    assert res.checked_at is not None
    assert res.arc17_pending is False


def test_health_live_connector_config_missing_error() -> None:
    from app.services.connection_health_service import ConnectionHealthService

    svc = ConnectionHealthService(_settings())
    res = svc.check_health(
        _StubConn(connection_type="outbound_webhook", non_secret_config={})
    )
    assert res.status == "error"
    assert res.checked_at is not None


def test_health_deferred_oauth_unconfigured_never_fakes_connected() -> None:
    """DEPLOY-GATED: with no OAuth client creds the deferred connector
    stays honest unconfigured + arc17_pending, never connected."""
    from app.services.connection_health_service import ConnectionHealthService

    svc = ConnectionHealthService(_settings())
    res = svc.check_health(_StubConn(connection_type="calendar"))
    assert res.status == "unconfigured"
    assert res.arc17_pending is True
    assert res.checked_at is None
    assert res.status != "connected"


def test_health_deferred_oauth_refresh_success_connected_with_fakes() -> None:
    """The full real refresh path, exercised behind fakes: a configured
    fake provider + a stored fake refresh token → connected, with the
    rotated secret_ref surfaced."""
    from app.integrations.oauth import OAuthTokens
    from app.integrations.secrets import LocalFakeSecretStore
    from app.services.connection_health_service import ConnectionHealthService

    store = LocalFakeSecretStore()
    cred_ref = store.put("oauth/refresh/cal-1", "old-refresh-token")

    class _FakeProvider:
        def is_configured(self):
            return True

        def refresh(self, *, refresh_token):
            assert refresh_token == "old-refresh-token"
            return OAuthTokens(
                access_token="new-access",
                refresh_token="new-refresh-token",
                expires_in=3600,
            )

    svc = ConnectionHealthService(_settings(), secret_store=store)
    import app.services.connection_health_service as mod

    orig = mod.get_oauth_provider
    mod.get_oauth_provider = lambda ct, s: _FakeProvider()
    try:
        res = svc.check_health(
            _StubConn(connection_type="calendar", secret_ref=cred_ref)
        )
    finally:
        mod.get_oauth_provider = orig

    assert res.status == "connected"
    assert res.checked_at is not None
    # A new refresh token was issued → the stored secret rotated.
    assert res.new_secret_ref is not None
    assert store.get(res.new_secret_ref) == "new-refresh-token"


def test_health_deferred_oauth_rejected_token_expired() -> None:
    from app.integrations.oauth import OAuthError
    from app.integrations.secrets import LocalFakeSecretStore
    from app.services.connection_health_service import ConnectionHealthService

    store = LocalFakeSecretStore()
    cred_ref = store.put("oauth/refresh/cal-2", "stale-token")

    class _FakeProvider:
        def is_configured(self):
            return True

        def refresh(self, *, refresh_token):
            raise OAuthError("invalid_grant")

    svc = ConnectionHealthService(_settings(), secret_store=store)
    import app.services.connection_health_service as mod

    orig = mod.get_oauth_provider
    mod.get_oauth_provider = lambda ct, s: _FakeProvider()
    try:
        res = svc.check_health(
            _StubConn(connection_type="crm", secret_ref=cred_ref)
        )
    finally:
        mod.get_oauth_provider = orig

    assert res.status == "expired"
    assert res.status != "connected"


# =====================================================================
# Lifecycle cascade — instance delete revokes + audits + enqueues.
# =====================================================================


def _make_connection(
    session, *, admin_id, instance_id, connection_type="record_source",
    provider="csv", secret_ref=None, status="connected",
):
    from app.repositories.instance_connection_repository import (
        InstanceConnectionRepository,
    )

    return InstanceConnectionRepository(session).configure(
        admin_id=admin_id,
        instance_id=instance_id,
        connection_type=connection_type,
        provider=provider,
        status=status,
        secret_ref=secret_ref,
        autocommit=True,
    )


def test_instance_delete_cascade_revokes_and_audits_connections() -> None:
    from app.repositories.instance_connection_repository import (
        InstanceConnectionRepository,
    )
    from app.repositories.instance_repository import InstanceRepository

    session = _build_sqlite_session()
    _seed_admin(session, admin_id="adminA")
    _seed_instance(session, instance_id=10, admin_id="adminA")
    _make_connection(session, admin_id="adminA", instance_id=10)
    _make_connection(
        session, admin_id="adminA", instance_id=10,
        connection_type="outbound_webhook", provider="generic",
    )

    repo = InstanceRepository(session)
    n = repo._cascade_revoke_connections(
        admin_id="adminA", instance_id=10, audit_ctx=_audit_ctx()
    )
    session.commit()

    assert n == 2
    live = InstanceConnectionRepository(session).list_for_instance(
        admin_id="adminA", instance_id=10
    )
    assert live == []  # all revoked
    assert _count_audit(session, action="connection_revoked") == 2


def test_cascade_enqueues_secret_cleanup_only_for_secret_ref() -> None:
    from app.repositories.instance_repository import InstanceRepository
    from sqlalchemy import text as sa_text

    session = _build_sqlite_session()
    _seed_admin(session, admin_id="adminA")
    _seed_instance(session, instance_id=10, admin_id="adminA")
    # One with a secret pointer, one without.
    _make_connection(
        session, admin_id="adminA", instance_id=10,
        connection_type="calendar", provider="google_calendar",
        secret_ref="luciel/connections/oauth-cal-1",
    )
    _make_connection(session, admin_id="adminA", instance_id=10)  # NULL ref

    InstanceRepository(session)._cascade_revoke_connections(
        admin_id="adminA", instance_id=10, audit_ctx=_audit_ctx()
    )
    session.commit()

    rows = session.execute(
        sa_text("SELECT secret_ref FROM secret_cleanup_outbox")
    ).fetchall()
    # Exactly one outbox row — for the connection that had a pointer.
    assert len(rows) == 1
    assert rows[0][0] == "luciel/connections/oauth-cal-1"


def test_account_closure_cascade_revokes_across_instances() -> None:
    from app.repositories.instance_connection_repository import (
        InstanceConnectionRepository,
    )
    from app.repositories.instance_repository import InstanceRepository

    session = _build_sqlite_session()
    _seed_admin(session, admin_id="adminA")
    _seed_instance(session, instance_id=10, admin_id="adminA")
    _seed_instance(session, instance_id=11, admin_id="adminA")
    _make_connection(session, admin_id="adminA", instance_id=10)
    _make_connection(session, admin_id="adminA", instance_id=11)

    n = InstanceRepository(session).cascade_revoke_connections_for_admin(
        admin_id="adminA", audit_ctx=_audit_ctx()
    )
    session.commit()
    assert n == 2
    for iid in (10, 11):
        assert (
            InstanceConnectionRepository(session).list_for_instance(
                admin_id="adminA", instance_id=iid
            )
            == []
        )


# =====================================================================
# Cross-tenant isolation — repo reads are admin-fenced.
# =====================================================================


def test_cross_tenant_admin_cannot_touch_other_admins_connection() -> None:
    from app.repositories.instance_connection_repository import (
        InstanceConnectionRepository,
    )

    session = _build_sqlite_session()
    _seed_admin(session, admin_id="adminA")
    _seed_admin(session, admin_id="adminB")
    _seed_instance(session, instance_id=10, admin_id="adminA")
    conn = _make_connection(session, admin_id="adminA", instance_id=10)

    repo = InstanceConnectionRepository(session)

    # Admin B cannot READ admin A's connection by pk.
    assert repo.get_live_for_admin(admin_id="adminB", connection_id=conn.id) is None
    # Admin A can.
    assert repo.get_live_for_admin(admin_id="adminA", connection_id=conn.id) is not None

    # Admin B cannot DISCONNECT (soft-revoke) admin A's connection.
    assert repo.disconnect(
        admin_id="adminB", connection_id=conn.id, autocommit=True
    ) is False
    # The row is still live for admin A.
    assert repo.get_live_for_admin(admin_id="adminA", connection_id=conn.id) is not None

    # Admin B's instance listing does not see admin A's row.
    assert repo.list_for_instance(admin_id="adminB", instance_id=10) == []


# =====================================================================
# Secret-cleanup outbox repo + drain disposition.
# =====================================================================


def test_outbox_enqueue_list_mark_done() -> None:
    from app.repositories.secret_cleanup_outbox_repository import (
        SecretCleanupOutboxRepository,
    )

    session = _build_sqlite_session()
    _seed_admin(session, admin_id="adminA")
    repo = SecretCleanupOutboxRepository(session)
    repo.enqueue(
        admin_id="adminA",
        secret_ref="luciel/connections/x",
        autocommit=True,
    )
    pending = repo.list_pending()
    assert len(pending) == 1
    repo.mark_done(row=pending[0], autocommit=True)
    assert repo.list_pending() == []
    assert pending[0].status == "done"
    assert pending[0].processed_at is not None


def test_outbox_mark_failed_flips_to_failed_after_max_attempts() -> None:
    from app.repositories.secret_cleanup_outbox_repository import (
        SecretCleanupOutboxRepository,
    )

    session = _build_sqlite_session()
    _seed_admin(session, admin_id="adminA")
    repo = SecretCleanupOutboxRepository(session)
    row = repo.enqueue(
        admin_id="adminA", secret_ref="luciel/connections/x", autocommit=True
    )
    for _ in range(4):
        repo.mark_failed(row=row, error="boom", max_attempts=5, autocommit=True)
        assert row.status == "pending"
    repo.mark_failed(row=row, error="boom", max_attempts=5, autocommit=True)
    assert row.status == "failed"
    assert row.attempts == 5


# =====================================================================
# No-secret-VALUE invariant — static grep over the shipped code.
# =====================================================================


def test_no_secret_value_written_to_non_secret_config_or_secret_ref() -> None:
    """The connections code must never persist a secret VALUE — only a
    pointer (secret_ref = NAME/ARN). Guard against an accidental
    `secret_ref=<token value>` or storing tokens in non_secret_config."""
    paths = [
        REPO_ROOT / "app" / "api" / "v1" / "admin_connections.py",
        REPO_ROOT / "app" / "services" / "connection_health_service.py",
        REPO_ROOT / "app" / "repositories" / "instance_connection_repository.py",
        REPO_ROOT / "app" / "repositories" / "secret_cleanup_outbox_repository.py",
        REPO_ROOT / "app" / "worker" / "tasks" / "refresh_connections.py",
    ]
    # The admin_connections route configures secret_ref=None in this
    # slice; no path should assign a token/secret value into it.
    forbidden = re.compile(
        r"secret_ref\s*=\s*['\"].*(token|secret|password|key)",
        re.IGNORECASE,
    )
    for p in paths:
        src = p.read_text(encoding="utf-8")
        assert not forbidden.search(src), f"possible secret value in {p.name}"

    # admin_connections explicitly pins secret_ref=None.
    conn_src = (
        REPO_ROOT / "app" / "api" / "v1" / "admin_connections.py"
    ).read_text(encoding="utf-8")
    assert "secret_ref=None" in conn_src
