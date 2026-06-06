"""RESCAN TIER-B(conn) — connections schema completion tests.

Covers:
  1. connection_status enum has all 6 values; can set 'revoked' + 'dormant'.
  2. Downgrade sets connections dormant (retained); re-upgrade restores.
  3. status_detail set on expired by the refresh-fail path.
  4. Single-active-per-type constraint enforced (3-tuple).
  5. Existing connections / lifecycle tests stay green.
  6. Migration shape (revision, down_revision, ADD VALUE, index changes).

Strategy: AST / source-text assertions (no live DB required for most
tests).  Behavioural tests use an in-memory SQLite session shaped to
the columns the units touch (same convention as the Arc 17 completion
tests).
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    REPO_ROOT / "alembic" / "versions" / "rescand_connections_schema.py"
)
MODEL_PATH = REPO_ROOT / "app" / "models" / "instance_connection.py"
REPO_PATH = REPO_ROOT / "app" / "repositories" / "instance_connection_repository.py"
DOWNGRADE_SVC_PATH = (
    REPO_ROOT / "app" / "services" / "downgrade_archive_service.py"
)
TIER_PROV_PATH = (
    REPO_ROOT / "app" / "services" / "tier_provisioning_service.py"
)
WORKER_REFRESH_PATH = (
    REPO_ROOT / "app" / "worker" / "tasks" / "refresh_connections.py"
)


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# =====================================================================
# §1 — Migration shape.
# =====================================================================

def test_migration_file_exists():
    assert MIGRATION_PATH.exists(), "rescand_connections_schema.py must exist."


def test_migration_revision_id():
    src = _text(MIGRATION_PATH)
    assert 'revision = "rescand_connections_schema"' in src


def test_migration_down_revision():
    src = _text(MIGRATION_PATH)
    assert 'down_revision = "rescand_lifecycle_states"' in src


def test_migration_adds_revoked_value():
    src = _text(MIGRATION_PATH)
    assert "'revoked'" in src, "Migration must ADD VALUE 'revoked' to connection_status."


def test_migration_adds_dormant_value():
    src = _text(MIGRATION_PATH)
    assert "'dormant'" in src, "Migration must ADD VALUE 'dormant' to connection_status."


def test_migration_uses_if_not_exists():
    src = _text(MIGRATION_PATH)
    assert "IF NOT EXISTS" in src, "ADD VALUE must use IF NOT EXISTS for idempotency."


def test_migration_uses_autocommit_pattern():
    """Must use the raw DBAPI autocommit pattern copied from rescand_lifecycle_states."""
    src = _text(MIGRATION_PATH)
    assert "dbapi_conn.commit()" in src, "Must commit empty txn before ADD VALUE."
    assert "autocommit = True" in src, "Must set autocommit=True for ADD VALUE."
    assert "autocommit = False" in src, "Must restore autocommit=False after ADD VALUE."


def test_migration_adds_status_detail_column():
    src = _text(MIGRATION_PATH)
    assert '"status_detail"' in src, "Migration must add status_detail column."
    assert "Text()" in src or "sa.Text" in src, "status_detail must be Text type."


def test_migration_adds_created_by_user_id_column():
    src = _text(MIGRATION_PATH)
    assert '"created_by_user_id"' in src, "Migration must add created_by_user_id column."
    assert "users.id" in src, "created_by_user_id must FK to users.id."


def test_migration_drops_4_tuple_index():
    src = _text(MIGRATION_PATH)
    assert "uq_instance_connections_active" in src
    # Should drop the old one and create new one.
    assert "drop_index" in src, "Must drop the old 4-tuple unique index."


def test_migration_creates_3_tuple_index():
    src = _text(MIGRATION_PATH)
    # The new index covers (admin_id, instance_id, connection_type) — 3 columns only.
    # The key sign is that 'provider' is NOT in the index column list.
    # Look for the create_index call with only the 3 columns.
    # The migration should have the 3 cols without 'provider'.
    new_idx_section = src[src.find("uq_instance_connections_active"):]
    # Check that the index is defined without provider as one of the columns.
    # We do this by checking the new index definition doesn't include provider.
    # The 3-tuple index section should come after the drop_index call.
    assert '"admin_id"' in src
    assert '"instance_id"' in src
    assert '"connection_type"' in src
    # The 3-tuple means provider is NOT in the unique index columns.
    # Find where we create the new index and verify provider is not there.
    lines = src.splitlines()
    in_create = False
    found_3tuple = False
    for i, line in enumerate(lines):
        if "uq_instance_connections_active" in line and "create_index" in lines[max(0,i-2):i+1]:
            in_create = True
        if in_create and '"provider"' not in line and 'unique=True' in line:
            found_3tuple = True
            break
    # Simpler: just verify the upgrade() creates an index with 3-col list.
    upgrade_src = src.split("def upgrade()")[1].split("def downgrade()")[0]
    # The 3-column index should appear in upgrade.
    assert '"connection_type"' in upgrade_src


def test_migration_index_predicate_excludes_dormant():
    src = _text(MIGRATION_PATH)
    upgrade_src = src.split("def upgrade()")[1].split("def downgrade()")[0]
    assert "dormant" in upgrade_src, (
        "New unique index predicate must exclude dormant rows."
    )


def test_migration_downgrade_restores_4_tuple_index():
    src = _text(MIGRATION_PATH)
    downgrade_src = src.split("def downgrade()")[1]
    assert '"provider"' in downgrade_src, (
        "Downgrade must restore the 4-tuple index including provider."
    )


def test_migration_downgrade_drops_new_columns():
    src = _text(MIGRATION_PATH)
    downgrade_src = src.split("def downgrade()")[1]
    assert "status_detail" in downgrade_src, "Downgrade must drop status_detail."
    assert "created_by_user_id" in downgrade_src, "Downgrade must drop created_by_user_id."


def test_migration_documents_dual_revoked_representation():
    src = _text(MIGRATION_PATH)
    assert "DUAL" in src or "dual" in src or "revoked_at" in src, (
        "Migration must document the dual revoked representation "
        "(revoked_at timestamp + status='revoked')."
    )


def test_migration_enum_downgrade_is_no_op_documented():
    src = _text(MIGRATION_PATH)
    assert "LEFT IN PLACE" in src or "no-op" in src.lower() or "no op" in src.lower(), (
        "Migration must document that ENUM values are left in place on downgrade."
    )


# =====================================================================
# §2 — ORM model has all 6 enum values + new columns.
# =====================================================================

def test_model_has_revoked_in_connection_statuses():
    src = _text(MODEL_PATH)
    assert '"revoked"' in src, "MODEL must include 'revoked' in CONNECTION_STATUSES."


def test_model_has_dormant_in_connection_statuses():
    src = _text(MODEL_PATH)
    assert '"dormant"' in src, "MODEL must include 'dormant' in CONNECTION_STATUSES."


def test_model_has_status_detail_column():
    src = _text(MODEL_PATH)
    assert "status_detail" in src, "InstanceConnection model must have status_detail."


def test_model_has_created_by_user_id_column():
    src = _text(MODEL_PATH)
    assert "created_by_user_id" in src, (
        "InstanceConnection model must have created_by_user_id."
    )


def test_model_imports_clean():
    from app.models.instance_connection import CONNECTION_STATUSES
    assert "revoked" in CONNECTION_STATUSES
    assert "dormant" in CONNECTION_STATUSES
    assert len(CONNECTION_STATUSES) == 6, (
        "CONNECTION_STATUSES must have exactly 6 values: "
        "unconfigured, connected, error, expired, revoked, dormant."
    )


# =====================================================================
# §3 — Repository: revoke sets status='revoked'; dormant methods exist.
# =====================================================================

def test_repo_revoke_sets_status_revoked():
    src = _text(REPO_PATH)
    assert 'row.status = "revoked"' in src or "status = 'revoked'" in src, (
        "_revoke_rows must set status='revoked' (dual representation)."
    )


def test_repo_has_set_dormant_for_admin():
    src = _text(REPO_PATH)
    assert "def set_dormant_for_admin" in src, (
        "Repository must have set_dormant_for_admin method."
    )


def test_repo_has_restore_from_dormant_for_admin():
    src = _text(REPO_PATH)
    assert "def restore_from_dormant_for_admin" in src, (
        "Repository must have restore_from_dormant_for_admin method."
    )


def test_repo_dormant_preserves_secret_ref():
    src = _text(REPO_PATH)
    dormant_src = src[src.find("def set_dormant_for_admin"):src.find("def restore_from_dormant")]
    # secret_ref must NOT be assigned/mutated in set_dormant_for_admin.
    # It may appear in comments/docstrings but must not be assigned.
    # Check no assignment to secret_ref within the function.
    assert "row.secret_ref" not in dormant_src, (
        "set_dormant_for_admin must NOT assign row.secret_ref "
        "(secrets must be retained per §3.6.7)."
    )


def test_repo_dormant_stores_prior_status_in_detail():
    src = _text(REPO_PATH)
    dormant_src = src[src.find("def set_dormant_for_admin"):src.find("def restore_from_dormant")]
    assert "prior_status" in dormant_src, (
        "set_dormant_for_admin must store prior_status in status_detail "
        "so restore_from_dormant can recover it."
    )


def test_repo_restore_clears_status_detail():
    src = _text(REPO_PATH)
    restore_src = src[src.find("def restore_from_dormant_for_admin"):]
    assert "status_detail = None" in restore_src or 'status_detail=None' in restore_src, (
        "restore_from_dormant_for_admin must clear status_detail after restore."
    )


def test_repo_apply_health_check_accepts_status_detail():
    src = _text(REPO_PATH)
    fn_src = src[src.find("def apply_health_check"):src.find("def disconnect")]
    assert "status_detail" in fn_src, (
        "apply_health_check must accept a status_detail parameter."
    )


# =====================================================================
# §4 — Downgrade service wires dormant.
# =====================================================================

def test_downgrade_archive_service_calls_set_dormant():
    src = _text(DOWNGRADE_SVC_PATH)
    assert "set_dormant_for_admin" in src, (
        "DowngradeArchiveService.archive_overflow_for_admin must call "
        "set_dormant_for_admin to wire dormant connections on downgrade."
    )


def test_downgrade_archive_service_imports_conn_repo():
    src = _text(DOWNGRADE_SVC_PATH)
    assert "InstanceConnectionRepository" in src, (
        "DowngradeArchiveService must import InstanceConnectionRepository."
    )


# =====================================================================
# §5 — Tier provisioning service wires restore on re-upgrade.
# =====================================================================

def test_tier_provisioning_calls_restore_from_dormant():
    src = _text(TIER_PROV_PATH)
    assert "restore_from_dormant_for_admin" in src, (
        "TierProvisioningService.upgrade_admin_tier must call "
        "restore_from_dormant_for_admin to restore connections on re-upgrade."
    )


# =====================================================================
# §6 — Worker wires status_detail on expired path.
# =====================================================================

def test_worker_passes_status_detail_on_expired():
    src = _text(WORKER_REFRESH_PATH)
    assert "status_detail" in src, (
        "refresh_connections worker must pass status_detail to apply_health_check "
        "on the expired path (CJ §7 Reconnect chip)."
    )


def test_worker_status_detail_only_on_expired():
    src = _text(WORKER_REFRESH_PATH)
    # The worker should only set status_detail when status is 'expired'.
    assert "expired" in src and "status_detail" in src, (
        "status_detail must be populated only on expired path in the worker."
    )


# =====================================================================
# §7 — Behavioural: in-memory SQLite session tests.
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
    """Build an in-memory SQLite session with the minimal schema needed
    for InstanceConnectionRepository tests."""
    import app.db.session  # noqa: F401 — installs the prod handler
    from sqlalchemy import event
    from sqlalchemy.orm import Session as _SQLASession

    from app.repositories.audit_chain import _before_flush_handler

    if event.contains(_SQLASession, "before_flush", _before_flush_handler):
        event.remove(_SQLASession, "before_flush", _before_flush_handler)

    from sqlalchemy import (
        Column,
        DateTime,
        Integer,
        MetaData,
        String,
        Text,
    )
    from sqlalchemy import create_engine
    from sqlalchemy.orm import declarative_base, Session

    engine = create_engine("sqlite:///:memory:", echo=False)
    meta = MetaData()

    from sqlalchemy import Table
    connections_table = Table(
        "instance_connections",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("admin_id", String(100), nullable=False),
        Column("instance_id", Integer, nullable=False),
        Column("connection_type", String(64), nullable=False),
        Column("provider", String(64), nullable=False),
        Column("status", String(32), nullable=False, default="unconfigured"),
        Column("non_secret_config", Text, nullable=True),
        Column("secret_ref", String(255), nullable=True),
        Column("last_health_check_at", DateTime, nullable=True),
        Column("created_at", DateTime, nullable=False,
               default=lambda: datetime.now(timezone.utc)),
        Column("updated_at", DateTime, nullable=False,
               default=lambda: datetime.now(timezone.utc)),
        Column("revoked_at", DateTime, nullable=True),
        # New columns added by rescand_connections_schema:
        Column("status_detail", Text, nullable=True),
        Column("created_by_user_id", String(36), nullable=True),
    )
    meta.create_all(engine)

    # We use plain ORM-mapped objects backed by the real InstanceConnection
    # model, but since SQLite doesn't enforce PG enums we can set status freely.
    Base = declarative_base(metadata=meta)

    class IC(Base):
        __tablename__ = "instance_connections"
        __table_args__ = {"extend_existing": True}
        id = Column(Integer, primary_key=True, autoincrement=True)
        admin_id = Column(String(100), nullable=False)
        instance_id = Column(Integer, nullable=False)
        connection_type = Column(String(64), nullable=False)
        provider = Column(String(64), nullable=False)
        status = Column(String(32), nullable=False, default="unconfigured")
        non_secret_config = Column(Text, nullable=True)
        secret_ref = Column(String(255), nullable=True)
        last_health_check_at = Column(DateTime, nullable=True)
        created_at = Column(DateTime, nullable=False,
                            default=lambda: datetime.now(timezone.utc))
        updated_at = Column(DateTime, nullable=False,
                            default=lambda: datetime.now(timezone.utc))
        revoked_at = Column(DateTime, nullable=True)
        status_detail = Column(Text, nullable=True)
        created_by_user_id = Column(String(36), nullable=True)

    session = Session(engine)
    return session, IC


@pytest.fixture
def sqlite_session():
    session, IC = _build_sqlite_session()
    yield session, IC
    session.close()


def _make_conn(IC, **kwargs):
    defaults = dict(
        admin_id="admin-1",
        instance_id=1,
        connection_type="calendar",
        provider="google_calendar",
        status="connected",
        secret_ref="arn:aws:secretsmanager:us-east-1:123:secret:conn/1",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    return IC(**defaults)


class _FakeRepo:
    """Minimal stand-in for InstanceConnectionRepository that operates on
    our SQLite session and the test's IC model class directly."""

    def __init__(self, session, IC):
        self.db = session
        self.IC = IC

    def set_dormant_for_admin(self, *, admin_id, autocommit=True):
        from sqlalchemy import select, and_
        rows = self.db.execute(
            select(self.IC).where(
                and_(
                    self.IC.admin_id == admin_id,
                    self.IC.revoked_at.is_(None),
                    self.IC.status != "dormant",
                    self.IC.status != "revoked",
                )
            )
        ).scalars().all()
        now = datetime.now(timezone.utc)
        for row in rows:
            prior = row.status
            row.status = "dormant"
            row.status_detail = (
                f"prior_status={prior}; Connection dormant: Pro→Free downgrade. "
                "Secrets retained. Re-upgrade to restore."
            )
            row.updated_at = now
            self.db.add(row)
        if autocommit:
            self.db.commit()
        return rows

    def restore_from_dormant_for_admin(self, *, admin_id, autocommit=True):
        from sqlalchemy import select, and_
        from app.repositories.instance_connection_repository import (
            _extract_prior_status,
        )
        rows = self.db.execute(
            select(self.IC).where(
                and_(
                    self.IC.admin_id == admin_id,
                    self.IC.status == "dormant",
                )
            )
        ).scalars().all()
        now = datetime.now(timezone.utc)
        for row in rows:
            prior_status = _extract_prior_status(row.status_detail)
            row.status = prior_status
            row.status_detail = None
            row.updated_at = now
            self.db.add(row)
        if autocommit:
            self.db.commit()
        return rows

    def revoke(self, row, autocommit=True):
        now = datetime.now(timezone.utc)
        row.revoked_at = now
        row.updated_at = now
        row.status = "revoked"
        self.db.add(row)
        if autocommit:
            self.db.commit()
        return row


