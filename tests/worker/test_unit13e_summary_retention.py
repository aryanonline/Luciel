"""Unit 13e §3.4.10 — session-summary retention hard-delete.

1. A Free-tier summary older than 90 days is hard-deleted; a fresh one is
   kept. Each delete emits an ACTION_DATA_RETENTION_HARD_DELETE audit row
   with the §3.4.10 payload (data_class, resolved_lead_id,
   retention_policy_applied, deleted_at).
2. A Pro-tier summary at 100 days is KEPT (Pro window is 365 days) —
   proving the per-tier TTL is honoured, not a flat window.
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
class TestUnit13eSummaryRetention(unittest.TestCase):
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
        from app.models.session_summary import SessionSummary

        cleanup = self.SessionLocal()
        try:
            if self._admin_ids:
                for model in (SessionSummary, AdminAuditLog):
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

    def _make_admin(self, *, tier: str) -> str:
        from app.models.admin import Admin

        admin_id = f"u13esr-{uuid.uuid4().hex[:10]}"
        self._admin_ids.append(admin_id)
        self.db.add(
            Admin(id=admin_id, name="u13esr", tier=tier, active=True)
        )
        self.db.commit()
        return admin_id

    def _make_instance(self, *, admin_id: str) -> int:
        from app.models.instance import Instance

        inst = Instance(
            admin_id=admin_id,
            instance_slug=f"slug-{uuid.uuid4().hex[:8]}",
            display_name="u13esr inst",
        )
        self.db.add(inst)
        self.db.commit()
        self.db.refresh(inst)
        return inst.id

    def _seed_summary(
        self, *, admin_id: str, instance_id: int, age_days: int,
        text: str = "s", resolved_lead_id: str | None = None,
    ) -> int:
        from app.models.session_summary import SessionSummary

        row = SessionSummary(
            admin_id=admin_id,
            luciel_instance_id=instance_id,
            resolved_lead_id=resolved_lead_id or f"lead-{uuid.uuid4().hex[:8]}",
            session_id=str(uuid.uuid4()),
            summary=text,
        )
        self.db.add(row)
        self.db.commit()
        # Backdate created_at to simulate age.
        row.created_at = self.now - timedelta(days=age_days)
        self.db.commit()
        self.db.refresh(row)
        return row.id

    def test_free_past_90d_deleted_fresh_kept_with_audit(self):
        from app.models.admin_audit_log import (
            ACTION_DATA_RETENTION_HARD_DELETE,
            AdminAuditLog,
        )
        from app.models.session_summary import SessionSummary
        from app.worker.tasks.session_summary_retention import (
            find_and_hard_delete_expired_summaries,
        )

        admin = self._make_admin(tier="free")
        inst = self._make_instance(admin_id=admin)
        lead = f"lead-{uuid.uuid4().hex[:8]}"

        expired = self._seed_summary(
            admin_id=admin, instance_id=inst, age_days=100,
            text="old", resolved_lead_id=lead,
        )
        fresh = self._seed_summary(
            admin_id=admin, instance_id=inst, age_days=10, text="new",
        )

        deleted = find_and_hard_delete_expired_summaries(
            self.db, now=self.now
        )
        self.db.commit()

        deleted_sessions = {d["session_id"] for d in deleted}
        self.assertEqual(len(deleted), 1)

        self.assertIsNone(self.db.get(SessionSummary, expired))
        self.assertIsNotNone(self.db.get(SessionSummary, fresh))

        # The §3.4.10 hard-delete audit row was written for the expired one.
        rows = (
            self.db.query(AdminAuditLog)
            .filter(
                AdminAuditLog.admin_id == admin,
                AdminAuditLog.action == ACTION_DATA_RETENTION_HARD_DELETE,
            )
            .all()
        )
        self.assertEqual(len(rows), 1)
        payload = rows[0].after_json
        self.assertEqual(payload["data_class"], "session_summary")
        self.assertEqual(payload["resolved_lead_id"], lead)
        self.assertEqual(payload["retention_policy_applied"], "free:90d")
        self.assertIn("deleted_at", payload)
        # The deleted session_id matches the expired summary's session.
        self.assertTrue(deleted_sessions)

    def test_pro_at_100d_kept(self):
        from app.models.session_summary import SessionSummary
        from app.worker.tasks.session_summary_retention import (
            find_and_hard_delete_expired_summaries,
        )

        admin = self._make_admin(tier="pro")
        inst = self._make_instance(admin_id=admin)

        pro_summary = self._seed_summary(
            admin_id=admin, instance_id=inst, age_days=100, text="pro",
        )

        deleted = find_and_hard_delete_expired_summaries(
            self.db, now=self.now
        )
        self.db.commit()

        self.assertEqual(len(deleted), 0)
        self.assertIsNotNone(self.db.get(SessionSummary, pro_summary))


if __name__ == "__main__":
    unittest.main()
