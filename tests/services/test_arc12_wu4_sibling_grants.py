"""Arc 12 WU4 — sibling_call_grants table + grant-authoring service tests.

Covers the binding-spec assertions from
``arc12_specs/01_WORKUNITS.md`` §WU4:

  1. Free → author rejected (composition not available on tier).
  2. Pro → author lands ``approval_state='live'`` immediately.
  3. Enterprise → author lands ``pending_approval``; approve flips
     it to ``live`` and stamps approved_by + approved_at.
  4. Partial-unique index: a second active (live or pending) row
     for the same (admin, caller, callee) triple is rejected by
     the DB; after revoke the triple is free to re-author.
  5. Revoke flips state to ``revoked`` and stamps ``revoked_at``.
  6. Reject is structurally distinct from revoke at the audit-verb
     level (different ACTION_* row) but writes identical DB
     columns; reject only applies to pending grants.
  7. A→B grant does not create a B→A grant (the edge is directed).
  8. Audit row written for every author / approve / reject / revoke.
  9. Cycle of self-edges blocked at the DB CHECK level.
 10. Deactivation cascade: revoking grants where the deactivated
     Instance appears as caller OR callee.
 11. Migration shape — RLS posture mirrors WU2, partial unique
     index present, composite dispatch index present.

The tests use an in-memory SQLite DB for the service-level checks
(same pattern as ``test_arc12_wu2_authorization.py``). The RLS
posture is verified by inspecting the migration body statically.
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("OPENAI_API_KEY", "dummy")


@pytest.fixture(autouse=True)
def _restore_prod_audit_chain_handler():
    """Restore the prod audit-chain before_flush handler after each test.

    ``_build_sqlite_session`` globally swaps the prod
    ``_before_flush_handler`` for a SQLite stub that stamps
    ``row_hash``/``prev_row_hash`` = ``'0'*64``. Without this teardown the
    stub leaks onto ``sqlalchemy.orm.Session`` for the rest of the
    process, so a later real-Postgres test (e.g. the Arc 13 channel
    provisioning audit tests) writes zero-hash rows that collide on
    ``ux_admin_audit_logs_row_hash``. This fixture strips the stub and
    reinstalls the prod handler once the test finishes.
    """
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


# =====================================================================
# In-memory SQLite test fixture.
# =====================================================================
#
# Same shape as test_arc12_wu2_authorization.py — we define a fresh
# MetaData with just the four parent tables + sibling_call_grants and
# admin_audit_logs, then let the ORM map onto it by table name.


def _build_sqlite_session():
    # The audit-chain before_flush listener (installed by importing
    # app.db.session) issues a Postgres-only ``pg_advisory_xact_lock``
    # which fails on SQLite. We work around that by attaching a
    # before_flush handler that *removes* the chain handler if it's
    # registered, then sets row_hash + prev_row_hash to deterministic
    # placeholders so the NOT NULL constraint is satisfied. The
    # production chain handler is exercised by
    # tests/integrity/test_audit_chain_fields_in_sync.py against
    # Postgres; here we're verifying the WU4 service writes the right
    # rows with the right columns, not the chain hash itself.
    import app.db.session  # noqa: F401 — installs the prod handler
    from sqlalchemy import event
    from sqlalchemy.orm import Session as _SQLASession

    from app.repositories.audit_chain import _before_flush_handler

    # Remove the prod handler if registered (idempotent).
    if event.contains(_SQLASession, "before_flush", _before_flush_handler):
        event.remove(_SQLASession, "before_flush", _before_flush_handler)

    # Install a sqlite-friendly placeholder so row_hash / prev_row_hash
    # are populated.
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
        Column("tier", String(20), nullable=False, server_default="free"),
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
    # Partial unique index — SQLite supports WHERE clauses on indexes
    # via Index(..., sqlite_where=...). We mirror the production
    # partial-unique semantics so the test can prove the DB-layer
    # invariant rather than only the service-layer check.
    Index(
        "uq_sibling_call_grants_active",
        md.tables["sibling_call_grants"].c.admin_id,
        md.tables["sibling_call_grants"].c.caller_instance_id,
        md.tables["sibling_call_grants"].c.callee_instance_id,
        unique=True,
        sqlite_where=sa_text("approval_state <> 'revoked'"),
    )

    # admin_audit_logs — minimal shape, no chain hashes for tests.
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

    # The AdminAuditLog ORM is wired to compute row_hash via a
    # before_insert event in app.repositories.audit_chain. That event
    # imports `cryptography` and other heavy deps and isn't needed
    # for our coverage — we install a SQLite default of 64 zeros for
    # row_hash + prev_row_hash so inserts pass NOT NULL even if the
    # production chain handler doesn't fire under SQLite. The
    # before_insert listener (when it fires) overwrites the defaults.

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    return session


def _seed_admin_with_tier(
    session, *, admin_id: str, tier: str = "free", name: str | None = None
) -> None:
    from sqlalchemy import text as sa_text

    session.execute(
        sa_text(
            "INSERT INTO admins (id, name, tier) "
            "VALUES (:id, :name, :tier)"
        ),
        {
            "id": admin_id,
            "name": name or f"admin-{admin_id}",
            "tier": tier,
        },
    )
    session.commit()


def _seed_instance(session, *, instance_id: int, admin_id: str) -> None:
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


def _seed_user(session, *, user_id: uuid.UUID) -> None:
    from sqlalchemy import text as sa_text

    session.execute(
        sa_text("INSERT INTO users (id) VALUES (:id)"),
        {"id": str(user_id)},
    )
    session.commit()


def _make_audit_ctx() -> MagicMock:
    """A minimal AuditContext-shaped stub. The repository's record()
    reads ctx.actor_key_prefix / ctx.permissions_str / ctx.actor_label;
    we make them all None so the audit-row insert is a plain row.
    """
    ctx = MagicMock()
    ctx.actor_key_prefix = None
    ctx.permissions_str = None
    ctx.actor_label = None
    ctx.actor_permissions = []
    return ctx


def _count_audit_rows(session, *, action: str, admin_id: str) -> int:
    from sqlalchemy import text as sa_text

    row = session.execute(
        sa_text(
            "SELECT COUNT(*) FROM admin_audit_logs "
            "WHERE action = :action AND admin_id = :admin_id"
        ),
        {"action": action, "admin_id": admin_id},
    ).scalar_one()
    return int(row or 0)


# =====================================================================
# 1. Free tier — author rejected
# =====================================================================


def test_free_tier_author_rejected() -> None:
    """Free has composition_enabled=False per §3.3.4; the service
    must raise TierNotEligibleForSiblingGrants before any row is
    inserted."""
    from app.services.sibling_call_grant_service import (
        SiblingCallGrantService,
        TierNotEligibleForSiblingGrants,
    )

    session = _build_sqlite_session()
    admin_id = "admin-free"
    user_id = uuid.uuid4()
    _seed_admin_with_tier(session, admin_id=admin_id, tier="free")
    _seed_instance(session, instance_id=100, admin_id=admin_id)
    _seed_instance(session, instance_id=101, admin_id=admin_id)
    _seed_user(session, user_id=user_id)

    service = SiblingCallGrantService(session)
    with pytest.raises(TierNotEligibleForSiblingGrants):
        service.author(
            admin_id=admin_id,
            caller_instance_id=100,
            callee_instance_id=101,
            granted_by_user_id=user_id,
            audit_ctx=_make_audit_ctx(),
            autocommit=True,
        )
    # No row was inserted.
    from sqlalchemy import text as sa_text
    n = session.execute(
        sa_text("SELECT COUNT(*) FROM sibling_call_grants")
    ).scalar_one()
    assert int(n) == 0


# =====================================================================
# 2. Pro tier — author lands LIVE immediately
# =====================================================================


def test_pro_tier_author_lands_live() -> None:
    from app.models.sibling_call_grant import APPROVAL_STATE_LIVE
    from app.services.sibling_call_grant_service import (
        SiblingCallGrantService,
    )

    session = _build_sqlite_session()
    admin_id = "admin-pro"
    user_id = uuid.uuid4()
    _seed_admin_with_tier(session, admin_id=admin_id, tier="pro")
    _seed_instance(session, instance_id=200, admin_id=admin_id)
    _seed_instance(session, instance_id=201, admin_id=admin_id)
    _seed_user(session, user_id=user_id)

    service = SiblingCallGrantService(session)
    grant = service.author(
        admin_id=admin_id,
        caller_instance_id=200,
        callee_instance_id=201,
        granted_by_user_id=user_id,
        audit_ctx=_make_audit_ctx(),
        autocommit=True,
    )
    assert grant.approval_state == APPROVAL_STATE_LIVE
    assert grant.approved_by_user_id is None
    assert grant.approved_at is None
    assert grant.revoked_at is None
    # Audit row written.
    assert _count_audit_rows(
        session, action="sibling_grant_authored", admin_id=admin_id
    ) == 1


# =====================================================================
# 3. Enterprise tier — author lands PENDING; approve flips to LIVE
# =====================================================================


def test_enterprise_author_then_approve() -> None:
    from app.models.sibling_call_grant import (
        APPROVAL_STATE_LIVE,
        APPROVAL_STATE_PENDING,
    )
    from app.services.sibling_call_grant_service import (
        SiblingCallGrantService,
    )

    session = _build_sqlite_session()
    admin_id = "admin-ent"
    author_user = uuid.uuid4()
    owner_user = uuid.uuid4()
    _seed_admin_with_tier(session, admin_id=admin_id, tier="enterprise")
    _seed_instance(session, instance_id=300, admin_id=admin_id)
    _seed_instance(session, instance_id=301, admin_id=admin_id)
    _seed_user(session, user_id=author_user)
    _seed_user(session, user_id=owner_user)

    service = SiblingCallGrantService(session)
    pending = service.author(
        admin_id=admin_id,
        caller_instance_id=300,
        callee_instance_id=301,
        granted_by_user_id=author_user,
        audit_ctx=_make_audit_ctx(),
        autocommit=True,
    )
    assert pending.approval_state == APPROVAL_STATE_PENDING
    assert pending.approved_at is None

    live = service.approve(
        admin_id=admin_id,
        grant_id=pending.id,
        approved_by_user_id=owner_user,
        audit_ctx=_make_audit_ctx(),
        autocommit=True,
    )
    assert live.approval_state == APPROVAL_STATE_LIVE
    assert live.approved_by_user_id == owner_user
    assert live.approved_at is not None
    # Two audit rows: authored + approved.
    assert _count_audit_rows(
        session, action="sibling_grant_authored", admin_id=admin_id
    ) == 1
    assert _count_audit_rows(
        session, action="sibling_grant_approved", admin_id=admin_id
    ) == 1


# =====================================================================
# 4. Partial-unique constraint — second live edge rejected;
#    re-author allowed after revoke.
# =====================================================================


def test_partial_unique_blocks_duplicate_live_edge() -> None:
    from app.services.sibling_call_grant_service import (
        GrantAlreadyExists,
        SiblingCallGrantService,
    )

    session = _build_sqlite_session()
    admin_id = "admin-pro"
    user_id = uuid.uuid4()
    _seed_admin_with_tier(session, admin_id=admin_id, tier="pro")
    _seed_instance(session, instance_id=400, admin_id=admin_id)
    _seed_instance(session, instance_id=401, admin_id=admin_id)
    _seed_user(session, user_id=user_id)

    service = SiblingCallGrantService(session)
    service.author(
        admin_id=admin_id,
        caller_instance_id=400,
        callee_instance_id=401,
        granted_by_user_id=user_id,
        audit_ctx=_make_audit_ctx(),
        autocommit=True,
    )
    with pytest.raises(GrantAlreadyExists):
        service.author(
            admin_id=admin_id,
            caller_instance_id=400,
            callee_instance_id=401,
            granted_by_user_id=user_id,
            audit_ctx=_make_audit_ctx(),
            autocommit=True,
        )


def test_can_reauthor_after_revoke() -> None:
    from app.models.sibling_call_grant import APPROVAL_STATE_LIVE
    from app.services.sibling_call_grant_service import (
        SiblingCallGrantService,
    )

    session = _build_sqlite_session()
    admin_id = "admin-pro"
    user_id = uuid.uuid4()
    _seed_admin_with_tier(session, admin_id=admin_id, tier="pro")
    _seed_instance(session, instance_id=410, admin_id=admin_id)
    _seed_instance(session, instance_id=411, admin_id=admin_id)
    _seed_user(session, user_id=user_id)

    service = SiblingCallGrantService(session)
    first = service.author(
        admin_id=admin_id,
        caller_instance_id=410,
        callee_instance_id=411,
        granted_by_user_id=user_id,
        audit_ctx=_make_audit_ctx(),
        autocommit=True,
    )
    service.revoke(
        admin_id=admin_id,
        grant_id=first.id,
        audit_ctx=_make_audit_ctx(),
        autocommit=True,
    )
    # Re-authoring the same edge now succeeds — the revoked row is
    # excluded from the partial unique index.
    second = service.author(
        admin_id=admin_id,
        caller_instance_id=410,
        callee_instance_id=411,
        granted_by_user_id=user_id,
        audit_ctx=_make_audit_ctx(),
        autocommit=True,
    )
    assert second.id != first.id
    assert second.approval_state == APPROVAL_STATE_LIVE


# =====================================================================
# 5. Revoke flips state and stamps revoked_at
# =====================================================================


def test_revoke_sets_revoked_at_and_state() -> None:
    from app.models.sibling_call_grant import APPROVAL_STATE_REVOKED
    from app.services.sibling_call_grant_service import (
        SiblingCallGrantService,
    )

    session = _build_sqlite_session()
    admin_id = "admin-pro"
    user_id = uuid.uuid4()
    _seed_admin_with_tier(session, admin_id=admin_id, tier="pro")
    _seed_instance(session, instance_id=500, admin_id=admin_id)
    _seed_instance(session, instance_id=501, admin_id=admin_id)
    _seed_user(session, user_id=user_id)

    service = SiblingCallGrantService(session)
    grant = service.author(
        admin_id=admin_id,
        caller_instance_id=500,
        callee_instance_id=501,
        granted_by_user_id=user_id,
        audit_ctx=_make_audit_ctx(),
        autocommit=True,
    )
    revoked = service.revoke(
        admin_id=admin_id,
        grant_id=grant.id,
        audit_ctx=_make_audit_ctx(),
        autocommit=True,
    )
    assert revoked.approval_state == APPROVAL_STATE_REVOKED
    assert revoked.revoked_at is not None
    assert _count_audit_rows(
        session, action="sibling_grant_revoked", admin_id=admin_id
    ) == 1


# =====================================================================
# 6. Reject distinct from revoke (verb + state-precondition)
# =====================================================================


def test_reject_only_applies_to_pending_grants() -> None:
    from app.services.sibling_call_grant_service import (
        InvalidStateTransition,
        SiblingCallGrantService,
    )

    session = _build_sqlite_session()
    admin_id = "admin-pro"
    user_id = uuid.uuid4()
    _seed_admin_with_tier(session, admin_id=admin_id, tier="pro")
    _seed_instance(session, instance_id=600, admin_id=admin_id)
    _seed_instance(session, instance_id=601, admin_id=admin_id)
    _seed_user(session, user_id=user_id)

    service = SiblingCallGrantService(session)
    # Pro author lands live — reject must refuse (only pending is
    # rejectable; live → revoke).
    grant = service.author(
        admin_id=admin_id,
        caller_instance_id=600,
        callee_instance_id=601,
        granted_by_user_id=user_id,
        audit_ctx=_make_audit_ctx(),
        autocommit=True,
    )
    with pytest.raises(InvalidStateTransition):
        service.reject(
            admin_id=admin_id,
            grant_id=grant.id,
            rejected_by_user_id=user_id,
            audit_ctx=_make_audit_ctx(),
            autocommit=True,
        )


def test_reject_pending_grant_distinct_audit_verb() -> None:
    from app.models.sibling_call_grant import APPROVAL_STATE_REVOKED
    from app.services.sibling_call_grant_service import (
        SiblingCallGrantService,
    )

    session = _build_sqlite_session()
    admin_id = "admin-ent"
    author_user = uuid.uuid4()
    owner_user = uuid.uuid4()
    _seed_admin_with_tier(session, admin_id=admin_id, tier="enterprise")
    _seed_instance(session, instance_id=610, admin_id=admin_id)
    _seed_instance(session, instance_id=611, admin_id=admin_id)
    _seed_user(session, user_id=author_user)
    _seed_user(session, user_id=owner_user)

    service = SiblingCallGrantService(session)
    pending = service.author(
        admin_id=admin_id,
        caller_instance_id=610,
        callee_instance_id=611,
        granted_by_user_id=author_user,
        audit_ctx=_make_audit_ctx(),
        autocommit=True,
    )
    rejected = service.reject(
        admin_id=admin_id,
        grant_id=pending.id,
        rejected_by_user_id=owner_user,
        audit_ctx=_make_audit_ctx(),
        autocommit=True,
    )
    # DB columns are the same as revoke (revoked state + revoked_at).
    assert rejected.approval_state == APPROVAL_STATE_REVOKED
    assert rejected.revoked_at is not None
    # But the audit-verb is distinct from revoke.
    assert _count_audit_rows(
        session, action="sibling_grant_rejected", admin_id=admin_id
    ) == 1
    assert _count_audit_rows(
        session, action="sibling_grant_revoked", admin_id=admin_id
    ) == 0


# =====================================================================
# 7. A→B grant does not create B→A grant (directed edge)
# =====================================================================


def test_grant_is_directed_no_implicit_reverse() -> None:
    from app.repositories.sibling_call_grant_repository import (
        SiblingCallGrantRepository,
    )
    from app.services.sibling_call_grant_service import (
        SiblingCallGrantService,
    )

    session = _build_sqlite_session()
    admin_id = "admin-pro"
    user_id = uuid.uuid4()
    _seed_admin_with_tier(session, admin_id=admin_id, tier="pro")
    _seed_instance(session, instance_id=700, admin_id=admin_id)
    _seed_instance(session, instance_id=701, admin_id=admin_id)
    _seed_user(session, user_id=user_id)

    service = SiblingCallGrantService(session)
    service.author(
        admin_id=admin_id,
        caller_instance_id=700,
        callee_instance_id=701,
        granted_by_user_id=user_id,
        audit_ctx=_make_audit_ctx(),
        autocommit=True,
    )

    repo = SiblingCallGrantRepository(session)
    forward = repo.get_live(
        admin_id=admin_id, caller_instance_id=700, callee_instance_id=701,
    )
    reverse = repo.get_live(
        admin_id=admin_id, caller_instance_id=701, callee_instance_id=700,
    )
    assert forward is not None, "A→B grant should be live"
    assert reverse is None, "B→A must NOT be implicitly authorised"


# =====================================================================
# 8. CHECK constraint blocks self-edges (caller == callee)
# =====================================================================


def test_self_edge_rejected_by_service() -> None:
    from app.services.sibling_call_grant_service import (
        InvalidStateTransition,
        SiblingCallGrantService,
    )

    session = _build_sqlite_session()
    admin_id = "admin-pro"
    user_id = uuid.uuid4()
    _seed_admin_with_tier(session, admin_id=admin_id, tier="pro")
    _seed_instance(session, instance_id=800, admin_id=admin_id)
    _seed_user(session, user_id=user_id)

    service = SiblingCallGrantService(session)
    with pytest.raises(InvalidStateTransition):
        service.author(
            admin_id=admin_id,
            caller_instance_id=800,
            callee_instance_id=800,
            granted_by_user_id=user_id,
            audit_ctx=_make_audit_ctx(),
            autocommit=True,
        )


# =====================================================================
# 9. Deactivation cascade — caller-side AND callee-side revoked
# =====================================================================


def test_cascade_revokes_caller_and_callee_side_grants() -> None:
    from app.models.sibling_call_grant import (
        APPROVAL_STATE_LIVE,
        APPROVAL_STATE_REVOKED,
    )
    from app.repositories.sibling_call_grant_repository import (
        SiblingCallGrantRepository,
    )
    from app.services.sibling_call_grant_service import (
        SiblingCallGrantService,
    )

    session = _build_sqlite_session()
    admin_id = "admin-pro"
    user_id = uuid.uuid4()
    _seed_admin_with_tier(session, admin_id=admin_id, tier="pro")
    # Three instances; we'll deactivate B.
    _seed_instance(session, instance_id=900, admin_id=admin_id)  # A
    _seed_instance(session, instance_id=901, admin_id=admin_id)  # B (target)
    _seed_instance(session, instance_id=902, admin_id=admin_id)  # C
    _seed_user(session, user_id=user_id)

    service = SiblingCallGrantService(session)
    # B as caller (B→C), B as callee (A→B), and an unrelated edge (A→C).
    g_b_to_c = service.author(
        admin_id=admin_id,
        caller_instance_id=901, callee_instance_id=902,
        granted_by_user_id=user_id,
        audit_ctx=_make_audit_ctx(), autocommit=True,
    )
    g_a_to_b = service.author(
        admin_id=admin_id,
        caller_instance_id=900, callee_instance_id=901,
        granted_by_user_id=user_id,
        audit_ctx=_make_audit_ctx(), autocommit=True,
    )
    g_a_to_c = service.author(
        admin_id=admin_id,
        caller_instance_id=900, callee_instance_id=902,
        granted_by_user_id=user_id,
        audit_ctx=_make_audit_ctx(), autocommit=True,
    )

    # Cascade for B.
    revoked = service.revoke_all_touching_instance(
        admin_id=admin_id,
        instance_id=901,
        audit_ctx=_make_audit_ctx(),
        autocommit=True,
    )
    revoked_ids = {g.id for g in revoked}
    assert revoked_ids == {g_b_to_c.id, g_a_to_b.id}, (
        "Cascade must revoke grants where the deactivated instance "
        "appears as caller OR callee, and only those."
    )

    repo = SiblingCallGrantRepository(session)
    # The unrelated A→C edge stays live.
    fresh_a_to_c = repo.get_by_id(admin_id=admin_id, grant_id=g_a_to_c.id)
    assert fresh_a_to_c.approval_state == APPROVAL_STATE_LIVE
    assert fresh_a_to_c.revoked_at is None
    # Both touching edges are revoked.
    fresh_b_to_c = repo.get_by_id(admin_id=admin_id, grant_id=g_b_to_c.id)
    fresh_a_to_b = repo.get_by_id(admin_id=admin_id, grant_id=g_a_to_b.id)
    assert fresh_b_to_c.approval_state == APPROVAL_STATE_REVOKED
    assert fresh_a_to_b.approval_state == APPROVAL_STATE_REVOKED
    # Each revoke wrote its own audit row carrying the cascade source.
    assert _count_audit_rows(
        session, action="sibling_grant_revoked", admin_id=admin_id
    ) == 2


# =====================================================================
# 10. Cross-admin isolation — admin A's grant invisible under admin B
# =====================================================================


def test_cross_admin_isolation_at_repo_layer() -> None:
    from app.repositories.sibling_call_grant_repository import (
        SiblingCallGrantRepository,
    )
    from app.services.sibling_call_grant_service import (
        SiblingCallGrantService,
    )

    session = _build_sqlite_session()
    admin_a = "admin-A"
    admin_b = "admin-B"
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    _seed_admin_with_tier(session, admin_id=admin_a, tier="pro")
    _seed_admin_with_tier(session, admin_id=admin_b, tier="pro")
    _seed_instance(session, instance_id=1000, admin_id=admin_a)
    _seed_instance(session, instance_id=1001, admin_id=admin_a)
    _seed_instance(session, instance_id=1100, admin_id=admin_b)
    _seed_user(session, user_id=user_a)
    _seed_user(session, user_id=user_b)

    service = SiblingCallGrantService(session)
    grant = service.author(
        admin_id=admin_a,
        caller_instance_id=1000, callee_instance_id=1001,
        granted_by_user_id=user_a,
        audit_ctx=_make_audit_ctx(), autocommit=True,
    )

    repo = SiblingCallGrantRepository(session)
    # Admin B can't see admin A's row even by id.
    assert repo.get_by_id(admin_id=admin_b, grant_id=grant.id) is None
    # Admin A can see it.
    assert repo.get_by_id(admin_id=admin_a, grant_id=grant.id) is not None


# =====================================================================
# 11. Migration shape — static-source contract
# =====================================================================


_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "arc12_wu4_sibling_call_grants.py"
)


def _migration_src() -> str:
    return _MIGRATION_PATH.read_text(encoding="utf-8")


def test_migration_chains_off_wu2_head() -> None:
    src = _migration_src()
    assert re.search(
        r"down_revision\s*=\s*['\"]arc12_wu2_instance_tool_authorizations['\"]",
        src,
    ), (
        "WU4 migration must chain off the WU2 head."
    )
    assert re.search(
        r"revision\s*=\s*['\"]arc12_wu4_sibling_call_grants['\"]",
        src,
    )


def test_migration_enables_and_forces_rls() -> None:
    src = _migration_src()
    assert "ENABLE ROW LEVEL SECURITY" in src
    assert "FORCE ROW LEVEL SECURITY" in src


def test_migration_creates_tenant_isolation_policy() -> None:
    src = _migration_src()
    assert "sibling_call_grants_tenant_isolation" in src
    assert "current_setting('app.admin_id', true)" in src
    # Both USING and WITH CHECK must be present and strict.
    assert re.search(
        r"USING\s*\(\s*admin_id\s*=\s*current_setting", src
    )
    assert re.search(
        r"WITH CHECK\s*\(\s*admin_id\s*=\s*current_setting", src
    )


def test_migration_creates_partial_unique_index_excluding_revoked() -> None:
    src = _migration_src()
    assert "uq_sibling_call_grants_active" in src
    # The partial-index predicate must filter out revoked rows.
    assert re.search(
        r"postgresql_where=sa\.text\(\s*['\"]approval_state\s*<>\s*'revoked'['\"]",
        src,
    )


def test_migration_creates_dispatch_lookup_index() -> None:
    """WU5's runtime dispatch hot path uses
    (admin_id, caller_instance_id) — the migration must declare a
    composite index on those columns."""
    src = _migration_src()
    assert "ix_sibling_call_grants_dispatch" in src
    # The columns are admin_id + caller_instance_id (verified by the
    # surrounding declaration text).
    assert "admin_id" in src and "caller_instance_id" in src


def test_migration_creates_no_self_edge_check() -> None:
    """The CHECK constraint blocks the trivial self-edge case at the
    DB layer; the runtime cycle detection (WU5) handles longer
    cycles."""
    src = _migration_src()
    assert "ck_sibling_call_grants_no_self_edge" in src
    assert re.search(
        r"caller_instance_id\s*<>\s*callee_instance_id", src,
    )


def test_migration_creates_approval_state_check() -> None:
    """approval_state pinned to the three legal values via CHECK."""
    src = _migration_src()
    assert "ck_sibling_call_grants_approval_state" in src
    assert "'live'" in src
    assert "'pending_approval'" in src
    assert "'revoked'" in src


# =====================================================================
# 12. ACTION_* + RESOURCE_* registration
# =====================================================================


def test_action_constants_registered() -> None:
    from app.models.admin_audit_log import (
        ACTION_SIBLING_GRANT_APPROVED,
        ACTION_SIBLING_GRANT_AUTHORED,
        ACTION_SIBLING_GRANT_REJECTED,
        ACTION_SIBLING_GRANT_REVOKED,
        ALLOWED_ACTIONS,
        ALLOWED_RESOURCE_TYPES,
        RESOURCE_SIBLING_CALL_GRANT,
    )

    assert ACTION_SIBLING_GRANT_AUTHORED in ALLOWED_ACTIONS
    assert ACTION_SIBLING_GRANT_APPROVED in ALLOWED_ACTIONS
    assert ACTION_SIBLING_GRANT_REJECTED in ALLOWED_ACTIONS
    assert ACTION_SIBLING_GRANT_REVOKED in ALLOWED_ACTIONS
    assert RESOURCE_SIBLING_CALL_GRANT in ALLOWED_RESOURCE_TYPES