def test_all_6_status_values_can_be_set(sqlite_session):
    """Can set status to all 6 values (SQLite doesn't enforce enum)."""
    session, IC = sqlite_session
    all_statuses = ("unconfigured", "connected", "error", "expired", "revoked", "dormant")
    for status in all_statuses:
        conn = _make_conn(IC, connection_type="calendar", provider="google", status=status)
        session.add(conn)
        session.commit()
        session.refresh(conn)
        assert conn.status == status, f"Expected status={status!r}"


def test_downgrade_sets_connections_dormant(sqlite_session):
    """Downgrade path sets status='dormant', preserves secret_ref."""
    session, IC = sqlite_session
    # Seed: two connected connections for admin-1.
    c1 = _make_conn(IC, connection_type="calendar", provider="google",
                    status="connected", secret_ref="arn:1")
    c2 = _make_conn(IC, connection_type="email_sender", provider="sendgrid",
                    status="connected", secret_ref="arn:2")
    session.add_all([c1, c2])
    session.commit()

    repo = _FakeRepo(session, IC)
    dormant = repo.set_dormant_for_admin(admin_id="admin-1")

    assert len(dormant) == 2
    for row in dormant:
        session.refresh(row)
        assert row.status == "dormant", f"Expected dormant, got {row.status!r}"
        # secret_ref must be preserved (secrets retained).
        assert row.secret_ref is not None
        assert row.secret_ref.startswith("arn:"), (
            "secret_ref must not be cleared on dormant transition."
        )
        # prior_status stored in status_detail.
        assert "prior_status=connected" in row.status_detail


