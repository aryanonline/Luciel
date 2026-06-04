"""RESCAN ALIGN(channel-lifecycle) — inbound lifecycle gate (real Postgres).

Architecture §3.6.2: a channel webhook (SMS/email) that resolves an
inbound to a NON-active instance must acknowledge with a 2xx no-op and
NOT route to the runtime — no reply, no budget accrual — while auditing
the drop with ACTION_CHANNEL_INBOUND_DROPPED. The widget path already
enforces this (chat_widget.py 204 gate); these tests cover the
store-and-forward channel path, which previously called
chat_service.respond unconditionally.

The gate lives once in app/channels/base.check_instance_lifecycle so all
channels inherit it. We test it two ways:

  * End-to-end through the Twilio SMS webhook route body (active →
    routed+replied; paused/deactivating/grace_window/missing → silent
    drop, respond NOT called, drop audited). respond is a spy so the
    "no routing → no budget accrual" guarantee is provable.
  * Directly at the channel layer (check_instance_lifecycle), which is
    channel-agnostic, so the email adapter is provably covered too.

Skipped unless DATABASE_URL points at a real Postgres (the audit
hash-chain handler is Postgres-only).
"""
from __future__ import annotations

import asyncio
import os
import types
import unittest
import uuid
from unittest.mock import MagicMock

os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("OPENAI_API_KEY", "dummy")

_DB_URL = os.environ.get("DATABASE_URL", "")
_LIVE = _DB_URL.startswith("postgresql+psycopg://") or bool(
    os.environ.get("LUCIEL_LIVE_POSTGRES_URL")
)


def _make_request(*, params: dict[str, str], signature: str = "sig"):
    """Build a real Starlette Request bound to a form payload.

    The ``@limiter.limit`` decorator on the webhook insists on a genuine
    ``starlette.requests.Request`` instance, so we construct one from a
    minimal ASGI scope and stub ``.form()`` to return the provided params
    (the SMS adapter is patched, so the form is only read for the audit
    ``To`` field on the drop path).
    """
    from starlette.datastructures import FormData
    from starlette.requests import Request

    headers = [
        (b"x-twilio-signature", signature.encode()),
        (b"host", b"luciel.test"),
    ]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/twilio/sms",
        "raw_path": b"/api/v1/twilio/sms",
        "query_string": b"",
        "headers": headers,
        "scheme": "https",
        "server": ("luciel.test", 443),
        "client": ("127.0.0.1", 12345),
    }
    req = Request(scope)

    async def _form():
        return FormData(params)

    req.form = _form  # type: ignore[method-assign]
    return req


