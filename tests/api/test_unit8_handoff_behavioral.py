"""Unit 8 §3.4.12 — human-handoff admin API BEHAVIORAL tests (real Postgres).

The existing ``tests/api/test_tierc_admin_handoff_routes.py`` covers the
handoff endpoints only via SOURCE-TEXT assertions (``assert "X" in
handoff_source``) — they prove strings exist in the module, NOT that the
endpoints behave correctly. This file closes that gap: it calls the REAL
route function bodies (admin_takeover / admin_handback / admin_reply)
against a real-Postgres ORM session, exactly like the arc13 channels
route tests, and asserts OUTCOMES — DB state transitions, audit rows,
the handback duration math, tenant isolation, and admin-reply attribution.

A platform_admin caller is synthesised (permissions=['platform_admin'])
so these tests isolate the takeover LOGIC from role resolution, exactly
as the arc13 channels-route test does. Permission/role gating is already
covered by the source tests + the arc12b permission tests.

Skipped unless the harness points at a real Postgres (the run_tests.sh
harness sets LUCIEL_LIVE_POSTGRES_URL so these RUN, not skip).
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


def _request(*, admin_id: str, actor_user_id=None):
    """Synthesise a platform_admin Request bound to ``admin_id``.

    Mirrors the arc13 channels-route ``_request`` helper. ``actor_user_id``
    flows into the takeover/handback/reply attribution path.
    """
    req = types.SimpleNamespace()
    req.state = types.SimpleNamespace(
        admin_id=admin_id,
        permissions=["platform_admin"],
        actor_user_id=actor_user_id,
    )
    req.headers = {}
    req.client = types.SimpleNamespace(host="127.0.0.1")
    return req


@unittest.skipUnless(
    _LIVE,
    "Requires DATABASE_URL=postgresql+psycopg://... or LUCIEL_LIVE_POSTGRES_URL",
)
class TestUnit8HandoffBehavioral(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.db.session import SessionLocal

        cls.SessionLocal = SessionLocal

    # -----------------------------------------------------------------
    # Fixtures — build Admin + Instance + SessionModel rows, mirroring
    # arc13's _make_admin_instance. The route handlers under test call
    # db.commit(), so committed rows persist in the real DB; tearDown
    # purges sessions + instances it created (audit rows are append-only
    # and intentionally kept).
    # -----------------------------------------------------------------

    def setUp(self) -> None:
        self.db = self.SessionLocal()
        self._admin_ids: list[str] = []
        self._session_ids: list[str] = []

    def tearDown(self) -> None:
        self.db.rollback()
        self.db.close()
        if self._admin_ids:
            self._purge(self._admin_ids, self._session_ids)

    def _purge(self, admin_ids: list[str], session_ids: list[str]) -> None:
        from app.models.instance import Instance
        from app.models.session import SessionModel

        cleanup = self.SessionLocal()
        try:
            if session_ids:
                cleanup.query(SessionModel).filter(
                    SessionModel.id.in_(session_ids)
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

        admin_id = f"unit8-{uuid.uuid4().hex[:10]}"
        self._admin_ids.append(admin_id)
        self.db.add(Admin(id=admin_id, name="unit8 rt", tier="pro", active=True))
        self.db.flush()
        inst = Instance(
            admin_id=admin_id,
            instance_slug=f"i-{uuid.uuid4().hex[:8]}",
            display_name="unit8 rt instance",
        )
        self.db.add(inst)
        self.db.flush()
        return admin_id, inst

    def _make_session(
        self,
        *,
        admin_id: str,
        inst,
        control_mode: str = "luciel",
        channel: str = "widget",
        taken_over_at=None,
        taken_over_by_user_id=None,
    ):
        from app.models.session import SessionModel

        session_id = str(uuid.uuid4())
        self._session_ids.append(session_id)
        row = SessionModel(
            id=session_id,
            admin_id=admin_id,
            luciel_instance_id=inst.id,
            user_id=f"cust-{uuid.uuid4().hex[:8]}",
            channel=channel,
            status="active",
            control_mode=control_mode,
            taken_over_at=taken_over_at,
            taken_over_by_user_id=taken_over_by_user_id,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def _instance_service(self):
        from app.services.instance_service import InstanceService

        return InstanceService(self.db)

    def _audit_ctx(self):
        from app.repositories.admin_audit_repository import AuditContext

        return AuditContext.system("unit8-test")

    def _audit_rows(self, *, session_id: str, action: str):
        from app.models.admin_audit_log import AdminAuditLog

        return (
            self.db.query(AdminAuditLog)
            .filter(
                AdminAuditLog.action == action,
                AdminAuditLog.resource_natural_id == session_id,
            )
            .all()
        )

    # =================================================================
    # TAKEOVER
    # =================================================================

    def test_takeover_happy_path_transitions_to_human_controlled(self):
        from app.api.v1.admin_handoff import admin_takeover

        admin_id, inst = self._make_admin_instance()
        actor = uuid.uuid4()
        session = self._make_session(admin_id=admin_id, inst=inst)

        resp = admin_takeover(
            request=_request(admin_id=admin_id, actor_user_id=actor),
            session_id=session.id,
            db=self.db,
            instance_service=self._instance_service(),
            audit_ctx=self._audit_ctx(),
        )

        self.assertEqual(resp.control_mode, "human_controlled")
        self.assertEqual(resp.trigger, "admin_initiated")
        self.assertEqual(resp.taken_over_by_user_id, actor)
        self.assertIsNotNone(resp.taken_over_at)

        self.db.refresh(session)
        self.assertEqual(session.control_mode, "human_controlled")
        self.assertEqual(session.taken_over_by_user_id, actor)
        self.assertIsNotNone(session.taken_over_at)

    def test_takeover_writes_started_audit_row_with_trigger_and_actor(self):
        from app.api.v1.admin_handoff import admin_takeover
        from app.models.admin_audit_log import ACTION_HUMAN_TAKEOVER_STARTED

        admin_id, inst = self._make_admin_instance()
        actor = uuid.uuid4()
        session = self._make_session(admin_id=admin_id, inst=inst)

        admin_takeover(
            request=_request(admin_id=admin_id, actor_user_id=actor),
            session_id=session.id,
            db=self.db,
            instance_service=self._instance_service(),
            audit_ctx=self._audit_ctx(),
        )

        rows = self._audit_rows(
            session_id=session.id, action=ACTION_HUMAN_TAKEOVER_STARTED
        )
        self.assertEqual(len(rows), 1)
        after = rows[0].after_json
        self.assertEqual(after["trigger"], "admin_initiated")
        self.assertEqual(after["actor_user_id"], str(actor))
        self.assertEqual(rows[0].admin_id, admin_id)

    def test_takeover_idempotent_no_duplicate_transition_or_audit(self):
        from app.api.v1.admin_handoff import admin_takeover
        from app.models.admin_audit_log import ACTION_HUMAN_TAKEOVER_STARTED

        admin_id, inst = self._make_admin_instance()
        actor = uuid.uuid4()
        session = self._make_session(admin_id=admin_id, inst=inst)

        first = admin_takeover(
            request=_request(admin_id=admin_id, actor_user_id=actor),
            session_id=session.id,
            db=self.db,
            instance_service=self._instance_service(),
            audit_ctx=self._audit_ctx(),
        )
        first_taken_at = first.taken_over_at

        # Second call on an already-human_controlled session must NOT error
        # and must NOT create a second transition / audit row.
        second = admin_takeover(
            request=_request(admin_id=admin_id, actor_user_id=actor),
            session_id=session.id,
            db=self.db,
            instance_service=self._instance_service(),
            audit_ctx=self._audit_ctx(),
        )

        self.assertEqual(second.control_mode, "human_controlled")
        self.assertIn("idempotent", second.message.lower())
        # Timestamp not bumped — the original transition is preserved.
        self.assertEqual(second.taken_over_at, first_taken_at)

        self.db.refresh(session)
        self.assertEqual(session.control_mode, "human_controlled")

        rows = self._audit_rows(
            session_id=session.id, action=ACTION_HUMAN_TAKEOVER_STARTED
        )
        self.assertEqual(len(rows), 1, "idempotent re-takeover must not duplicate audit row")

    def test_takeover_tenant_isolation_admin_b_gets_404(self):
        from fastapi import HTTPException

        from app.api.v1.admin_handoff import admin_takeover

        # Session owned by admin A.
        admin_a, inst_a = self._make_admin_instance()
        session = self._make_session(admin_id=admin_a, inst=inst_a)

        # Admin B (a SEPARATE tenant, NOT platform_admin) must not see it.
        admin_b = f"unit8-b-{uuid.uuid4().hex[:8]}"
        self._admin_ids.append(admin_b)
        from app.models.admin import Admin

        self.db.add(Admin(id=admin_b, name="unit8 b", tier="pro", active=True))
        self.db.commit()

        req_b = _request(admin_id=admin_b)
        # Strip platform_admin so the cross-tenant guard actually runs.
        req_b.state.permissions = []

        with self.assertRaises(HTTPException) as caught:
            admin_takeover(
                request=req_b,
                session_id=session.id,
                db=self.db,
                instance_service=self._instance_service(),
                audit_ctx=self._audit_ctx(),
            )
        # 404 (NOT 403) so session-id existence is not leaked.
        self.assertEqual(caught.exception.status_code, 404)

        # The session was NOT taken over by the foreign request.
        self.db.refresh(session)
        self.assertEqual(session.control_mode, "luciel")

    # =================================================================
    # HANDBACK
    # =================================================================

    def test_handback_happy_path_duration_math_is_correct(self):
        from datetime import datetime, timedelta, timezone

        from app.api.v1.admin_handoff import admin_handback

        admin_id, inst = self._make_admin_instance()
        actor = uuid.uuid4()
        taken_at = datetime.now(timezone.utc) - timedelta(seconds=120)
        session = self._make_session(
            admin_id=admin_id,
            inst=inst,
            control_mode="human_controlled",
            taken_over_at=taken_at,
            taken_over_by_user_id=actor,
        )

        resp = admin_handback(
            request=_request(admin_id=admin_id, actor_user_id=actor),
            session_id=session.id,
            db=self.db,
            instance_service=self._instance_service(),
            audit_ctx=self._audit_ctx(),
        )

        self.assertEqual(resp.control_mode, "luciel")
        self.assertIsNotNone(resp.handed_back_at)
        # THE behavioral proof source-grep can't give: duration ~= 120s.
        self.assertIsNotNone(resp.duration_seconds)
        self.assertGreaterEqual(resp.duration_seconds, 110)
        self.assertLessEqual(resp.duration_seconds, 130)

        self.db.refresh(session)
        self.assertEqual(session.control_mode, "luciel")
        self.assertIsNotNone(session.handed_back_at)

    def test_handback_writes_ended_audit_row_with_duration_and_actor(self):
        from datetime import datetime, timedelta, timezone

        from app.api.v1.admin_handoff import admin_handback
        from app.models.admin_audit_log import ACTION_HUMAN_TAKEOVER_ENDED

        admin_id, inst = self._make_admin_instance()
        actor = uuid.uuid4()
        taken_at = datetime.now(timezone.utc) - timedelta(seconds=120)
        session = self._make_session(
            admin_id=admin_id,
            inst=inst,
            control_mode="human_controlled",
            taken_over_at=taken_at,
            taken_over_by_user_id=actor,
        )

        admin_handback(
            request=_request(admin_id=admin_id, actor_user_id=actor),
            session_id=session.id,
            db=self.db,
            instance_service=self._instance_service(),
            audit_ctx=self._audit_ctx(),
        )

        rows = self._audit_rows(
            session_id=session.id, action=ACTION_HUMAN_TAKEOVER_ENDED
        )
        self.assertEqual(len(rows), 1)
        after = rows[0].after_json
        self.assertEqual(after["actor_user_id"], str(actor))
        self.assertIsNotNone(after["duration_seconds"])
        self.assertGreaterEqual(after["duration_seconds"], 110)
        self.assertLessEqual(after["duration_seconds"], 130)

    def test_handback_409_when_not_human_controlled(self):
        from fastapi import HTTPException

        from app.api.v1.admin_handoff import admin_handback

        admin_id, inst = self._make_admin_instance()
        session = self._make_session(
            admin_id=admin_id, inst=inst, control_mode="luciel"
        )

        with self.assertRaises(HTTPException) as caught:
            admin_handback(
                request=_request(admin_id=admin_id),
                session_id=session.id,
                db=self.db,
                instance_service=self._instance_service(),
                audit_ctx=self._audit_ctx(),
            )
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["error"], "session_not_human_controlled"
        )

    # =================================================================
    # REPLY
    # =================================================================

    def test_reply_409_when_not_human_controlled(self):
        from fastapi import HTTPException

        from app.api.v1.admin_handoff import admin_reply, AdminReplyRequest

        admin_id, inst = self._make_admin_instance()
        session = self._make_session(
            admin_id=admin_id, inst=inst, control_mode="luciel"
        )

        with self.assertRaises(HTTPException) as caught:
            admin_reply(
                request=_request(admin_id=admin_id),
                session_id=session.id,
                body=AdminReplyRequest(body="hi there"),
                db=self.db,
                instance_service=self._instance_service(),
                audit_ctx=self._audit_ctx(),
            )
        self.assertEqual(caught.exception.status_code, 409)

    def test_reply_happy_path_dispatches_with_actor_and_channel(self):
        """Happy-path widget reply: MONKEYPATCH _dispatch_admin_reply to a
        fake recorder so no real provider send happens. Assert it was called
        with the admin's body + actor_user_id (attribution proof) and that
        the response channel matches the session channel."""
        from unittest.mock import patch

        from app.api.v1 import admin_handoff
        from app.api.v1.admin_handoff import admin_reply, AdminReplyRequest

        admin_id, inst = self._make_admin_instance()
        actor = uuid.uuid4()
        session = self._make_session(
            admin_id=admin_id,
            inst=inst,
            control_mode="human_controlled",
            channel="widget",
            taken_over_by_user_id=actor,
        )

        recorded = {}

        def _fake_dispatch(*, session, reply_body, actor_user_id):
            recorded["session_id"] = session.id
            recorded["channel"] = session.channel
            recorded["reply_body"] = reply_body
            recorded["actor_user_id"] = actor_user_id
            return {
                "provider_message_id": "fake-msg-123",
                "status": "sent",
                "channel": session.channel,
            }

        with patch.object(admin_handoff, "_dispatch_admin_reply", _fake_dispatch):
            resp = admin_reply(
                request=_request(admin_id=admin_id, actor_user_id=actor),
                session_id=session.id,
                body=AdminReplyRequest(body="Here is your answer."),
                db=self.db,
                instance_service=self._instance_service(),
                audit_ctx=self._audit_ctx(),
            )

        # Attribution proof: dispatched with the admin's body + actor_user_id
        # (NOT luciel_runtime).
        self.assertEqual(recorded["reply_body"], "Here is your answer.")
        self.assertEqual(recorded["actor_user_id"], actor)
        self.assertEqual(recorded["channel"], "widget")
        # Response carries the session channel + the dispatch status.
        self.assertEqual(resp.channel, session.channel)
        self.assertEqual(resp.channel, "widget")
        self.assertEqual(resp.status, "sent")
        self.assertEqual(resp.provider_message_id, "fake-msg-123")


if __name__ == "__main__":
    unittest.main()
