"""Unit 13d (§3.9) — cross-tenant analytics isolation (NON-NEGOTIABLE).

Seeds TWO tenants' sessions / leads / escalation_events / traces, then
runs the AnalyticsService for tenant A and asserts every metric counts
ONLY tenant A's rows — tenant B's data is never included. The service is
invoked under ``bind_tenant_scope`` so the database RLS policy is active
(production posture), AND the service's explicit ``WHERE admin_id`` fence
is the belt-and-suspenders second layer.

This lives in the isolation suite (tests/db) and MUST pass. It does not
weaken any existing isolation test; it adds a new cross-tenant exclusion
guarantee for the §3.9 read surface.
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
class TestUnit13dAnalyticsIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.db.session import SessionLocal

        cls.SessionLocal = SessionLocal

    def setUp(self) -> None:
        self.db = self.SessionLocal()
        self._admin_ids: list[str] = []
        self.now = datetime.now(timezone.utc)
        self.ts = self.now - timedelta(days=2)

    def tearDown(self) -> None:
        self.db.rollback()
        self.db.close()
        self._purge()

    def _period(self):
        from app.analytics.service import AnalyticsPeriod

        return AnalyticsPeriod(
            start=self.now - timedelta(days=30),
            end=self.now + timedelta(days=1),
            label="iso_window",
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

    # -- seeding ----------------------------------------------------------

    def _make_admin(self) -> str:
        from app.models.admin import Admin

        admin_id = f"u13di-{uuid.uuid4().hex[:10]}"
        self._admin_ids.append(admin_id)
        self.db.add(Admin(id=admin_id, name="u13di", tier="pro", active=True))
        self.db.commit()
        return admin_id

    def _make_instance(self, *, admin_id: str) -> int:
        from app.models.instance import Instance

        inst = Instance(
            admin_id=admin_id,
            instance_slug=f"slug-{uuid.uuid4().hex[:8]}",
            display_name="iso inst",
        )
        self.db.add(inst)
        self.db.commit()
        self.db.refresh(inst)
        return inst.id

    def _seed_tenant(
        self, *, admin_id: str, instance_id: int, n_sessions: int,
        n_leads_converted: int, signal: str, n_traces_appt: int,
        source_id: int,
    ) -> None:
        from app.models.escalation_event import EscalationEvent
        from app.models.lead import Lead
        from app.models.session import SessionModel
        from app.models.trace import Trace

        for _ in range(n_sessions):
            s = SessionModel(
                id=str(uuid.uuid4()),
                admin_id=admin_id,
                luciel_instance_id=instance_id,
                user_id="cust",
                channel="web",
            )
            self.db.add(s)
            self.db.commit()
            s.created_at = self.ts
            self.db.commit()

        for _ in range(n_leads_converted):
            lead = Lead(
                admin_id=admin_id,
                luciel_instance_id=instance_id,
                session_id=str(uuid.uuid4()),
                user_id="cust",
                intent="x",
                outcome="converted",
            )
            self.db.add(lead)
            self.db.commit()
            lead.created_at = self.ts
            self.db.commit()

        ev = EscalationEvent(
            admin_id=admin_id,
            luciel_instance_id=instance_id,
            session_id=str(uuid.uuid4()),
            user_id="cust",
            signal=signal,
            gate="intake",
        )
        self.db.add(ev)
        self.db.commit()
        ev.created_at = self.ts
        self.db.commit()

        for _ in range(n_traces_appt):
            t = Trace(
                trace_id=str(uuid.uuid4()),
                session_id=str(uuid.uuid4()),
                admin_id=admin_id,
                luciel_instance_id=instance_id,
                user_message="hi",
                assistant_reply="ok",
                tool_called=True,
                tool_name="book_appointment",
                source_ids_used=[source_id],
            )
            self.db.add(t)
            self.db.commit()
            t.created_at = self.ts
            self.db.commit()

    # -- the isolation guarantee -----------------------------------------

    def test_tenant_a_analytics_exclude_tenant_b(self):
        from app.analytics.service import AnalyticsService
        from app.db.tenant_scope import bind_tenant_scope

        admin_a = self._make_admin()
        admin_b = self._make_admin()
        inst_a = self._make_instance(admin_id=admin_a)
        inst_b = self._make_instance(admin_id=admin_b)

        # Tenant A: 2 sessions, 2 converted leads, 1 high_value_lead
        # escalation, 1 booked appt, source 101.
        self._seed_tenant(
            admin_id=admin_a, instance_id=inst_a, n_sessions=2,
            n_leads_converted=2, signal="high_value_lead",
            n_traces_appt=1, source_id=101,
        )
        # Tenant B: 5 sessions, 5 converted leads, a different signal,
        # 4 booked appts, source 202 — none should appear for A.
        self._seed_tenant(
            admin_id=admin_b, instance_id=inst_b, n_sessions=5,
            n_leads_converted=5, signal="explicit_human_request",
            n_traces_appt=4, source_id=202,
        )

        period = self._period()

        # Run A's analytics under A's RLS scope (production posture).
        scoped = self.SessionLocal()
        try:
            with bind_tenant_scope(admin_id=admin_a, instance_id=inst_a):
                svc = AnalyticsService(scoped)
                conv = svc.conversations(admin_id=admin_a, period=period)
                leads = svc.leads(admin_id=admin_a, period=period)
                esc = svc.escalations_by_signal(admin_id=admin_a, period=period)
                appts = svc.appointments_booked(admin_id=admin_a, period=period)
                conversion = svc.conversion(admin_id=admin_a, period=period)
                channels = svc.channel_mix(admin_id=admin_a, period=period)
                sources = svc.top_knowledge_sources(
                    admin_id=admin_a, period=period
                )
        finally:
            scoped.close()

        # Counts reflect ONLY tenant A.
        self.assertEqual(conv["this_period"], 2)
        self.assertEqual(conv["total"], 2)
        self.assertEqual(leads["this_period"], 2)
        self.assertEqual(esc["high_value_lead"], 1)
        # Tenant B's signal must be 0 for A.
        self.assertEqual(esc["explicit_human_request"], 0)
        self.assertEqual(appts, 1)
        self.assertEqual(conversion["by_outcome"]["converted"], 2)
        self.assertEqual(channels["total"], 2)
        source_ids = {r["source_id"] for r in sources}
        self.assertIn(101, source_ids)
        self.assertNotIn(202, source_ids)


if __name__ == "__main__":
    unittest.main()
