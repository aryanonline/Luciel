"""Unit 9 §3.5.4/§3.5.5 — escalation ack mechanism (live Postgres).

Covers EscalationDeliveryService.mark_acked + the
POST /admin/escalations/{id}/ack endpoint behaviorally:

  * mark_acked transitions delivery_status -> 'acked' and emits the
    ACTION_ESCALATION_ACKED audit row.
  * idempotent: a second mark_acked on an already-acked event is a
    no-op success (no duplicate audit row).
  * cross-tenant event id is not found (tenant fence returns None).
  * the ack endpoint: happy path (status -> acked, audit row) + 404
    cross-tenant.
  * ACTION_ESCALATION_ACKED and ACTION_ESCALATION_OWNER_FALLBACK are in
    ALLOWED_ACTIONS (a record() call with each succeeds, not ValueError).

Skipped unless the harness points at a real Postgres (run_tests.sh sets
LUCIEL_LIVE_POSTGRES_URL so these RUN, not skip).
"""
from __future__ import annotations

import os
import types
import unittest
import uuid

os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("OPENAI_API_KEY", "dummy")

_DB_URL = os.environ.get("DATABASE_URL", "")
_LIVE = _DB_URL.startswith("postgresql+psycopg://") or bool(
    os.environ.get("LUCIEL_LIVE_POSTGRES_URL")
)


def _request(*, admin_id: str, actor_user_id=None, platform=True):
    req = types.SimpleNamespace()
    req.state = types.SimpleNamespace(
        admin_id=admin_id,
        permissions=["platform_admin"] if platform else [],
        actor_user_id=actor_user_id,
    )
    req.headers = {}
    req.client = types.SimpleNamespace(host="127.0.0.1")
    return req


