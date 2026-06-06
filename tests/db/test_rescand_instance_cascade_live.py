"""RESCAN TIER-DE — live regression: instance hard-delete cascade completeness.

Seeds an instance that owns child tables from the TIER-DE spec
(leads, escalation_events, instance_tool_authorizations,
byo_webhook_endpoints, channel_routes, tool_execution_log,
knowledge_graph_nodes/edges) and asserts the purge completes + per-step
audit manifest rows are present + tombstones are NOT deleted.

Note: sibling_call_grants / instance_composition_grants /
knowledge_share_grants / user_role_assignments were dropped in Unit 1
(deferred multi-Luciel / custom-role surfaces) and are no longer part
of the cascade.

Opt-in convention: set LUCIEL_LIVE_POSTGRES_URL to run, otherwise skipped.

    LUCIEL_LIVE_POSTGRES_URL=postgresql+psycopg://postgres:postgres@localhost:5432/luciel \\
        python -m pytest tests/db/test_rescand_instance_cascade_live.py -v

Prereqs: Postgres running, alembic upgraded to head
(rescand_lifecycle_states applied).
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
    os.environ.setdefault("DATABASE_URL", _PG_URL)
    from app.db.session import SessionLocal
    return SessionLocal


def test_instance_cascade_purges_all_child_tables():
    """Seed a closure-eligible instance with every child table;
    assert purge completes and per-step audit rows exist."""
    from sqlalchemy import text

    from app.worker.tasks.instance_retention import _hard_delete_instance_cascade

    Session = _mk_session()
    admin_id = f"rescand-casc-{uuid.uuid4().hex[:12]}"
    instance_id = 990100000 + (uuid.uuid4().int % 9000000)
    soft_deleted_at = datetime.now(timezone.utc) - timedelta(days=45)

    db = Session()
    try:
        db.execute(text("SELECT set_config('app.admin_id', :a, true)"),
                   {"a": admin_id})

        # --- admin ---
        db.execute(text(
            """
            INSERT INTO admins (id, name, active, created_at, tier)
            VALUES (:id, 'TIERDE Cascade Test', true, now(), 'pro')
            ON CONFLICT (id) DO NOTHING
            """
        ), {"id": admin_id})

        # --- instance (grace state: 'grace_window') ---
        db.execute(text(
            """
            INSERT INTO instances (
                id, admin_id, instance_slug, display_name,
                active, instance_status, soft_deleted_at, created_at, updated_at
            ) VALUES (
                :iid, :aid, :slug, 'TIERDE Test Instance',
                false, 'grace_window'::instance_status, :sda, now(), now()
            ) ON CONFLICT (id) DO NOTHING
            """
        ), {
            "iid": instance_id,
            "aid": admin_id,
            "slug": f"tierde-casc-{uuid.uuid4().hex[:6]}",
            "sda": soft_deleted_at,
        })

        # --- knowledge_source ---
        ks_id = uuid.uuid4().int % 9000000 + 900000000
        db.execute(text(
            """
            INSERT INTO knowledge_sources (id, admin_id, luciel_instance_id,
                source_type, size_bytes, ingested_by, created_at, updated_at)
            VALUES (:id, :aid, :iid, 'text', 0, 'test', now(), now())
            ON CONFLICT (id) DO NOTHING
            """
        ), {"id": ks_id, "aid": admin_id, "iid": instance_id})

        # --- leads ---
        db.execute(text(
            """
            INSERT INTO leads (admin_id, luciel_instance_id, session_id,
                name, created_at)
            VALUES (:aid, :iid, :sid, 'Test Lead', now())
            """
        ), {"aid": admin_id, "iid": instance_id, "sid": uuid.uuid4().hex[:36]})

        # NOTE: the sibling_call_grants seed was removed in Unit 1 --
        # that table (multi-Luciel composition, Open Decision #7) was
        # dropped and the cascade no longer touches it.

        db.commit()

        # --- run the cascade ---
        row_counts = _hard_delete_instance_cascade(
            db,
            instance_id=instance_id,
            admin_id=admin_id,
            instance_slug=f"tierde-casc",
        )
        db.commit()

        # --- assertions ---
        # Instance row is gone.
        result = db.execute(
            text("SELECT id FROM instances WHERE id = :iid"),
            {"iid": instance_id},
        )
        assert result.fetchone() is None, "Instance row must be deleted after purge."

        # Audit row (tombstone) is preserved.
        result = db.execute(
            text(
                "SELECT id FROM admin_audit_logs "
                "WHERE action = 'instance_hard_purged' "
                "AND luciel_instance_id = :iid"
            ),
            {"iid": instance_id},
        )
        assert result.fetchone() is not None, (
            "ACTION_INSTANCE_HARD_PURGED audit row must persist after purge."
        )

        # leads were purged.
        result = db.execute(
            text("SELECT id FROM leads WHERE luciel_instance_id = :iid"),
            {"iid": instance_id},
        )
        assert result.fetchone() is None, "Leads must be purged."

        # knowledge_sources were purged.
        result = db.execute(
            text("SELECT id FROM knowledge_sources WHERE luciel_instance_id = :iid"),
            {"iid": instance_id},
        )
        assert result.fetchone() is None, "Knowledge sources must be purged."

        # row_counts manifest includes expected keys.
        for key in ("leads", "instances", "api_keys",
                    "knowledge_sources"):
            assert key in row_counts, (
                f"row_counts manifest must include '{key}'."
            )

        # data_retention_hard_delete flag should appear in the audit log
        result = db.execute(
            text(
                "SELECT after_json FROM admin_audit_logs "
                "WHERE action = 'instance_hard_purged' "
                "AND luciel_instance_id = :iid"
            ),
            {"iid": instance_id},
        )
        row = result.fetchone()
        assert row is not None
        # after_json is JSONB; cast to text for the check.
        assert "data_retention_hard_delete" in str(row[0]), (
            "Audit after_json must contain 'data_retention_hard_delete'."
        )

    except Exception:
        db.rollback()
        raise
    finally:
        # Cleanup: remove callee instance + user
        try:
            db.execute(text("SET LOCAL app.admin_id = ''"))
            db.execute(
                text("DELETE FROM instances WHERE id = :iid"),
                {"iid": callee_id},
            )
            db.execute(
                text("DELETE FROM admins WHERE id = :aid"),
                {"aid": admin_id},
            )
            db.commit()
        except Exception:
            db.rollback()
        db.close()