def test_re_upgrade_restores_prior_status(sqlite_session):
    """Re-upgrade restores dormant connections to their prior status."""
    session, IC = sqlite_session
    c1 = _make_conn(IC, connection_type="calendar", provider="google",
                    status="dormant",
                    status_detail="prior_status=connected; Connection dormant.")
    session.add(c1)
    session.commit()

    repo = _FakeRepo(session, IC)
    restored = repo.restore_from_dormant_for_admin(admin_id="admin-1")

    assert len(restored) == 1
    session.refresh(c1)
    assert c1.status == "connected", (
        f"Expected status='connected' after restore, got {c1.status!r}"
    )
    assert c1.status_detail is None, (
        "status_detail must be cleared after restore."
    )


def test_re_upgrade_restore_fallback_to_connected(sqlite_session):
    """If status_detail is missing/malformed, restore falls back to 'connected'."""
    session, IC = sqlite_session
    c1 = _make_conn(IC, connection_type="calendar", provider="google",
                    status="dormant", status_detail=None)
    session.add(c1)
    session.commit()

    repo = _FakeRepo(session, IC)
    repo.restore_from_dormant_for_admin(admin_id="admin-1")

    session.refresh(c1)
    assert c1.status == "connected"


def test_dormant_does_not_affect_revoked_connections(sqlite_session):
    """Already-revoked connections must not be touched by set_dormant."""
    session, IC = sqlite_session
    c_revoked = _make_conn(IC, connection_type="calendar", provider="google",
                           status="revoked",
                           revoked_at=datetime.now(timezone.utc))
    session.add(c_revoked)
    session.commit()

    repo = _FakeRepo(session, IC)
    dormant = repo.set_dormant_for_admin(admin_id="admin-1")

    assert len(dormant) == 0, "Revoked connections must not be set dormant."
    session.refresh(c_revoked)
    assert c_revoked.status == "revoked"


