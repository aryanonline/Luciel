"""Arc 12 WU2b — per-instance tool-authorization admin API tests.

Covers the binding-spec assertions for the new admin tools router:

  1. Routes register at the right paths / methods.
  2. Cognition behaviours (escalate / save_memory / session_summary)
     are NOT exposed in the GET response -- Decision #20.
  3. GET returns all 8 v1 catalog tools with the §3.3.1 contract
     fields + per-instance authorization state + tier-availability +
     channel-availability.
  4. Authorize creates the instance_tool_authorizations row and writes
     one ACTION_TOOL_AUTHORIZED audit row in the same transaction.
  5. Revoke soft-deletes the row and writes one ACTION_TOOL_REVOKED
     audit row.
  6. Authorize is rejected when the Admin's tier is not in
     ``tool.requires_tier`` (Free admin attempting an enterprise-only
     tool -> 403).
  7. Wall-2: read_only_viewer cannot toggle; owner + manager can;
     instance_operator cannot toggle.
  8. Wall-1 / Wall-3: a caller scoped to a different Admin / Instance
     cannot toggle.

Following the WU4 sibling-grants test pattern, the FastAPI plumbing
is bypassed: the route helpers and route bodies are exercised with a
synthesised Request state + a SQLite-backed session for the rows.
"""
from __future__ import annotations

import os
import uuid
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("OPENAI_API_KEY", "dummy")


@pytest.fixture(autouse=True)
def _restore_prod_audit_chain_handler():
    """Restore the prod audit-chain before_flush handler after each test.

    ``_build_sqlite_session`` swaps the prod ``_before_flush_handler``
    (Postgres-only ``pg_advisory_xact_lock``) for a SQLite stub that
    stamps placeholder ``row_hash``/``prev_row_hash`` = ``'0'*64``. The
    swap is GLOBAL — it mutates the ``sqlalchemy.orm.Session`` class — so
    without this teardown the stub leaks into every later test in the
    process. Real-Postgres tests that run after this module (e.g. the
    Arc 13 channel-provisioning audit tests) would then write
    zero-hash audit rows that collide on ``ux_admin_audit_logs_row_hash``.
    This fixture removes the stub and reinstalls the prod handler once the
    test finishes, regardless of how it exits.
    """
    yield
    from sqlalchemy import event
    from sqlalchemy.orm import Session as _SQLASession

    from app.repositories.audit_chain import (
        _before_flush_handler,
        install_audit_chain_event,
    )

    # Drop any sqlite stub left attached by _build_sqlite_session. The
    # stub is a closure, so we can't reference it by name here; strip any
    # before_flush listener that isn't the prod handler.
    try:
        clslevel = _SQLASession.dispatch.before_flush._clslevel
        for target, fns in list(clslevel.items()):
            for fn in list(fns):
                if fn is not _before_flush_handler:
                    event.remove(target, "before_flush", fn)
    except Exception:
        pass
    install_audit_chain_event()


# =====================================================================
# SQLite test fixture -- mirrors tests/tools/test_arc12_wu2_authorization
# =====================================================================