@unittest.skipUnless(
    _LIVE,
    "Requires DATABASE_URL=postgresql+psycopg://... or LUCIEL_LIVE_POSTGRES_URL",
)
class TestChannelLifecycleGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.db.session import SessionLocal

        cls.SessionLocal = SessionLocal

    def setUp(self) -> None:
        from app.models.admin import Admin
        from app.models.instance import Instance
        from app.policy.entitlements import TIER_PRO

        self.db = self.SessionLocal()
        self.admin_id = f"clg-{uuid.uuid4().hex[:10]}"
        self.db.add(
            Admin(id=self.admin_id, name="clg", tier=TIER_PRO, active=True)
        )
        self.db.flush()
        self.instance = Instance(
            admin_id=self.admin_id,
            instance_slug=f"i-{uuid.uuid4().hex[:8]}",
            display_name="clg instance",
        )
        self.db.add(self.instance)
        self.db.flush()

    def tearDown(self) -> None:
        self.db.rollback()
        self.db.close()

    # -----------------------------------------------------------------
    # Helpers.
    # -----------------------------------------------------------------

    def _set_status(self, status) -> None:
        self.instance.instance_status = status
        self.db.flush()

    def _run_webhook(self, *, params, chat_service, session_service):
        """Invoke the webhook route body, patching the adapter so the
        signature/routing layer resolves to THIS instance deterministically.
        """
        from unittest.mock import patch

        from app.api.v1 import twilio_webhook
        from app.channels.base import InboundMessage, InstanceContext

        ctx = InstanceContext(
            admin_id=self.admin_id, instance_id=self.instance.id, session_id=None
        )
        inbound = InboundMessage(
            admin_id=self.admin_id,
            instance_id=self.instance.id,
            session_id=None,
            customer_identifier="+15559990000",
            body="hello",
            channel_metadata={"to": params.get("To"), "message_sid": "SM1"},
        )
        req = _make_request(params=params)
        with patch.object(
            twilio_webhook.SmsChannelAdapter, "verify_inbound", return_value=ctx
        ), patch.object(
            twilio_webhook.SmsChannelAdapter, "receive", return_value=inbound
        ), patch.object(
            twilio_webhook.SmsChannelAdapter,
            "send",
            return_value=types.SimpleNamespace(
                provider_message_id="SMfake", status="logged"
            ),
        ):
            return asyncio.run(
                twilio_webhook.receive_twilio_sms(
                    request=req,
                    db=self.db,
                    chat_service=chat_service,
                    session_service=session_service,
                )
            )

    def _count_drops(self) -> int:
        from app.models.admin_audit_log import (
            ACTION_CHANNEL_INBOUND_DROPPED,
            AdminAuditLog,
        )

        return (
            self.db.query(AdminAuditLog)
            .filter(
                AdminAuditLog.admin_id == self.admin_id,
                AdminAuditLog.action == ACTION_CHANNEL_INBOUND_DROPPED,
            )
            .count()
        )

    def _last_drop(self):
        from app.models.admin_audit_log import (
            ACTION_CHANNEL_INBOUND_DROPPED,
            AdminAuditLog,
        )

        return (
            self.db.query(AdminAuditLog)
            .filter(
                AdminAuditLog.admin_id == self.admin_id,
                AdminAuditLog.action == ACTION_CHANNEL_INBOUND_DROPPED,
            )
            .order_by(AdminAuditLog.id.desc())
            .first()
        )

    # -----------------------------------------------------------------
    # Happy path — ACTIVE instance routes + replies (unchanged).
    # -----------------------------------------------------------------

    def test_active_instance_routes_and_replies(self):
        from app.models.instance_status import InstanceStatus

        self._set_status(InstanceStatus.ACTIVE)
        chat = MagicMock()
        chat.respond.return_value = "the reply"
        session_service = MagicMock()
        session_service.create_session_with_identity.return_value = (
            types.SimpleNamespace(session=types.SimpleNamespace(id="sess-1"))
        )

        resp = self._run_webhook(
            params={"To": "+15550001111", "From": "+15559990000", "Body": "hi"},
            chat_service=chat,
            session_service=session_service,
        )

        self.assertEqual(resp.status_code, 204)
        chat.respond.assert_called_once()
        self.assertEqual(self._count_drops(), 0)

    # -----------------------------------------------------------------
    # THE bug — non-active states are silently dropped (no respond).
    # -----------------------------------------------------------------

    def _assert_silent_drop(self, status, expected_token: str):
        chat = MagicMock()
        session_service = MagicMock()

        resp = self._run_webhook(
            params={"To": "+15550001111", "From": "+15559990000", "Body": "hi"},
            chat_service=chat,
            session_service=session_service,
        )

        # 2xx no-op acknowledgement, consistent with the unresolvable drop.
        self.assertEqual(resp.status_code, 204)
        # No routing to the runtime → no budget accrual.
        chat.respond.assert_not_called()
        # Session resolution (and thus the budget path) is never reached.
        session_service.create_session_with_identity.assert_not_called()
        # Drop is audited with the lifecycle status note + instance_id.
        self.assertEqual(self._count_drops(), 1)
        drop = self._last_drop()
        self.assertEqual(drop.luciel_instance_id, self.instance.id)
        self.assertIn(f"status={expected_token}", drop.note or "")

    def test_paused_instance_is_silently_dropped(self):
        from app.models.instance_status import InstanceStatus

        self._set_status(InstanceStatus.PAUSED)
        self._assert_silent_drop(InstanceStatus.PAUSED, "paused")

    def test_deactivating_instance_is_silently_dropped(self):
        from app.models.instance_status import InstanceStatus

        self._set_status(InstanceStatus.DEACTIVATING)
        self._assert_silent_drop(InstanceStatus.DEACTIVATING, "deactivating")

    def test_grace_window_instance_is_silently_dropped(self):
        from app.models.instance_status import InstanceStatus

        self._set_status(InstanceStatus.GRACE_WINDOW)
        self._assert_silent_drop(InstanceStatus.GRACE_WINDOW, "grace_window")

    # -----------------------------------------------------------------
    # Channel-layer unit test — proves email (and any channel) is covered.
    # -----------------------------------------------------------------

    def test_check_instance_lifecycle_gate_directly(self):
        from app.channels.base import (
            InactiveInstanceDrop,
            InstanceContext,
            check_instance_lifecycle,
        )
        from app.models.instance_status import InstanceStatus

        ctx = InstanceContext(
            admin_id=self.admin_id, instance_id=self.instance.id, session_id=None
        )

        # ACTIVE → None (caller proceeds to runtime).
        self._set_status(InstanceStatus.ACTIVE)
        self.assertIsNone(check_instance_lifecycle(self.db, ctx))

        # Each non-active state → an InactiveInstanceDrop carrying the token.
        for status in (
            InstanceStatus.PAUSED,
            InstanceStatus.DEACTIVATING,
            InstanceStatus.GRACE_WINDOW,
        ):
            self._set_status(status)
            drop = check_instance_lifecycle(self.db, ctx)
            self.assertIsInstance(drop, InactiveInstanceDrop)
            self.assertEqual(drop.instance_id, self.instance.id)
            self.assertEqual(drop.status, status.value)

        # Missing instance row → "missing" drop (fail-closed).
        missing_ctx = InstanceContext(
            admin_id=self.admin_id, instance_id=999_000_111, session_id=None
        )
        drop = check_instance_lifecycle(self.db, missing_ctx)
        self.assertIsInstance(drop, InactiveInstanceDrop)
        self.assertEqual(drop.status, "missing")

    # -----------------------------------------------------------------
    # Unresolvable inbound — existing behavior preserved (regression).
    # -----------------------------------------------------------------

    def test_unresolvable_inbound_preserves_existing_drop(self):
        from unittest.mock import patch

        from app.api.v1 import twilio_webhook
        from app.channels.base import UnresolvableInboundError

        chat = MagicMock()
        session_service = MagicMock()
        req = _make_request(
            params={"To": "+15550001111", "From": "+15559990000", "Body": "hi"}
        )
        with patch.object(
            twilio_webhook.SmsChannelAdapter,
            "verify_inbound",
            side_effect=UnresolvableInboundError("no route"),
        ):
            resp = asyncio.run(
                twilio_webhook.receive_twilio_sms(
                    request=req,
                    db=self.db,
                    chat_service=chat,
                    session_service=session_service,
                )
            )

        self.assertEqual(resp.status_code, 204)
        chat.respond.assert_not_called()
        # The unresolvable drop keeps its EXISTING shape: audited under the
        # same action verb but recorded against the "platform" actor (no
        # instance was resolved), distinct from the lifecycle drop which
        # carries the resolved admin/instance. autocommit=True so it is
        # committed; read it back fresh.
        from app.models.admin_audit_log import (
            ACTION_CHANNEL_INBOUND_DROPPED,
            AdminAuditLog,
        )

        self.db.rollback()
        row = (
            self.db.query(AdminAuditLog)
            .filter(
                AdminAuditLog.admin_id == "platform",
                AdminAuditLog.action == ACTION_CHANNEL_INBOUND_DROPPED,
                AdminAuditLog.note.ilike("%unresolvable%"),
            )
            .order_by(AdminAuditLog.id.desc())
            .first()
        )
        self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