@unittest.skipUnless(
    _LIVE,
    "Requires DATABASE_URL=postgresql+psycopg://... or LUCIEL_LIVE_POSTGRES_URL",
)
class TestUnit9EscalationAck(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.db.session import SessionLocal

        cls.SessionLocal = SessionLocal

    def setUp(self) -> None:
        self.db = self.SessionLocal()
        self._admin_ids: list[str] = []
        self._event_ids: list[int] = []

    def tearDown(self) -> None:
        self.db.rollback()
        self.db.close()
        if self._admin_ids:
            self._purge(self._admin_ids, self._event_ids)

    def _purge(self, admin_ids: list[str], event_ids: list[int]) -> None:
        from app.models.escalation_event import EscalationEvent
        from app.models.instance import Instance

        cleanup = self.SessionLocal()
        try:
            if event_ids:
                cleanup.query(EscalationEvent).filter(
                    EscalationEvent.id.in_(event_ids)
                ).delete(synchronize_session=False)
            cleanup.query(Instance).filter(
                Instance.admin_id.in_(admin_ids)
            ).delete(synchronize_session=False)
            cleanup.commit()
        except Exception:
            cleanup.rollback()
        finally:
            cleanup.close()

    def _make_admin_instance(self):
        from app.models.admin import Admin
        from app.models.instance import Instance

        admin_id = f"unit9-{uuid.uuid4().hex[:10]}"
        self._admin_ids.append(admin_id)
        self.db.add(Admin(id=admin_id, name="unit9 rt", tier="pro", active=True))
        self.db.flush()
        inst = Instance(
            admin_id=admin_id,
            instance_slug=f"i-{uuid.uuid4().hex[:8]}",
            display_name="unit9 rt instance",
        )
        self.db.add(inst)
        self.db.flush()
        return admin_id, inst

    def _make_event(self, *, admin_id, inst, delivery_status="delivered"):
        from app.models.escalation_event import (
            EscalationEvent,
            GATE_OUTCOME,
            SIGNAL_HIGH_VALUE_LEAD,
        )

        row = EscalationEvent(
            admin_id=admin_id,
            luciel_instance_id=inst.id,
            session_id=str(uuid.uuid4()),
            signal=SIGNAL_HIGH_VALUE_LEAD,
            gate=GATE_OUTCOME,
            delivery_status=delivery_status,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        self._event_ids.append(row.id)
        return row

    def _service(self):
        from app.services.escalation_delivery_service import (
            EscalationDeliveryService,
        )

        return EscalationDeliveryService()

    def _audit_rows(self, *, event_id, action):
        from app.models.admin_audit_log import AdminAuditLog

        return (
            self.db.query(AdminAuditLog)
            .filter(
                AdminAuditLog.action == action,
                AdminAuditLog.resource_pk == event_id,
            )
            .all()
        )

    # =================================================================
    # mark_acked
    # =================================================================

    def test_mark_acked_transitions_and_emits_audit(self):
        from app.models.admin_audit_log import ACTION_ESCALATION_ACKED
        from app.models.escalation_event import (
            EscalationEvent,
            DELIVERY_STATUS_ACKED,
        )

        admin_id, inst = self._make_admin_instance()
        event = self._make_event(admin_id=admin_id, inst=inst)
        actor = uuid.uuid4()

        new_status = self._service().mark_acked(
            event_id=event.id,
            admin_id=admin_id,
            actor_user_id=actor,
            db=self.db,
        )
        self.db.commit()

        self.assertEqual(new_status, DELIVERY_STATUS_ACKED)
        refreshed = self.db.get(EscalationEvent, event.id)
        self.assertEqual(refreshed.delivery_status, DELIVERY_STATUS_ACKED)

        rows = self._audit_rows(event_id=event.id, action=ACTION_ESCALATION_ACKED)
        self.assertEqual(len(rows), 1)
        after = rows[0].after_json
        self.assertEqual(after["event_id"], event.id)
        self.assertEqual(after["actor_user_id"], str(actor))
        self.assertEqual(after["signal"], event.signal)

    def test_mark_acked_idempotent_second_call_no_duplicate(self):
        from app.models.admin_audit_log import ACTION_ESCALATION_ACKED
        from app.models.escalation_event import DELIVERY_STATUS_ACKED

        admin_id, inst = self._make_admin_instance()
        event = self._make_event(admin_id=admin_id, inst=inst)

        svc = self._service()
        first = svc.mark_acked(
            event_id=event.id, admin_id=admin_id, actor_user_id=None, db=self.db
        )
        self.db.commit()
        second = svc.mark_acked(
            event_id=event.id, admin_id=admin_id, actor_user_id=None, db=self.db
        )
        self.db.commit()

        self.assertEqual(first, DELIVERY_STATUS_ACKED)
        self.assertEqual(second, DELIVERY_STATUS_ACKED)
        rows = self._audit_rows(event_id=event.id, action=ACTION_ESCALATION_ACKED)
        self.assertEqual(len(rows), 1, "idempotent ack must not duplicate audit row")

    def test_mark_acked_cross_tenant_not_found(self):
        admin_id, inst = self._make_admin_instance()
        event = self._make_event(admin_id=admin_id, inst=inst)

        other_admin = f"unit9-other-{uuid.uuid4().hex[:8]}"
        result = self._service().mark_acked(
            event_id=event.id,
            admin_id=other_admin,
            actor_user_id=None,
            db=self.db,
        )
        self.assertIsNone(result)

    # =================================================================
    # ack endpoint
    # =================================================================

    def test_ack_endpoint_happy_path(self):
        from app.api.v1.admin_escalation_ack import admin_ack_escalation
        from app.models.admin_audit_log import ACTION_ESCALATION_ACKED
        from app.models.escalation_event import DELIVERY_STATUS_ACKED

        admin_id, inst = self._make_admin_instance()
        event = self._make_event(admin_id=admin_id, inst=inst)
        actor = uuid.uuid4()

        resp = admin_ack_escalation(
            request=_request(admin_id=admin_id, actor_user_id=actor),
            escalation_id=event.id,
            db=self.db,
        )

        self.assertEqual(resp.escalation_id, event.id)
        self.assertEqual(resp.delivery_status, DELIVERY_STATUS_ACKED)

        rows = self._audit_rows(event_id=event.id, action=ACTION_ESCALATION_ACKED)
        self.assertEqual(len(rows), 1)

    def test_ack_endpoint_404_cross_tenant(self):
        from fastapi import HTTPException

        from app.api.v1.admin_escalation_ack import admin_ack_escalation
        from app.models.admin import Admin

        admin_a, inst_a = self._make_admin_instance()
        event = self._make_event(admin_id=admin_a, inst=inst_a)

        admin_b = f"unit9-b-{uuid.uuid4().hex[:8]}"
        self._admin_ids.append(admin_b)
        self.db.add(Admin(id=admin_b, name="unit9 b", tier="pro", active=True))
        self.db.commit()

        req_b = _request(admin_id=admin_b, platform=False)

        with self.assertRaises(HTTPException) as caught:
            admin_ack_escalation(
                request=req_b,
                escalation_id=event.id,
                db=self.db,
            )
        self.assertEqual(caught.exception.status_code, 404)

    # =================================================================
    # Recordability (no ValueError) of the two new actions.
    # =================================================================

    def test_new_actions_are_recordable(self):
        from app.models.admin_audit_log import (
            ACTION_ESCALATION_ACKED,
            ACTION_ESCALATION_OWNER_FALLBACK,
            RESOURCE_ESCALATION_EVENT,
        )
        from app.repositories.admin_audit_repository import (
            AdminAuditRepository,
            AuditContext,
        )

        admin_id, inst = self._make_admin_instance()
        self.db.commit()
        repo = AdminAuditRepository(self.db)
        ctx = AuditContext.system("unit9-test")

        for action in (ACTION_ESCALATION_ACKED, ACTION_ESCALATION_OWNER_FALLBACK):
            row = repo.record(
                ctx=ctx,
                admin_id=admin_id,
                action=action,
                resource_type=RESOURCE_ESCALATION_EVENT,
                resource_pk=None,
                resource_natural_id="sess-recordable",
                luciel_instance_id=inst.id,
                after={"probe": action},
                note=f"recordability probe {action}",
            )
            self.assertIsNotNone(row)
        self.db.commit()


if __name__ == "__main__":
    unittest.main()