def _build_sqlite_session():
    """In-memory SQLite session with the minimum schema for the
    authorize/revoke + audit paths.

    Defines admins / instances / users / instance_tool_authorizations
    / admin_audit_logs. The full Base.metadata is Postgres-flavoured
    (INET, JSONB, vector); building a private MetaData lets the
    SQLAlchemy ORM read/write against the in-memory DB without
    pulling in the prod schema dependencies.

    The audit-chain before_flush listener (installed by importing
    app.db.session) issues Postgres-only ``pg_advisory_xact_lock``
    which fails on SQLite. We swap it for a sqlite-friendly stub
    (same approach as ``tests/services/test_arc12_wu4_sibling_grants
    .py``) that just stamps placeholder row_hash / prev_row_hash
    values so the NOT NULL constraint is satisfied. The production
    chain handler is exercised by tests/integrity against Postgres.
    """
    import app.db.session  # noqa: F401 -- installs the prod handler
    from sqlalchemy import event
    from sqlalchemy.orm import Session as _SQLASession

    from app.models.admin_audit_log import AdminAuditLog as _AAL
    from app.repositories.audit_chain import _before_flush_handler

    if event.contains(_SQLASession, "before_flush", _before_flush_handler):
        event.remove(_SQLASession, "before_flush", _before_flush_handler)

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
        Boolean,
        Column,
        DateTime,
        ForeignKey,
        Integer,
        JSON,
        MetaData,
        String,
        Table,
        Text,
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
        Column("tier", String(16), nullable=False, server_default="free"),
        Column(
            "tier_source", String(32), nullable=False, server_default="manual"
        ),
        Column("active", Boolean, nullable=False, server_default="1"),
        Column(
            "created_at", DateTime(timezone=True),
            nullable=False, server_default=func.now(),
        ),
        Column(
            "updated_at", DateTime(timezone=True),
            nullable=False, server_default=func.now(),
        ),
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
    # Arc 15 WU4 — the tools GET route now reads live connection statuses
    # per instance to drive the ToolView connection chip. Mirror the
    # instance_connections table shape (enums rendered as TEXT) so the
    # route's lookup resolves against this SQLite session.
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
        Column("config_json", JSON, nullable=True),
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
        # rescand_connections_schema additions (§3.8.2):
        Column("status_detail", String, nullable=True),
        Column("created_by_user_id", String(36), nullable=True),
    )
    # Minimal admin_audit_logs schema -- the AdminAuditRepository
    # writes these columns through the ORM. The full prod table has
    # row_hash + prev_row_hash + many more columns; the SQLite mirror
    # carries just what record() materialises so the audit write path
    # is exercised end-to-end.
    Table(
        "admin_audit_logs",
        md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("actor_key_prefix", String(64), nullable=True),
        Column("actor_permissions", Text, nullable=True),
        Column("actor_label", String(255), nullable=True),
        Column("admin_id", String(100), nullable=False),
        Column("luciel_instance_id", Integer, nullable=True),
        Column("action", String(64), nullable=False),
        Column("resource_type", String(64), nullable=False),
        Column("resource_pk", Integer, nullable=True),
        Column("resource_natural_id", String(255), nullable=True),
        Column("before_json", JSON, nullable=True),
        Column("after_json", JSON, nullable=True),
        Column("note", Text, nullable=True),
        Column(
            "created_at", DateTime(timezone=True),
            nullable=False, server_default=func.now(),
        ),
        Column(
            "updated_at", DateTime(timezone=True),
            nullable=False, server_default=func.now(),
        ),
        # The hash-chain columns are written by an event handler in
        # prod; on SQLite we leave them nullable / unwritten. The
        # tests only assert that an audit row exists.
        Column("row_hash", String(128), nullable=True),
        Column("prev_row_hash", String(128), nullable=True),
        Column("tier_at_write", String(32), nullable=True),
        Column("cold_archived_at", DateTime(timezone=True), nullable=True),
    )

    md.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return Session()