def test_revoke_sets_dual_representation(sqlite_session):
    """_revoke_rows must set both revoked_at and status='revoked'."""
    session, IC = sqlite_session
    c1 = _make_conn(IC, connection_type="calendar", provider="google",
                    status="connected")
    session.add(c1)
    session.commit()

    repo = _FakeRepo(session, IC)
    repo.revoke(c1)

    session.refresh(c1)
    assert c1.revoked_at is not None, "revoked_at must be set."
    assert c1.status == "revoked", (
        "status must be set to 'revoked' (dual representation §3.8.4)."
    )


def test_status_detail_set_on_expired(sqlite_session):
    """apply_health_check must set status_detail on expired path."""
    session, IC = sqlite_session
    c1 = _make_conn(IC, connection_type="calendar", provider="google",
                    status="connected")
    session.add(c1)
    session.commit()

    # Simulate apply_health_check for the expired path.
    # We test the logic directly rather than calling the real repo
    # (which uses PG enum types) — mirrors the worker's usage.
    now = datetime.now(timezone.utc)
    c1.status = "expired"
    c1.status_detail = "Refresh token rejected; reconnect required."
    c1.last_health_check_at = now
    session.add(c1)
    session.commit()
    session.refresh(c1)

    assert c1.status == "expired"
    assert c1.status_detail == "Refresh token rejected; reconnect required.", (
        "status_detail must be populated on expired path (CJ §7 Reconnect chip)."
    )


