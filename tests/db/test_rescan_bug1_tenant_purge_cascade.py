"""RESCAN BUG-1 — live regression for the tenant hard-delete FK cascade.

Manifest cite: RESCAN_AUDIT_MANIFEST.md TIER A / BUG-1; Architecture
§3.6.5 (hard-delete cascade) + §3.6.6 (account closure).

The bug
-------
``AdminService.hard_delete_tenant_after_retention`` issued
``DELETE FROM instances WHERE admin_id=:tid`` without first clearing the
child tables that carry ON DELETE RESTRICT foreign keys to
``instances.id`` (knowledge_sources, instance_connections,
instance_tool_authorizations, sibling_call_grants,
instance_composition_grants, knowledge_share_grants,
byo_webhook_endpoints, tool_execution_log, user_role_assignments,
channel_routes). For ANY tenant that had ever ingested a knowledge
source or configured a connection, the instance DELETE raised
PostgreSQL FK violation 23503, aborting the whole purge transaction.
The tenant was never tombstoned and the retention worker re-queued it
forever — a silent PIPEDA P5 / GDPR Art.17 retention-timeline breach.

This test reproduces the exact precondition (a closure-eligible tenant
with one instance that owns a knowledge_source AND an
instance_connection) and asserts the purge now completes and tombstones
the admin. Before the 6b fix this test fails with an IntegrityError;
after it, the admin row is tombstoned and the children are gone.

Opt-in convention matches tests/db/test_arc11_knowledge_rls.py: set
``LUCIEL_LIVE_POSTGRES_URL`` to run; otherwise skipped so stock
backend-free CI collection is unaffected.

    LUCIEL_LIVE_POSTGRES_URL=postgresql+psycopg://postgres:postgres@localhost:5432/luciel \\
        python -m pytest tests/db/test_rescan_bug1_tenant_purge_cascade.py -v

Prereqs at the URL: Postgres + pgvector, alembic upgraded to head.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

_PG_URL = os.environ.get("LUCIEL_LIVE_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not _PG_URL,
    reason="Set LUCIEL_LIVE_POSTGRES_URL=postgresql+psycopg://... to run",
)


def _mk_session():
    # Use the app's real SessionLocal so BOTH session-level listeners
    # are active exactly as in production:
    #   * audit_chain before_flush (populates row_hash/prev_row_hash) and
    #   * the after_begin tenant-context GUC setter.
    # Importing app.db.session installs install_audit_chain_event() at
    # module import (the structural choke point documented in that file).
    # The app reads DATABASE_URL from settings; the test sets it to the
    # same LUCIEL_LIVE_POSTGRES_URL before importing.
    os.environ.setdefault("DATABASE_URL", _PG_URL)
    from app.db.session import SessionLocal

    return SessionLocal


def test_tenant_purge_completes_with_knowledge_and_connection():
    """BUG-1: purge must not FK-violate when the tenant owns a
    knowledge_source + an instance_connection; admin must be tombstoned."""
    from sqlalchemy import text

    from app.services.admin_service import AdminService

    Session = _mk_session()
    admin_id = f"rescan-bug1-{uuid.uuid4().hex[:12]}"
    # instances.id is an integer identity column; pick a high, unlikely-
    # to-collide value and let children reference it directly.
    instance_id = 990000000 + (uuid.uuid4().int % 9000000)
    closure_ts = datetime.now(timezone.utc) - timedelta(days=45)  # eligible

    db = Session()
    try:
        # The cascade reads/writes via the app's tenant-scoped session;
        # set the GUC so RLS does not hide the rows we seed.
        db.execute(text("SELECT set_config('app.admin_id', :a, true)"),
                   {"a": admin_id})

        # --- seed: admin (closure-eligible, inactive), instance, and the
        #     two RESTRICT-FK children that used to break the cascade ---
        db.execute(text(
            """
            INSERT INTO admins (id, name, active, deactivated_at,
                                closure_initiated_at, created_at, tier)
            VALUES (:id, 'BUG1 Tenant', false, :ts, :ts, now(), 'pro')
            ON CONFLICT (id) DO NOTHING
            """
        ), {"id": admin_id, "ts": closure_ts})

        db.execute(text(
            """
            INSERT INTO instances
                (id, admin_id, instance_slug, display_name,
                 active, created_at, instance_status)
            OVERRIDING SYSTEM VALUE
            VALUES (:iid, :aid, :slug, 'BUG1 Instance', true, now(), 'active')
            ON CONFLICT (id) DO NOTHING
            """
        ), {"iid": instance_id, "aid": admin_id,
            "slug": f"bug1-{instance_id}"})

        # knowledge_source — RESTRICT FK on luciel_instance_id (bigint)
        db.execute(text(
            """
            INSERT INTO knowledge_sources
                (admin_id, luciel_instance_id, source_type,
                 filename, size_bytes, ingested_by, ingestion_status, created_at)
            VALUES (:aid, :iid, 'paste', 'bug1 source', 11,
                    'bug1-seed', 'ready', now())
            """
        ), {"aid": admin_id, "iid": instance_id})

        # instance_connection — RESTRICT FK on instance_id (integer)
        db.execute(text(
            """
            INSERT INTO instance_connections
                (admin_id, instance_id, connection_type,
                 provider, status, created_at)
            VALUES (:aid, :iid, 'crm', 'hubspot', 'connected', now())
            """
        ), {"aid": admin_id, "iid": instance_id})
        db.commit()

        # --- run the purge (the unit under test) ---
        svc = AdminService(db)
        counts = svc.hard_delete_tenant_after_retention(
            admin_id, retention_window_days=30
        )
        db.commit()

        # --- assert: purge ran, children gone, admin tombstoned ---
        assert counts, "purge returned empty dict (eligibility guard fired?)"
        assert counts.get("knowledge_sources", 0) >= 1
        assert counts.get("instance_connections", 0) >= 1
        assert counts.get("instances", 0) >= 1

        remaining_instances = db.execute(
            text("SELECT count(*) FROM instances WHERE admin_id=:a"),
            {"a": admin_id},
        ).scalar()
        assert remaining_instances == 0, "instance row not deleted"

        tombstoned = db.execute(
            text("SELECT name, hard_deleted_at FROM admins WHERE id=:a"),
            {"a": admin_id},
        ).one()
        assert tombstoned.hard_deleted_at is not None, "admin not tombstoned"
        assert tombstoned.name == "[REDACTED]", "PII not redacted on tombstone"
    finally:
        # best-effort cleanup of any residue if the assertions failed early
        for tbl, col in (
            ("instance_connections", "admin_id"),
            ("knowledge_sources", "admin_id"),
            ("instances", "admin_id"),
            ("admins", "id"),
        ):
            try:
                db.execute(text(f"DELETE FROM {tbl} WHERE {col}=:a"),
                           {"a": admin_id})
            except Exception:
                db.rollback()
        db.commit()
        db.close()
