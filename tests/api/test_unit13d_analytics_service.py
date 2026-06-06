"""Unit 13d (§3.9) — AnalyticsService metric + tier-shape behavioral tests.

Seeds real rows (sessions, leads, escalation_events, traces, the
ACTION_ESCALATION_ACKED audit row) for ONE tenant against live Postgres
and asserts every AnalyticsService metric computes the seeded values, then
asserts the tier shape: a Free admin's ``compute`` returns ONLY the BASIC
subset; a Pro admin's returns the full surface.

These isolate the metric LOGIC; the cross-tenant exclusion guarantee is
proven separately in tests/db/test_unit13d_analytics_isolation.py.
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
class TestUnit13dAnalyticsService(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.db.session import SessionLocal

        cls.SessionLocal = SessionLocal

    def setUp(self) -> None:
        self.db = self.SessionLocal()
        self._admin_ids: list[str] = []
        self._instance_ids: list[int] = []
        self.now = datetime.now(timezone.utc)
        # An in-window timestamp and an out-of-window one (60 days ago).
        self.in_window = self.now - timedelta(days=2)
        self.out_window = self.now - timedelta(days=60)

    def tearDown(self) -> None:
        self.db.rollback()
        self.db.close()
        self._purge()

    def _period(self):
        from app.analytics.service import AnalyticsPeriod

        return AnalyticsPeriod(
            start=self.now - timedelta(days=30),
            end=self.now + timedelta(days=1),
            label="test_window",
        )

    def _purge(self) -> None:
        from app.models.admin import Admin
        from app.models.admin_audit_log import AdminAuditLog
        from app.models.escalation_event import EscalationEvent
        from app.models.instance import Instance
        from app.models.lead import Lead
        from app.models.session import SessionModel
        from app.models.trace import Trace

        cleanup = self.SessionLocal()
        try:
            if self._admin_ids:
                for model in (
                    Trace,
                    EscalationEvent,
                    SessionModel,
                    Lead,
                    AdminAuditLog,
                ):
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

    # -- seeding helpers --------------------------------------------------

    def _make_admin(self, tier: str = "pro") -> str:
        from app.models.admin import Admin

        admin_id = f"u13ds-{uuid.uuid4().hex[:10]}"
        self._admin_ids.append(admin_id)
        self.db.add(Admin(id=admin_id, name="u13ds", tier=tier, active=True))
        self.db.commit()
        return admin_id

    def _make_instance(self, *, admin_id: str) -> int:
        from app.models.instance import Instance

        inst = Instance(
            admin_id=admin_id,
            instance_slug=f"slug-{uuid.uuid4().hex[:8]}",
            display_name="u13ds inst",
        )
        self.db.add(inst)
        self.db.commit()
        self.db.refresh(inst)
        self._instance_ids.append(inst.id)
        return inst.id

    def _make_session(
        self, *, admin_id: str, instance_id: int, channel: str, created_at
    ) -> str:
        from app.models.session import SessionModel

        sid = str(uuid.uuid4())
        row = SessionModel(
            id=sid,
            admin_id=admin_id,
            luciel_instance_id=instance_id,
            user_id="cust",
            channel=channel,
        )
        self.db.add(row)
        self.db.commit()
        # created_at is server-defaulted to now(); overwrite for windowing.
        row.created_at = created_at
        self.db.commit()
        return sid

    def _make_lead(self, *, admin_id: str, instance_id: int, outcome, created_at):
        from app.models.lead import Lead

        row = Lead(
            admin_id=admin_id,
            luciel_instance_id=instance_id,
            session_id=str(uuid.uuid4()),
            user_id="cust",
            intent="wants a viewing",
            outcome=outcome,
        )
        self.db.add(row)
        self.db.commit()
        row.created_at = created_at
        self.db.commit()
        return row

    def _make_escalation(
        self, *, admin_id: str, instance_id: int, signal: str, created_at
    ):
        from app.models.escalation_event import EscalationEvent

        row = EscalationEvent(
            admin_id=admin_id,
            luciel_instance_id=instance_id,
            session_id=str(uuid.uuid4()),
            user_id="cust",
            signal=signal,
            gate="intake",
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        row.created_at = created_at
        self.db.commit()
        return row

    def _ack_escalation(self, *, admin_id: str, event, acked_at):
        """Write the ACTION_ESCALATION_ACKED audit row the p50/p95 join reads."""
        from app.models.admin_audit_log import (
            ACTION_ESCALATION_ACKED,
            RESOURCE_ESCALATION_EVENT,
        )
        from app.repositories.admin_audit_repository import (
            AdminAuditRepository,
            AuditContext,
        )

        AdminAuditRepository(self.db).record(
            ctx=AuditContext.system("u13ds-test"),
            admin_id=admin_id,
            action=ACTION_ESCALATION_ACKED,
            resource_type=RESOURCE_ESCALATION_EVENT,
            resource_pk=event.id,
            resource_natural_id=event.session_id,
            luciel_instance_id=event.luciel_instance_id,
            before={},
            after={"event_id": event.id},
            note="ack",
            autocommit=True,
        )
        # Force the audit row's created_at to a deterministic ack time.
        from app.models.admin_audit_log import AdminAuditLog

        audit = (
            self.db.query(AdminAuditLog)
            .filter(
                AdminAuditLog.action == ACTION_ESCALATION_ACKED,
                AdminAuditLog.resource_pk == event.id,
                AdminAuditLog.admin_id == admin_id,
            )
            .one()
        )
        audit.created_at = acked_at
        self.db.commit()

    def _make_trace(
        self,
        *,
        admin_id: str,
        instance_id: int,
        created_at,
        tool_name=None,
        source_ids=None,
    ):
        from app.models.trace import Trace

        row = Trace(
            trace_id=str(uuid.uuid4()),
            session_id=str(uuid.uuid4()),
            admin_id=admin_id,
            luciel_instance_id=instance_id,
            user_message="hi",
            assistant_reply="hello",
            tool_called=tool_name is not None,
            tool_name=tool_name,
            source_ids_used=source_ids or [],
        )
        self.db.add(row)
        self.db.commit()
        row.created_at = created_at
        self.db.commit()
        return row

    # -- metric tests -----------------------------------------------------

    def test_conversations_period_and_total(self):
        from app.analytics.service import AnalyticsService

        admin = self._make_admin()
        inst = self._make_instance(admin_id=admin)
        self._make_session(
            admin_id=admin, instance_id=inst, channel="web",
            created_at=self.in_window,
        )
        self._make_session(
            admin_id=admin, instance_id=inst, channel="web",
            created_at=self.out_window,
        )

        svc = AnalyticsService(self.db)
        m = svc.conversations(admin_id=admin, period=self._period())
        self.assertEqual(m["this_period"], 1)
        self.assertEqual(m["total"], 2)

    def test_leads_period_and_total(self):
        from app.analytics.service import AnalyticsService

        admin = self._make_admin()
        inst = self._make_instance(admin_id=admin)
        self._make_lead(
            admin_id=admin, instance_id=inst, outcome=None,
            created_at=self.in_window,
        )
        self._make_lead(
            admin_id=admin, instance_id=inst, outcome="converted",
            created_at=self.out_window,
        )

        svc = AnalyticsService(self.db)
        m = svc.leads(admin_id=admin, period=self._period())
        self.assertEqual(m["this_period"], 1)
        self.assertEqual(m["total"], 2)

    def test_escalations_by_signal_keys_and_counts(self):
        from app.analytics.service import AnalyticsService

        admin = self._make_admin()
        inst = self._make_instance(admin_id=admin)
        self._make_escalation(
            admin_id=admin, instance_id=inst,
            signal="explicit_human_request", created_at=self.in_window,
        )
        self._make_escalation(
            admin_id=admin, instance_id=inst,
            signal="explicit_human_request", created_at=self.in_window,
        )
        self._make_escalation(
            admin_id=admin, instance_id=inst,
            signal="high_value_lead", created_at=self.in_window,
        )

        svc = AnalyticsService(self.db)
        m = svc.escalations_by_signal(admin_id=admin, period=self._period())
        # All four doctrinal signals always present.
        for sig in (
            "explicit_human_request",
            "cannot_confidently_answer",
            "high_value_lead",
            "strong_negative_sentiment",
        ):
            self.assertIn(sig, m)
        self.assertEqual(m["explicit_human_request"], 2)
        self.assertEqual(m["high_value_lead"], 1)
        self.assertEqual(m["cannot_confidently_answer"], 0)

    def test_escalation_first_response_p50_p95(self):
        from app.analytics.service import AnalyticsService

        admin = self._make_admin()
        inst = self._make_instance(admin_id=admin)
        # Two acked escalations: latencies 100s and 300s.
        ev1 = self._make_escalation(
            admin_id=admin, instance_id=inst,
            signal="high_value_lead", created_at=self.in_window,
        )
        self._ack_escalation(
            admin_id=admin, event=ev1,
            acked_at=self.in_window + timedelta(seconds=100),
        )
        ev2 = self._make_escalation(
            admin_id=admin, instance_id=inst,
            signal="high_value_lead", created_at=self.in_window,
        )
        self._ack_escalation(
            admin_id=admin, event=ev2,
            acked_at=self.in_window + timedelta(seconds=300),
        )

        svc = AnalyticsService(self.db)
        m = svc.escalation_first_response(admin_id=admin, period=self._period())
        self.assertEqual(m["count"], 2)
        self.assertIsNotNone(m["p50_seconds"])
        # p50 of {100,300} = 200; p95 ~ 290.
        self.assertAlmostEqual(m["p50_seconds"], 200.0, delta=1.0)
        self.assertGreater(m["p95_seconds"], m["p50_seconds"])

    def test_appointments_booked_counts_book_appointment_tool(self):
        from app.analytics.service import AnalyticsService

        admin = self._make_admin()
        inst = self._make_instance(admin_id=admin)
        self._make_trace(
            admin_id=admin, instance_id=inst, created_at=self.in_window,
            tool_name="book_appointment",
        )
        self._make_trace(
            admin_id=admin, instance_id=inst, created_at=self.in_window,
            tool_name="other_tool",
        )
        self._make_trace(
            admin_id=admin, instance_id=inst, created_at=self.in_window,
            tool_name=None,
        )

        svc = AnalyticsService(self.db)
        n = svc.appointments_booked(admin_id=admin, period=self._period())
        self.assertEqual(n, 1)

    def test_conversion_rate(self):
        from app.analytics.service import AnalyticsService

        admin = self._make_admin()
        inst = self._make_instance(admin_id=admin)
        # 3 converted, 1 lost, 1 in_progress, 1 unset → rate 3/4 = 0.75.
        for _ in range(3):
            self._make_lead(
                admin_id=admin, instance_id=inst, outcome="converted",
                created_at=self.in_window,
            )
        self._make_lead(
            admin_id=admin, instance_id=inst, outcome="lost",
            created_at=self.in_window,
        )
        self._make_lead(
            admin_id=admin, instance_id=inst, outcome="in_progress",
            created_at=self.in_window,
        )
        self._make_lead(
            admin_id=admin, instance_id=inst, outcome=None,
            created_at=self.in_window,
        )

        svc = AnalyticsService(self.db)
        m = svc.conversion(admin_id=admin, period=self._period())
        self.assertEqual(m["by_outcome"]["converted"], 3)
        self.assertEqual(m["by_outcome"]["lost"], 1)
        self.assertEqual(m["by_outcome"]["in_progress"], 1)
        self.assertEqual(m["by_outcome"]["unset"], 1)
        self.assertAlmostEqual(m["rate"], 0.75, delta=1e-9)

    def test_channel_mix_fractions(self):
        from app.analytics.service import AnalyticsService

        admin = self._make_admin()
        inst = self._make_instance(admin_id=admin)
        for _ in range(3):
            self._make_session(
                admin_id=admin, instance_id=inst, channel="web",
                created_at=self.in_window,
            )
        self._make_session(
            admin_id=admin, instance_id=inst, channel="sms",
            created_at=self.in_window,
        )

        svc = AnalyticsService(self.db)
        m = svc.channel_mix(admin_id=admin, period=self._period())
        self.assertEqual(m["total"], 4)
        self.assertEqual(m["counts"]["web"], 3)
        self.assertEqual(m["counts"]["sms"], 1)
        self.assertAlmostEqual(m["fractions"]["web"], 0.75, delta=1e-9)

    def test_top_knowledge_sources(self):
        from app.analytics.service import AnalyticsService

        admin = self._make_admin()
        inst = self._make_instance(admin_id=admin)
        self._make_trace(
            admin_id=admin, instance_id=inst, created_at=self.in_window,
            source_ids=[101, 102],
        )
        self._make_trace(
            admin_id=admin, instance_id=inst, created_at=self.in_window,
            source_ids=[101],
        )

        svc = AnalyticsService(self.db)
        rows = svc.top_knowledge_sources(admin_id=admin, period=self._period())
        by_id = {r["source_id"]: r["retrievals"] for r in rows}
        self.assertEqual(by_id[101], 2)
        self.assertEqual(by_id[102], 1)
        # Ordered by frequency desc → 101 first.
        self.assertEqual(rows[0]["source_id"], 101)

    def test_busiest_times_cells(self):
        from app.analytics.service import AnalyticsService

        admin = self._make_admin()
        inst = self._make_instance(admin_id=admin)
        self._make_session(
            admin_id=admin, instance_id=inst, channel="web",
            created_at=self.in_window,
        )

        svc = AnalyticsService(self.db)
        cells = svc.busiest_times(admin_id=admin, period=self._period())
        self.assertEqual(sum(c["count"] for c in cells), 1)
        for c in cells:
            self.assertIn("day_of_week", c)
            self.assertIn("hour", c)

    # -- tier shape -------------------------------------------------------

    def test_compute_free_returns_basic_only(self):
        from app.analytics.service import (
            BASIC_METRIC_KEYS,
            PRO_ONLY_METRIC_KEYS,
            AnalyticsService,
        )
        from app.policy.entitlements import TIER_FREE

        admin = self._make_admin(tier="free")
        inst = self._make_instance(admin_id=admin)
        self._make_session(
            admin_id=admin, instance_id=inst, channel="web",
            created_at=self.in_window,
        )

        svc = AnalyticsService(self.db)
        report = svc.compute(
            admin_id=admin, tier=TIER_FREE, period=self._period()
        )
        for key in BASIC_METRIC_KEYS:
            self.assertIn(key, report)
        for key in PRO_ONLY_METRIC_KEYS:
            self.assertNotIn(key, report)

    def test_compute_pro_returns_full_surface(self):
        from app.analytics.service import (
            BASIC_METRIC_KEYS,
            PRO_ONLY_METRIC_KEYS,
            AnalyticsService,
        )
        from app.policy.entitlements import TIER_PRO

        admin = self._make_admin(tier="pro")
        inst = self._make_instance(admin_id=admin)
        self._make_session(
            admin_id=admin, instance_id=inst, channel="web",
            created_at=self.in_window,
        )

        svc = AnalyticsService(self.db)
        report = svc.compute(
            admin_id=admin, tier=TIER_PRO, period=self._period()
        )
        for key in BASIC_METRIC_KEYS | PRO_ONLY_METRIC_KEYS:
            self.assertIn(key, report)


if __name__ == "__main__":
    unittest.main()