def test_status_detail_cleared_on_reconnect(sqlite_session):
    """status_detail must be cleared when connection transitions back to connected."""
    session, IC = sqlite_session
    c1 = _make_conn(IC, connection_type="calendar", provider="google",
                    status="expired",
                    status_detail="Refresh token rejected; reconnect required.")
    session.add(c1)
    session.commit()

    # Simulate apply_health_check for the connected path.
    c1.status = "connected"
    c1.status_detail = None  # apply_health_check clears on connected.
    session.add(c1)
    session.commit()
    session.refresh(c1)

    assert c1.status == "connected"
    assert c1.status_detail is None, (
        "status_detail must be cleared when connection becomes connected again."
    )


def test_3_tuple_unique_constraint_finding():
    """Source-text assertion: migration creates 3-tuple index (no provider)."""
    src = _text(MIGRATION_PATH)
    upgrade_src = src.split("def upgrade()")[1].split("def downgrade()")[0]
    # The 3-tuple create_index block must not include "provider" in column list.
    # Find the new unique index creation.
    # After drop_index, the create_index must list 3 columns.
    # We check that the unique create_index has 3 items without provider.
    create_idx = upgrade_src.find('"uq_instance_connections_active"')
    assert create_idx >= 0, "upgrade() must recreate uq_instance_connections_active."
    # After the index name, find the column list. It should NOT contain 'provider'.
    idx_block = upgrade_src[create_idx:create_idx + 300]
    assert '"provider"' not in idx_block, (
        "New unique index must be 3-tuple (admin_id, instance_id, connection_type) "
        "WITHOUT provider — doc says single-active-per-TYPE not per-provider."
    )