def _seed_admin_instance_user(
    session,
    *,
    admin_id: str,
    instance_id: int,
    user_id: uuid.UUID,
    tier: str = "pro",
) -> None:
    from sqlalchemy import text as sa_text

    session.execute(
        sa_text(
            "INSERT INTO admins (id, name, tier, tier_source, active) "
            "VALUES (:id, :name, :tier, 'manual', 1)"
        ),
        {"id": admin_id, "name": f"admin-{admin_id}", "tier": tier},
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


def _fake_request(
    *,
    admin_id: str,
    actor_user_id: uuid.UUID | None = None,
    role: str | None = None,
    permissions: list[str] | None = None,
    luciel_instance_id: int | None = None,
):
    request = MagicMock()
    request.state.admin_id = admin_id
    request.state.actor_user_id = actor_user_id
    request.state.role = role
    request.state.permissions = permissions or []
    request.state.scope_assignments = None
    request.state.luciel_instance_id = luciel_instance_id
    request.state.key_prefix = None
    request.state.actor_label = "test-actor"
    return request


def _fake_instance(*, instance_id: int, admin_id: str, active: bool = True):
    inst = MagicMock()
    inst.id = instance_id
    inst.admin_id = admin_id
    inst.active = active
    inst.instance_slug = f"inst-{instance_id}"
    return inst


def _fake_instance_service(instance):
    svc = MagicMock()
    svc.get_by_pk.return_value = instance
    return svc


def _audit_ctx(admin_id: str):
    from app.repositories.admin_audit_repository import AuditContext

    return AuditContext(
        actor_key_prefix=None,
        actor_permissions=("admin_owner",),
        actor_label="test-actor",
        actor_tenant_id=admin_id,
    )


# =====================================================================
# 1. Routes register at the right paths
# =====================================================================


def test_admin_tools_routes_registered() -> None:
    from app.api.v1 import admin_tools

    paths = {
        (r.path, tuple(sorted(r.methods)))
        for r in admin_tools.router.routes
    }
    assert (
        "/admin/instances/{instance_id}/tools", ("GET",)
    ) in paths
    assert (
        "/admin/instances/{instance_id}/tools/{tool_id}/authorize",
        ("POST",),
    ) in paths
    assert (
        "/admin/instances/{instance_id}/tools/{tool_id}/revoke",
        ("POST",),
    ) in paths


def test_admin_tools_router_mounted_in_api_router() -> None:
    """Mounted in app.api.router.api_router so /api/v1/admin/instances/...
    surfaces."""
    from app.api.router import api_router

    paths = {r.path for r in api_router.routes}
    assert "/admin/instances/{instance_id}/tools" in paths


# =====================================================================
# 2. Role gate sets -- Wall-2
# =====================================================================


def test_toggle_role_set_excludes_operator_and_viewer() -> None:
    """Tool toggle is admin-level configuration. read_only_viewer and
    instance_operator must NOT be in the toggle set. owner + manager
    only."""
    from app.api.v1.admin_tools import _TOGGLE_ROLES
    from app.policy.scope import (
        ROLE_ADMIN_MANAGER,
        ROLE_ADMIN_OWNER,
        ROLE_INSTANCE_OPERATOR,
        ROLE_READ_ONLY_VIEWER,
    )

    assert ROLE_INSTANCE_OPERATOR not in _TOGGLE_ROLES
    assert ROLE_READ_ONLY_VIEWER not in _TOGGLE_ROLES
    assert _TOGGLE_ROLES == frozenset(
        {ROLE_ADMIN_OWNER, ROLE_ADMIN_MANAGER}
    )


def test_read_role_set_allows_full_four_role_matrix() -> None:
    """GET is read-only; the full four-role matrix can read. Operator
    is constrained by enforce_role_on_instance to their bound
    Instance; that scoping happens inside ScopePolicy, not in the
    allowed-role frozenset here."""
    from app.api.v1.admin_tools import _READ_ROLES
    from app.policy.scope import ALL_KNOWLEDGE_ROLES

    assert _READ_ROLES == ALL_KNOWLEDGE_ROLES


# =====================================================================
# 3. GET returns all 8 v1 tools, NO cognition tools
# =====================================================================


def test_get_returns_8_v1_catalog_tools_no_cognition() -> None:
    """Cognition behaviours (escalate / save_memory / session_summary)
    are NOT registered per Decision #20. The GET response must
    therefore contain exactly the 8 v1 catalog tools and zero
    cognition rows."""
    from app.api.v1.admin_tools import list_tools_for_instance

    admin_id = "admin-pro-1"
    instance_id = 501
    user_id = uuid.uuid4()
    session = _build_sqlite_session()
    _seed_admin_instance_user(
        session, admin_id=admin_id, instance_id=instance_id,
        user_id=user_id, tier="pro",
    )

    request = _fake_request(
        admin_id=admin_id, actor_user_id=user_id, role="admin_owner",
    )
    instance = _fake_instance(instance_id=instance_id, admin_id=admin_id)
    instance_service = _fake_instance_service(instance)

    response = list_tools_for_instance(
        request=request,
        instance_id=instance_id,
        db=session,
        instance_service=instance_service,
    )

    assert response.instance_id == instance_id
    assert response.admin_id == admin_id
    assert response.admin_tier == "pro"

    tool_ids = {t.tool_id for t in response.tools}
    # Exactly the 8 v1 catalog tools (WU3 §3.3.2).
    expected = {
        "book_appointment",
        "send_email",
        "send_sms",
        "lookup_record",
        "schedule_callback",
        "push_to_crm",
        "call_sibling_luciel",
        "bring_your_own_webhook",
    }
    assert tool_ids == expected, (
        f"Expected exactly the 8 v1 catalog tools; got {tool_ids}"
    )
    # Cognition behaviours are explicitly absent.
    for cognition_name in ("escalate", "save_memory", "session_summary"):
        assert cognition_name not in tool_ids


def test_get_response_carries_contract_fields_and_availability() -> None:
    """Each tool entry must carry the §3.3.1 contract fields the UI
    needs (tool_id, display_name, description, requires_tier,
    requires_channels, execution_mode) PLUS the per-instance
    authorization state (False here -- no rows seeded) AND the
    tier/channel availability flags."""
    from app.api.v1.admin_tools import list_tools_for_instance

    admin_id = "admin-free-1"
    instance_id = 601
    user_id = uuid.uuid4()
    session = _build_sqlite_session()
    _seed_admin_instance_user(
        session, admin_id=admin_id, instance_id=instance_id,
        user_id=user_id, tier="free",
    )

    request = _fake_request(
        admin_id=admin_id, actor_user_id=user_id, role="admin_owner",
    )
    instance = _fake_instance(instance_id=instance_id, admin_id=admin_id)
    instance_service = _fake_instance_service(instance)

    response = list_tools_for_instance(
        request=request, instance_id=instance_id,
        db=session, instance_service=instance_service,
    )

    by_id = {t.tool_id for t in response.tools}
    assert by_id  # at least one tool

    for tool in response.tools:
        assert isinstance(tool.tool_id, str) and tool.tool_id
        assert isinstance(tool.display_name, str) and tool.display_name
        assert isinstance(tool.description, str)
        assert isinstance(tool.requires_tier, list)
        assert all(
            t in {"free", "pro", "enterprise"} for t in tool.requires_tier
        )
        assert isinstance(tool.requires_channels, list)
        assert tool.execution_mode in {"in_process", "subprocess"}
        # No rows seeded -> all unauthorized.
        assert tool.authorized is False
        assert tool.authorization_id is None
        assert tool.authorized_at is None
        assert tool.authorized_by_user_id is None

    # On a Free admin, tools whose requires_tier excludes 'free' must
    # surface tier_available=False so the UI can grey them out.
    # call_sibling_luciel is pro/enterprise-only per WU3.
    sibling = next(
        t for t in response.tools if t.tool_id == "call_sibling_luciel"
    )
    assert sibling.tier_available is False
    assert "free" not in sibling.requires_tier

    # send_email and send_sms declare requires_channels={"email"} /
    # {"sms"}; channel adapters land in Arc 13 so channels_available
    # must be False for these on every Instance until then.
    send_email = next(
        t for t in response.tools if t.tool_id == "send_email"
    )
    assert "email" in send_email.requires_channels
    assert send_email.channels_available is False

    send_sms = next(
        t for t in response.tools if t.tool_id == "send_sms"
    )
    assert "sms" in send_sms.requires_channels
    assert send_sms.channels_available is False

    # A tool with no channel requirement must surface channels_available=True.
    book = next(
        t for t in response.tools if t.tool_id == "book_appointment"
    )
    assert book.requires_channels == []
    assert book.channels_available is True


def test_get_surfaces_live_authorization_row() -> None:
    """When a live row exists for (admin_id, instance_id, tool_id),
    the GET response must surface authorized=True + the row id +
    timestamps."""
    from app.api.v1.admin_tools import list_tools_for_instance
    from app.services.instance_tool_authorization_service import (
        InstanceToolAuthorizationService,
    )

    admin_id = "admin-pro-2"
    instance_id = 701
    user_id = uuid.uuid4()
    session = _build_sqlite_session()
    _seed_admin_instance_user(
        session, admin_id=admin_id, instance_id=instance_id,
        user_id=user_id, tier="pro",
    )

    # Authorise book_appointment directly via the service (bypassing
    # the route) so we can observe the GET surfaces the live row.
    svc = InstanceToolAuthorizationService(session)
    svc.authorize(
        admin_id=admin_id, instance_id=instance_id,
        tool_id="book_appointment",
        authorized_by_user_id=user_id,
    )

    request = _fake_request(
        admin_id=admin_id, actor_user_id=user_id, role="admin_owner",
    )
    instance = _fake_instance(instance_id=instance_id, admin_id=admin_id)
    instance_service = _fake_instance_service(instance)

    response = list_tools_for_instance(
        request=request, instance_id=instance_id,
        db=session, instance_service=instance_service,
    )

    book = next(
        t for t in response.tools if t.tool_id == "book_appointment"
    )
    assert book.authorized is True
    assert book.authorization_id is not None
    assert book.authorized_at is not None
    assert book.authorized_by_user_id == str(user_id)


# =====================================================================
# 4. Authorize creates row + audit
# =====================================================================


def test_authorize_creates_row_and_emits_audit() -> None:
    from sqlalchemy import select, text as sa_text

    from app.api.v1.admin_tools import authorize_tool_on_instance
    from app.models.admin_audit_log import (
        ACTION_TOOL_AUTHORIZED,
        RESOURCE_INSTANCE_TOOL_AUTHORIZATION,
    )
    from app.models.instance_tool_authorization import (
        InstanceToolAuthorization,
    )

    admin_id = "admin-pro-3"
    instance_id = 801
    user_id = uuid.uuid4()
    session = _build_sqlite_session()
    _seed_admin_instance_user(
        session, admin_id=admin_id, instance_id=instance_id,
        user_id=user_id, tier="pro",
    )

    request = _fake_request(
        admin_id=admin_id, actor_user_id=user_id, role="admin_owner",
    )
    instance = _fake_instance(instance_id=instance_id, admin_id=admin_id)
    instance_service = _fake_instance_service(instance)

    response = authorize_tool_on_instance(
        request=request,
        instance_id=instance_id,
        tool_id="book_appointment",
        db=session,
        instance_service=instance_service,
        audit_ctx=_audit_ctx(admin_id),
    )

    assert response.tool_id == "book_appointment"
    assert response.enabled is True
    assert response.revoked_at is None
    assert response.admin_id == admin_id
    assert response.instance_id == instance_id

    # Row materialised.
    rows = session.execute(
        select(InstanceToolAuthorization).where(
            InstanceToolAuthorization.admin_id == admin_id,
            InstanceToolAuthorization.instance_id == instance_id,
            InstanceToolAuthorization.tool_id == "book_appointment",
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].enabled is True
    assert rows[0].revoked_at is None

    # Audit row written in the same transaction.
    audit_rows = session.execute(
        sa_text(
            "SELECT action, resource_type, resource_pk, resource_natural_id "
            "FROM admin_audit_logs "
            "WHERE admin_id = :admin_id"
        ),
        {"admin_id": admin_id},
    ).all()
    assert len(audit_rows) == 1
    action, resource_type, resource_pk, natural_id = audit_rows[0]
    assert action == ACTION_TOOL_AUTHORIZED
    assert resource_type == RESOURCE_INSTANCE_TOOL_AUTHORIZATION
    assert resource_pk == rows[0].id
    assert natural_id == f"{instance_id}:book_appointment"


def test_authorize_is_idempotent_no_duplicate_audit() -> None:
    """A second authorize call against an already-live row returns the
    existing row WITHOUT emitting a second audit entry -- the original
    ACTION_TOOL_AUTHORIZED is already in the chain."""
    from sqlalchemy import text as sa_text

    from app.api.v1.admin_tools import authorize_tool_on_instance

    admin_id = "admin-pro-idem"
    instance_id = 802
    user_id = uuid.uuid4()
    session = _build_sqlite_session()
    _seed_admin_instance_user(
        session, admin_id=admin_id, instance_id=instance_id,
        user_id=user_id, tier="pro",
    )

    request = _fake_request(
        admin_id=admin_id, actor_user_id=user_id, role="admin_owner",
    )
    instance = _fake_instance(instance_id=instance_id, admin_id=admin_id)
    instance_service = _fake_instance_service(instance)

    first = authorize_tool_on_instance(
        request=request,
        instance_id=instance_id,
        tool_id="book_appointment",
        db=session,
        instance_service=instance_service,
        audit_ctx=_audit_ctx(admin_id),
    )
    second = authorize_tool_on_instance(
        request=request,
        instance_id=instance_id,
        tool_id="book_appointment",
        db=session,
        instance_service=instance_service,
        audit_ctx=_audit_ctx(admin_id),
    )

    assert first.authorization_id == second.authorization_id
    audit_count = session.execute(
        sa_text(
            "SELECT count(*) FROM admin_audit_logs WHERE admin_id = :a"
        ),
        {"a": admin_id},
    ).scalar_one()
    assert audit_count == 1


# =====================================================================
# 5. Revoke soft-deletes + audit
# =====================================================================


def test_revoke_soft_deletes_and_emits_audit() -> None:
    from sqlalchemy import select, text as sa_text

    from app.api.v1.admin_tools import (
        authorize_tool_on_instance,
        revoke_tool_on_instance,
    )
    from app.models.admin_audit_log import (
        ACTION_TOOL_AUTHORIZED,
        ACTION_TOOL_REVOKED,
    )
    from app.models.instance_tool_authorization import (
        InstanceToolAuthorization,
    )

    admin_id = "admin-pro-revoke"
    instance_id = 901
    user_id = uuid.uuid4()
    session = _build_sqlite_session()
    _seed_admin_instance_user(
        session, admin_id=admin_id, instance_id=instance_id,
        user_id=user_id, tier="pro",
    )

    request = _fake_request(
        admin_id=admin_id, actor_user_id=user_id, role="admin_owner",
    )
    instance = _fake_instance(instance_id=instance_id, admin_id=admin_id)
    instance_service = _fake_instance_service(instance)

    authorize_tool_on_instance(
        request=request,
        instance_id=instance_id,
        tool_id="book_appointment",
        db=session,
        instance_service=instance_service,
        audit_ctx=_audit_ctx(admin_id),
    )

    response = revoke_tool_on_instance(
        request=request,
        instance_id=instance_id,
        tool_id="book_appointment",
        db=session,
        instance_service=instance_service,
        audit_ctx=_audit_ctx(admin_id),
    )

    assert response.revoked_at is not None

    # Row soft-deleted (revoked_at set, row still exists).
    rows = session.execute(
        select(InstanceToolAuthorization).where(
            InstanceToolAuthorization.tool_id == "book_appointment",
            InstanceToolAuthorization.instance_id == instance_id,
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].revoked_at is not None

    # Two audit rows: one authorize, one revoke.
    actions = session.execute(
        sa_text(
            "SELECT action FROM admin_audit_logs "
            "WHERE admin_id = :a ORDER BY id"
        ),
        {"a": admin_id},
    ).scalars().all()
    assert actions == [ACTION_TOOL_AUTHORIZED, ACTION_TOOL_REVOKED]


def test_revoke_without_live_row_returns_404() -> None:
    """No live row -> 404 so the operator notices the no-op (stale UI
    state, double-click, etc.). No audit emission."""
    from fastapi import HTTPException

    from app.api.v1.admin_tools import revoke_tool_on_instance

    admin_id = "admin-pro-404"
    instance_id = 902
    user_id = uuid.uuid4()
    session = _build_sqlite_session()
    _seed_admin_instance_user(
        session, admin_id=admin_id, instance_id=instance_id,
        user_id=user_id, tier="pro",
    )

    request = _fake_request(
        admin_id=admin_id, actor_user_id=user_id, role="admin_owner",
    )
    instance = _fake_instance(instance_id=instance_id, admin_id=admin_id)
    instance_service = _fake_instance_service(instance)

    with pytest.raises(HTTPException) as exc:
        revoke_tool_on_instance(
            request=request,
            instance_id=instance_id,
            tool_id="book_appointment",
            db=session,
            instance_service=instance_service,
            audit_ctx=_audit_ctx(admin_id),
        )
    assert exc.value.status_code == 404


# =====================================================================
# 6. Tier-locked tool authorize is rejected
# =====================================================================


def test_authorize_rejects_when_tier_excludes_tool() -> None:
    """A Free admin attempting to authorise a Pro/Enterprise-only
    tool gets 403. The error mentions the tier so the operator can
    see why."""
    from fastapi import HTTPException

    from app.api.v1.admin_tools import authorize_tool_on_instance

    admin_id = "admin-free-locked"
    instance_id = 1001
    user_id = uuid.uuid4()
    session = _build_sqlite_session()
    _seed_admin_instance_user(
        session, admin_id=admin_id, instance_id=instance_id,
        user_id=user_id, tier="free",
    )

    request = _fake_request(
        admin_id=admin_id, actor_user_id=user_id, role="admin_owner",
    )
    instance = _fake_instance(instance_id=instance_id, admin_id=admin_id)
    instance_service = _fake_instance_service(instance)

    # call_sibling_luciel is pro/enterprise-only per WU3.
    with pytest.raises(HTTPException) as exc:
        authorize_tool_on_instance(
            request=request,
            instance_id=instance_id,
            tool_id="call_sibling_luciel",
            db=session,
            instance_service=instance_service,
            audit_ctx=_audit_ctx(admin_id),
        )
    assert exc.value.status_code == 403
    assert "free" in exc.value.detail.lower()


def test_authorize_404_for_unknown_tool_id() -> None:
    """Cognition behaviours and typos both yield 404 -- only the 8
    registered v1 catalog tools are toggleable."""
    from fastapi import HTTPException

    from app.api.v1.admin_tools import authorize_tool_on_instance

    admin_id = "admin-pro-unknown"
    instance_id = 1101
    user_id = uuid.uuid4()
    session = _build_sqlite_session()
    _seed_admin_instance_user(
        session, admin_id=admin_id, instance_id=instance_id,
        user_id=user_id, tier="pro",
    )

    request = _fake_request(
        admin_id=admin_id, actor_user_id=user_id, role="admin_owner",
    )
    instance = _fake_instance(instance_id=instance_id, admin_id=admin_id)
    instance_service = _fake_instance_service(instance)

    for bad_tool_id in ("escalate", "save_memory", "session_summary", "nope"):
        with pytest.raises(HTTPException) as exc:
            authorize_tool_on_instance(
                request=request,
                instance_id=instance_id,
                tool_id=bad_tool_id,
                db=session,
                instance_service=instance_service,
                audit_ctx=_audit_ctx(admin_id),
            )
        assert exc.value.status_code == 404, (
            f"unknown tool_id {bad_tool_id!r} should yield 404, "
            f"got {exc.value.status_code}"
        )


# =====================================================================
# 7. Role gating (Wall-2)
# =====================================================================


def test_read_only_viewer_cannot_authorize() -> None:
    """A read_only_viewer (the most restricted of the four scope
    roles) cannot toggle tool authorization."""
    from fastapi import HTTPException

    from app.api.v1.admin_tools import authorize_tool_on_instance

    admin_id = "admin-pro-viewer"
    instance_id = 1201
    user_id = uuid.uuid4()
    session = _build_sqlite_session()
    _seed_admin_instance_user(
        session, admin_id=admin_id, instance_id=instance_id,
        user_id=user_id, tier="pro",
    )

    request = _fake_request(
        admin_id=admin_id,
        actor_user_id=user_id,
        role="read_only_viewer",
    )
    instance = _fake_instance(instance_id=instance_id, admin_id=admin_id)
    instance_service = _fake_instance_service(instance)

    with pytest.raises(HTTPException) as exc:
        authorize_tool_on_instance(
            request=request,
            instance_id=instance_id,
            tool_id="book_appointment",
            db=session,
            instance_service=instance_service,
            audit_ctx=_audit_ctx(admin_id),
        )
    assert exc.value.status_code == 403


def test_instance_operator_cannot_authorize() -> None:
    """instance_operator is excluded from the toggle set per the
    §3.2.2-analogue policy: operator is list/view-scoped on Knowledge
    and the analogous gate here is "read tool state, cannot toggle"."""
    from fastapi import HTTPException

    from app.api.v1.admin_tools import authorize_tool_on_instance

    admin_id = "admin-pro-operator"
    instance_id = 1301
    user_id = uuid.uuid4()
    session = _build_sqlite_session()
    _seed_admin_instance_user(
        session, admin_id=admin_id, instance_id=instance_id,
        user_id=user_id, tier="pro",
    )

    request = _fake_request(
        admin_id=admin_id,
        actor_user_id=user_id,
        role="instance_operator",
        luciel_instance_id=instance_id,  # bound to this instance
    )
    instance = _fake_instance(instance_id=instance_id, admin_id=admin_id)
    instance_service = _fake_instance_service(instance)

    with pytest.raises(HTTPException) as exc:
        authorize_tool_on_instance(
            request=request,
            instance_id=instance_id,
            tool_id="book_appointment",
            db=session,
            instance_service=instance_service,
            audit_ctx=_audit_ctx(admin_id),
        )
    assert exc.value.status_code == 403


def test_admin_manager_can_authorize() -> None:
    """admin_manager is in the toggle set alongside admin_owner."""
    from app.api.v1.admin_tools import authorize_tool_on_instance

    admin_id = "admin-pro-mgr"
    instance_id = 1401
    user_id = uuid.uuid4()
    session = _build_sqlite_session()
    _seed_admin_instance_user(
        session, admin_id=admin_id, instance_id=instance_id,
        user_id=user_id, tier="pro",
    )

    request = _fake_request(
        admin_id=admin_id,
        actor_user_id=user_id,
        role="admin_manager",
    )
    instance = _fake_instance(instance_id=instance_id, admin_id=admin_id)
    instance_service = _fake_instance_service(instance)

    response = authorize_tool_on_instance(
        request=request,
        instance_id=instance_id,
        tool_id="book_appointment",
        db=session,
        instance_service=instance_service,
        audit_ctx=_audit_ctx(admin_id),
    )
    assert response.enabled is True


# =====================================================================
# 8. Wall-1: cross-Admin toggle is blocked
# =====================================================================


def test_wall_1_cannot_toggle_other_admins_instance() -> None:
    """A caller scoped to Admin A cannot toggle tools on an Instance
    owned by Admin B -- enforce_admin_owns_instance fires inside
    _load_active_instance."""
    from fastapi import HTTPException

    from app.api.v1.admin_tools import authorize_tool_on_instance

    user_id = uuid.uuid4()
    session = _build_sqlite_session()
    _seed_admin_instance_user(
        session, admin_id="admin-a", instance_id=1501,
        user_id=user_id, tier="pro",
    )

    # The caller's admin_id is admin-A but the Instance is owned by
    # admin-B -- enforce_admin_owns_instance must 403.
    request = _fake_request(
        admin_id="admin-a", actor_user_id=user_id, role="admin_owner",
    )
    other_instance = _fake_instance(
        instance_id=1501, admin_id="admin-b",
    )
    instance_service = _fake_instance_service(other_instance)

    with pytest.raises(HTTPException) as exc:
        authorize_tool_on_instance(
            request=request,
            instance_id=1501,
            tool_id="book_appointment",
            db=session,
            instance_service=instance_service,
            audit_ctx=_audit_ctx("admin-a"),
        )
    assert exc.value.status_code == 403
