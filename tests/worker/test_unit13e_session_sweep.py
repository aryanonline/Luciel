"""Unit 13e §3.4.8/§3.4.9 — session inactivity sweep + reopened-thread.

1. A session idle past its channel-class TTL is finalized deterministically
   (status='ended' + the ACTION_SESSION_FINALIZED_INACTIVITY audit row).
2. A session NOT yet past its TTL is left active.
3. After a session ends, the §3.4.8 session-key lookup no longer resolves
   to it — a new inbound starts a NEW session (a new budget unit), per the
   §3.4.9 reopened-thread rule.
"""
from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("MODERATION_PROVIDER", "null")

_DB_URL = os.environ.get("DATABASE_URL", "")
_LIVE = _DB_URL.startswith("postgresql+psycopg://") or bool(
    os.environ.get("LUCIEL_LIVE_POSTGRES_URL")
)


@unittest.skipUnless(_LIVE, "Requires a live Postgres DATABASE_URL")
class TestUnit13eSessionSweep(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.db.session import SessionLocal

        cls.SessionLocal = SessionLocal

    def setUp(self) -> None:
        self.db = self.SessionLocal()
        self._admin_ids: list[str] = []
        self.now = datetime.now(timezone.utc)

    def tearDown(self) -> None:
        self.db.rollback()
        self.db.close()
        self._purge()

    def _purge(self) -> None:
        from app.models.admin import Admin
        from app.models.admin_audit_log import AdminAuditLog
        from app.models.instance import Instance
        from app.models.session import SessionModel

        cleanup = self.SessionLocal()
        try:
            if self._admin_ids:
                for model in (SessionModel, AdminAuditLog):
                    cleanup.query(model).filter(
                        model.admin_id.in_(self._admin_ids)
                    ).delete(synchronize_session=False)
                cleanup.query(Instance).filter(
                    Instance.admin_id.in_(self._admin_ids)
                ).delete(synchronize_session=False)
                cleanup.query(Admin).filter(
                    Admin.id.in_(self._admin_ids)
                ).delete(synchronize_session=False)
            cleanup.commit()
        except Exception:
            cleanup.rollback()
        finally:
            cleanup.close()

    def _make_admin(self) -> str:
        from app.models.admin import Admin

        admin_id = f"u13es-{uuid.uuid4().hex[:10]}"
        self._admin_ids.append(admin_id)
        self.db.add(Admin(id=admin_id, name="u13es", tier="pro", active=True))
        self.db.commit()
        return admin_id

    def _make_instance(self, *, admin_id: str) -> int:
        from app.models.instance import Instance

        inst = Instance(
            admin_id=admin_id,
            instance_slug=f"slug-{uuid.uuid4().hex[:8]}",
            display_name="u13es inst",
        )
        self.db.add(inst)
        self.db.commit()
        self.db.refresh(inst)
        return inst.id

    def _seed_session(
        self, *, admin_id: str, instance_id: int, channel: str,
        idle_minutes: int, resolved_lead_id: str | None = None,
    ) -> str:
        from app.models.session import SessionModel

        sid = str(uuid.uuid4())
        s = SessionModel(
            id=sid,
            admin_id=admin_id,
            luciel_instance_id=instance_id,
            user_id=resolved_lead_id,
            resolved_lead_id=resolved_lead_id,
            channel=channel,
            status="active",
        )
        self.db.add(s)
        self.db.commit()
        # Backdate updated_at to simulate idleness.
        s.updated_at = self.now - timedelta(minutes=idle_minutes)
        self.db.commit()
        return sid

    # -- 1 + 2: sweep finalizes past-TTL, leaves fresh sessions -----------

    def test_past_ttl_session_finalized_fresh_left_active(self):
        from app.models.admin_audit_log import (
            ACTION_SESSION_FINALIZED_INACTIVITY,
            AdminAuditLog,
        )
        from app.models.session import SessionModel
        from app.worker.tasks.session_sweep import (
            find_and_finalize_expired_sessions,
        )

        admin = self._make_admin()
        inst = self._make_instance(admin_id=admin)

        # widget = synchronous_web (30 min). 40 min idle → finalize.
        expired = self._seed_session(
            admin_id=admin, instance_id=inst, channel="widget",
            idle_minutes=40,
        )
        # widget, only 5 min idle → still active.
        fresh = self._seed_session(
            admin_id=admin, instance_id=inst, channel="widget",
            idle_minutes=5,
        )
        # sms = async_messaging (4 h). 40 min idle → still active
        # (proves the class table is honoured, not a flat window).
        sms_fresh = self._seed_session(
            admin_id=admin, instance_id=inst, channel="sms",
            idle_minutes=40,
        )

        finalized = find_and_finalize_expired_sessions(self.db, now=self.now)
        self.db.commit()

        finalized_ids = {f["session_id"] for f in finalized}
        self.assertIn(expired, finalized_ids)
        self.assertNotIn(fresh, finalized_ids)
        self.assertNotIn(sms_fresh, finalized_ids)

        self.assertEqual(
            self.db.get(SessionModel, expired).status, "ended"
        )
        self.assertEqual(
            self.db.get(SessionModel, fresh).status, "active"
        )
        self.assertEqual(
            self.db.get(SessionModel, sms_fresh).status, "active"
        )

        # The §3.4.8 audit row was written for the finalized session.
        rows = (
            self.db.query(AdminAuditLog)
            .filter(
                AdminAuditLog.admin_id == admin,
                AdminAuditLog.action == ACTION_SESSION_FINALIZED_INACTIVITY,
                AdminAuditLog.resource_natural_id == expired,
            )
            .all()
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].after_json["channel_class"], "synchronous_web")
        self.assertEqual(rows[0].after_json["timeout_seconds"], 30 * 60)

    def test_sweep_idempotent_on_already_ended(self):
        from app.worker.tasks.session_sweep import (
            find_and_finalize_expired_sessions,
        )

        admin = self._make_admin()
        inst = self._make_instance(admin_id=admin)
        self._seed_session(
            admin_id=admin, instance_id=inst, channel="widget",
            idle_minutes=40,
        )

        first = find_and_finalize_expired_sessions(self.db, now=self.now)
        self.db.commit()
        self.assertEqual(len(first), 1)

        # Second run finds no active candidate → no double-finalize.
        second = find_and_finalize_expired_sessions(self.db, now=self.now)
        self.db.commit()
        self.assertEqual(len(second), 0)

    # -- 3: reopened-thread rule -----------------------------------------

    def test_reopened_thread_starts_new_session(self):
        from app.repositories.session_repository import SessionRepository
        from app.worker.tasks.session_sweep import (
            find_and_finalize_expired_sessions,
        )

        admin = self._make_admin()
        inst = self._make_instance(admin_id=admin)
        lead = f"lead-{uuid.uuid4().hex[:8]}"

        original = self._seed_session(
            admin_id=admin, instance_id=inst, channel="widget",
            idle_minutes=40, resolved_lead_id=lead,
        )
        repo = SessionRepository(self.db)

        # Before the sweep the key resolves to the active session.
        before = repo.find_session_by_key(
            luciel_instance_id=inst, resolved_lead_id=lead,
            channel="widget", admin_id=admin,
        )
        self.assertIsNotNone(before)
        self.assertEqual(before.id, original)

        # Sweep finalizes it (status='ended').
        find_and_finalize_expired_sessions(self.db, now=self.now)
        self.db.commit()

        # After end, the key no longer resolves to the ended session — a
        # new inbound starts a NEW session (a new budget unit).
        after = repo.find_session_by_key(
            luciel_instance_id=inst, resolved_lead_id=lead,
            channel="widget", admin_id=admin,
        )
        self.assertIsNone(after)


if __name__ == "__main__":
    unittest.main()