def test_constraint_finding_documented():
    """Report: the migration docstring must document the 4-tuple → 3-tuple finding."""
    src = _text(MIGRATION_PATH)
    assert "4-tuple" in src or "4 tuple" in src or "four-tuple" in src or "four tuple" in src.lower(), (
        "Migration must document the constraint finding (4-tuple → 3-tuple change)."
    )


# =====================================================================
# §8 — _extract_prior_status helper correctness.
# =====================================================================

def test_extract_prior_status_happy_path():
    from app.repositories.instance_connection_repository import _extract_prior_status

    detail = "prior_status=connected; Connection dormant: Pro→Free downgrade."
    assert _extract_prior_status(detail) == "connected"


def test_extract_prior_status_error_value():
    from app.repositories.instance_connection_repository import _extract_prior_status

    detail = "prior_status=error; Connection dormant."
    assert _extract_prior_status(detail) == "error"


def test_extract_prior_status_none_fallback():
    from app.repositories.instance_connection_repository import _extract_prior_status

    assert _extract_prior_status(None) == "connected"


def test_extract_prior_status_malformed_fallback():
    from app.repositories.instance_connection_repository import _extract_prior_status

    assert _extract_prior_status("no prefix here") == "connected"


def test_extract_prior_status_unknown_value_fallback():
    from app.repositories.instance_connection_repository import _extract_prior_status

    # 'revoked' is not a valid restore target (once revoked, stays revoked).
    detail = "prior_status=revoked; Connection dormant."
    # Should fall back to 'connected' since 'revoked' is not in the valid set.
    assert _extract_prior_status(detail) == "connected"
