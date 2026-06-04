"""Rescan Tier-C — EscalationDeliveryService tests.

Covers:
  - Adapter unit tests (email/sms/slack format + dry-run records, no real
    send under flag=False).
  - Delivery idempotency: same (session,signal,gate) delivered once even
    on replay.
  - Pro fan-out: one signal -> multiple contacts.
  - Enterprise chain: step advance on SLA timeout; ack stops the chain;
    owner fallback at end; each transition writes escalation_chain_step audit.
  - Retry: 3 failures -> Pro owner-fallback + delivery_failed audit;
    Enterprise -> advance.
  - Orchestrator integration: firing a high_value_lead signal triggers a
    delivery attempt + the escalation_notification_sent audit row (in dry-run).
  - §3.5.6 audit event-type constants exist.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch, call

from app.models.admin_audit_log import (
    ACTION_ESCALATION_NOTIFICATION_SENT,
    ACTION_ESCALATION_DELIVERY_FAILED,
    ACTION_ESCALATION_CHAIN_STEP,
    ACTION_ESCALATION_ACKED,
    ACTION_ESCALATION_CHAIN_END_FALLBACK,
)
from app.models.escalation_event import (
    GATE_INTAKE,
    GATE_OUTCOME,
    SIGNAL_HIGH_VALUE_LEAD,
    SIGNAL_EXPLICIT_HUMAN_REQUEST,
    SIGNAL_STRONG_NEGATIVE_SENTIMENT,
    SIGNAL_CANNOT_CONFIDENTLY_ANSWER,
    SIGNAL_BUDGET_EXHAUSTED,
    DELIVERY_STATUS_PENDING,
    DELIVERY_STATUS_DELIVERED,
    DELIVERY_STATUS_ACKED,
)
from app.notifications.base import NotificationAdapter, NotificationResult
from app.notifications.email_notifier import EmailNotificationAdapter
from app.notifications.sms_notifier import SmsNotificationAdapter
from app.notifications.slack_notifier import SlackNotificationAdapter
from app.policy.escalation_routing import EscalationContact, NOTIFY_EMAIL, NOTIFY_SMS, NOTIFY_SLACK
from app.policy.entitlements import TIER_FREE, TIER_PRO, TIER_ENTERPRISE
from app.services.escalation_delivery_service import EscalationDeliveryService


# ===========================================================================
# Audit event constants exist (§3.5.6)
# ===========================================================================


class TestAuditEventConstants(unittest.TestCase):
    """Verify all §3.5.6 constants are declared and in ALLOWED_ACTIONS."""

    def test_all_tier_c_constants_declared(self):
        self.assertEqual(ACTION_ESCALATION_NOTIFICATION_SENT, "escalation_notification_sent")
        self.assertEqual(ACTION_ESCALATION_DELIVERY_FAILED, "escalation_delivery_failed")
        self.assertEqual(ACTION_ESCALATION_CHAIN_STEP, "escalation_chain_step")
        self.assertEqual(ACTION_ESCALATION_ACKED, "escalation_acked")
        self.assertEqual(ACTION_ESCALATION_CHAIN_END_FALLBACK, "escalation_chain_end_fallback")

    def test_all_tier_c_constants_in_allowed_actions(self):
        from app.models.admin_audit_log import ALLOWED_ACTIONS
        self.assertIn(ACTION_ESCALATION_NOTIFICATION_SENT, ALLOWED_ACTIONS)
        self.assertIn(ACTION_ESCALATION_DELIVERY_FAILED, ALLOWED_ACTIONS)
        self.assertIn(ACTION_ESCALATION_CHAIN_STEP, ALLOWED_ACTIONS)
        self.assertIn(ACTION_ESCALATION_ACKED, ALLOWED_ACTIONS)
        self.assertIn(ACTION_ESCALATION_CHAIN_END_FALLBACK, ALLOWED_ACTIONS)

    def test_delivery_status_constants_declared(self):
        self.assertEqual(DELIVERY_STATUS_PENDING, "pending")
        self.assertEqual(DELIVERY_STATUS_DELIVERED, "delivered")
        self.assertEqual(DELIVERY_STATUS_ACKED, "acked")


# ===========================================================================
# Adapter unit tests — dry-run (no real send when flag is off)
# ===========================================================================


class _AlwaysFailAdapter(NotificationAdapter):
    """Returns a failure result on every send, for retry testing."""
    channel = "email"

    def send(self, **kwargs) -> NotificationResult:
        return NotificationResult(channel=self.channel, to=kwargs.get("to"), sent=False, error="boom")


class _AlwaysSucceedAdapter(NotificationAdapter):
    """Returns a sent=True result on every send."""
    channel = "email"
    results: list[NotificationResult]

    def __init__(self, channel="email"):
        self.channel = channel
        self.results = []

    def send(self, **kwargs) -> NotificationResult:
        r = NotificationResult(channel=self.channel, to=kwargs.get("to"), sent=True)
        self.results.append(r)
        return r


class TestEmailAdapterDryRun(unittest.TestCase):

    def test_email_dry_run_when_log_transport(self):
        with patch.dict("os.environ", {"LUCIEL_EMAIL_TRANSPORT": "log"}):
            adapter = EmailNotificationAdapter()
            result = adapter.send(
                to="test@example.com",
                subject="Escalation",
                body="body",
                signal=SIGNAL_HIGH_VALUE_LEAD,
                session_id="sess-abc",
            )
        self.assertTrue(result.dry_run)
        self.assertFalse(result.sent)
        self.assertEqual(result.channel, "email")

    def test_email_dry_run_when_no_recipient(self):
        adapter = EmailNotificationAdapter()
        result = adapter.send(
            to=None,
            subject="sub",
            body="body",
            signal=SIGNAL_HIGH_VALUE_LEAD,
            session_id="sess-1",
        )
        self.assertTrue(result.dry_run)
        self.assertFalse(result.sent)
        self.assertEqual(result.extra["reason"], "no_recipient")


class TestSmsAdapterDryRun(unittest.TestCase):

    def test_sms_dry_run_when_live_switch_off(self):
        with patch("app.core.config.settings.channels_live_provisioning_enabled", False):
            adapter = SmsNotificationAdapter()
            result = adapter.send(
                to="+16135551234",
                subject="sub",
                body="body",
                signal=SIGNAL_HIGH_VALUE_LEAD,
                session_id="sess-sms",
            )
        self.assertTrue(result.dry_run)
        self.assertFalse(result.sent)
        self.assertIsNotNone(result.provider_id)
        self.assertIn("SMfake", result.provider_id)

    def test_sms_dry_run_when_no_recipient(self):
        adapter = SmsNotificationAdapter()
        result = adapter.send(
            to=None,
            subject="sub",
            body="body",
            signal=SIGNAL_HIGH_VALUE_LEAD,
            session_id="sess-sms2",
        )
        self.assertTrue(result.dry_run)
        self.assertEqual(result.extra["reason"], "no_recipient")


class TestSlackAdapterDryRun(unittest.TestCase):

    def test_slack_dry_run_when_live_switch_off(self):
        with patch("app.core.config.settings.channels_live_provisioning_enabled", False):
            adapter = SlackNotificationAdapter()
            result = adapter.send(
                to="https://hooks.slack.com/services/xxx",
                subject="sub",
                body="body",
                signal=SIGNAL_HIGH_VALUE_LEAD,
                session_id="sess-slack",
            )
        self.assertTrue(result.dry_run)
        self.assertFalse(result.sent)

    def test_slack_dry_run_when_no_webhook(self):
        adapter = SlackNotificationAdapter()
        result = adapter.send(
            to=None,
            subject="sub",
            body="body",
            signal=SIGNAL_HIGH_VALUE_LEAD,
            session_id="sess-slack2",
        )
        self.assertTrue(result.dry_run)
        self.assertEqual(result.extra["reason"], "no_webhook_url")


# ===========================================================================
# Fake session + audit helpers for delivery service tests
# ===========================================================================


class _FakeEventRow:
    def __init__(self, *, delivery_status=DELIVERY_STATUS_PENDING):
        self.delivery_status = delivery_status


class _FakeDeliveryDB:
    """Minimal fake session for EscalationDeliveryService tests."""

    def __init__(self, *, tier=TIER_FREE, event_delivery_status=DELIVERY_STATUS_PENDING):
        self._tier = tier
        self._event_delivery_status = event_delivery_status
        self.committed = False
        self.closed = False
        self.audit_writes: list[dict] = []
        self._updated_status: str | None = None

    # resolve_contact query (Admin.tier)
    def execute(self, stmt, *args, **kwargs):
        # Handles both scalar queries (one-column selects returning one row)
        # and update statements.
        stmt_str = str(stmt)

        class _ScalarRes:
            def __init__(self_, value):
                self_._value = value
            def scalar_one_or_none(self_):
                return self_._value
            def scalars(self_):
                class _Scalars:
                    def first(self__):
                        return None
                return _Scalars()

        # delivery_status lookup
        if "delivery_status" in stmt_str and "UPDATE" not in stmt_str.upper():
            return _ScalarRes(self._event_delivery_status)
        # Admin.tier lookup
        if "admins" in stmt_str:
            return _ScalarRes(self._tier)
        # Instance escalation_config
        if "escalation_config" in stmt_str:
            return _ScalarRes(None)
        # Subscription customer_email
        if "customer_email" in stmt_str:
            class _Sub:
                def scalars(self_):
                    class _S:
                        def first(self__):
                            return "admin@example.com"
                    return _S()
            return _Sub()
        # UPDATE delivery_status
        if "UPDATE" in stmt_str.upper():
            # Store the status update
            return None
        return _ScalarRes(None)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        self.closed = True

    def add(self, row):
        pass

    def flush(self):
        pass


def _make_contact(tier: str, channels: tuple = None) -> EscalationContact:
    if channels is None:
        from app.policy.escalation_routing import channels_for_tier
        channels = channels_for_tier(tier)
    return EscalationContact(
        admin_id="admin-1",
        tier=tier,
        channels=channels,
    )


# ===========================================================================
# Delivery idempotency
# ===========================================================================


class TestDeliveryIdempotency(unittest.TestCase):

    def test_already_delivered_skips_send(self):
        """If delivery_status='delivered', no send is made on replay."""
        calls = []
        email_adapter = _AlwaysSucceedAdapter()

        db = _FakeDeliveryDB(
            tier=TIER_FREE,
            event_delivery_status=DELIVERY_STATUS_DELIVERED,
        )
        svc = EscalationDeliveryService(
            session_factory=lambda: db,
            email_adapter=email_adapter,
        )
        svc.deliver(
            event_id=42,
            admin_id="admin-1",
            luciel_instance_id=None,
            session_id="sess-idem",
            signal=SIGNAL_HIGH_VALUE_LEAD,
            gate=GATE_OUTCOME,
            contact=_make_contact(TIER_FREE),
        )
        # No send attempted — idempotency kicked in.
        self.assertEqual(len(email_adapter.results), 0)

    def test_pending_status_allows_send(self):
        """If delivery_status='pending', send is attempted."""
        email_adapter = _AlwaysSucceedAdapter()

        db = _FakeDeliveryDB(
            tier=TIER_FREE,
            event_delivery_status=DELIVERY_STATUS_PENDING,
        )
        svc = EscalationDeliveryService(
            session_factory=lambda: db,
            email_adapter=email_adapter,
        )
        svc.deliver(
            event_id=99,
            admin_id="admin-1",
            luciel_instance_id=None,
            session_id="sess-pending",
            signal=SIGNAL_EXPLICIT_HUMAN_REQUEST,
            gate=GATE_INTAKE,
            contact=_make_contact(TIER_FREE),
        )
        # One send attempted.
        self.assertEqual(len(email_adapter.results), 1)
        self.assertTrue(email_adapter.results[0].sent)


# ===========================================================================
# Pro fan-out
# ===========================================================================


class TestProFanOut(unittest.TestCase):

    def test_pro_fan_out_multiple_contacts(self):
        """One signal -> multiple contacts in routing_rules."""
        email_adapter = _AlwaysSucceedAdapter(channel="email")
        sms_adapter = _AlwaysSucceedAdapter(channel="sms")

        # Pro config with two contacts for high_value_lead.
        escalation_config = {
            "routing_rules": {
                SIGNAL_HIGH_VALUE_LEAD: [
                    {"channel": "email", "value": "sales@example.com"},
                    {"channel": "sms", "value": "+16135559999"},
                ]
            }
        }

        class _DBWithConfig(_FakeDeliveryDB):
            def execute(self_, stmt, *args, **kwargs):
                stmt_str = str(stmt)
                class _ScalarRes:
                    def __init__(self__, value):
                        self__._value = value
                    def scalar_one_or_none(self__):
                        return self__._value
                    def scalars(self__):
                        class _S:
                            def first(self___):
                                return "billing@example.com"
                        return _S()
                if "delivery_status" in stmt_str and "UPDATE" not in stmt_str.upper():
                    return _ScalarRes(DELIVERY_STATUS_PENDING)
                if "escalation_config" in stmt_str:
                    return _ScalarRes(escalation_config)
                if "customer_email" in stmt_str:
                    class _Sub:
                        def scalars(self__):
                            class _S:
                                def first(self___):
                                    return "billing@example.com"
                            return _S()
                    return _Sub()
                return _ScalarRes(None)

        db = _DBWithConfig(tier=TIER_PRO)
        svc = EscalationDeliveryService(
            session_factory=lambda: db,
            email_adapter=email_adapter,
            sms_adapter=sms_adapter,
        )
        svc.deliver(
            event_id=10,
            admin_id="admin-1",
            luciel_instance_id=7,
            session_id="sess-pro",
            signal=SIGNAL_HIGH_VALUE_LEAD,
            gate=GATE_OUTCOME,
            contact=_make_contact(TIER_PRO),
        )
        # Email and SMS both sent.
        self.assertEqual(len(email_adapter.results), 1)
        self.assertEqual(len(sms_adapter.results), 1)
        self.assertEqual(email_adapter.results[0].to, "sales@example.com")
        self.assertEqual(sms_adapter.results[0].to, "+16135559999")


# ===========================================================================
# Retry behaviour
# ===========================================================================


class TestRetry(unittest.TestCase):

    def test_pro_email_fallback_after_3_sms_failures(self):
        """Pro: 3 SMS send failures -> fall back to admin_owner email."""
        email_adapter = _AlwaysSucceedAdapter(channel="email")
        fail_sms_count = [0]

        class _FailSmsAdapter(NotificationAdapter):
            channel = "sms"
            def send(self_, **kwargs) -> NotificationResult:
                fail_sms_count[0] += 1
                return NotificationResult(channel="sms", to=kwargs.get("to"), sent=False, error="sms_fail")

        # Pro config with SMS contact only.
        escalation_config = {
            "primary_contact": {"channel": "sms", "value": "+16135551111"},
        }

        class _DBPro(_FakeDeliveryDB):
            def execute(self_, stmt, *args, **kwargs):
                stmt_str = str(stmt)
                class _ScalarRes:
                    def __init__(self__, value):
                        self__._value = value
                    def scalar_one_or_none(self__):
                        return self__._value
                    def scalars(self__):
                        class _S:
                            def first(self___):
                                return "billing@example.com"
                        return _S()
                if "delivery_status" in stmt_str and "UPDATE" not in stmt_str.upper():
                    return _ScalarRes(DELIVERY_STATUS_PENDING)
                if "escalation_config" in stmt_str:
                    return _ScalarRes(escalation_config)
                if "customer_email" in stmt_str:
                    class _Sub:
                        def scalars(self__):
                            class _S:
                                def first(self___):
                                    return "billing@example.com"
                            return _S()
                    return _Sub()
                return _ScalarRes(None)

        db = _DBPro(tier=TIER_PRO)
        svc = EscalationDeliveryService(
            session_factory=lambda: db,
            email_adapter=email_adapter,
            sms_adapter=_FailSmsAdapter(),
        )

        # Patch time.sleep to avoid actual waiting.
        with patch("app.services.escalation_delivery_service.time.sleep"):
            svc.deliver(
                event_id=20,
                admin_id="admin-1",
                luciel_instance_id=7,
                session_id="sess-retry",
                signal=SIGNAL_HIGH_VALUE_LEAD,
                gate=GATE_OUTCOME,
                contact=_make_contact(TIER_PRO),
            )

        # SMS failed 3 times (immediate + 2 retries).
        self.assertEqual(fail_sms_count[0], 3)
        # Fallback email was sent.
        self.assertEqual(len(email_adapter.results), 1)
        self.assertTrue(email_adapter.results[0].sent)

    def test_free_delivery_failed_audit_after_3_failures(self):
        """Free: 3 email failures -> delivery_failed audit row."""
        audit_actions = []

        class _FailEmailAdapter(NotificationAdapter):
            channel = "email"
            def send(self_, **kwargs) -> NotificationResult:
                return NotificationResult(channel="email", to=kwargs.get("to"), sent=False, error="ses_fail")

        class _AuditCapturingDB(_FakeDeliveryDB):
            def execute(self_, stmt, *args, **kwargs):
                stmt_str = str(stmt)
                class _ScalarRes:
                    def __init__(self__, value):
                        self__._value = value
                    def scalar_one_or_none(self__):
                        return self__._value
                    def scalars(self__):
                        class _S:
                            def first(self___):
                                return "billing@example.com"
                        return _S()
                if "delivery_status" in stmt_str and "UPDATE" not in stmt_str.upper():
                    return _ScalarRes(DELIVERY_STATUS_PENDING)
                if "customer_email" in stmt_str:
                    class _Sub:
                        def scalars(self__):
                            class _S:
                                def first(self___):
                                    return "billing@example.com"
                            return _S()
                    return _Sub()
                return _ScalarRes(None)

        def _fake_record(_self_repo, *, action, **kwargs):
            audit_actions.append(action)

        db = _AuditCapturingDB(tier=TIER_FREE)
        svc = EscalationDeliveryService(
            session_factory=lambda: db,
            email_adapter=_FailEmailAdapter(),
        )

        with patch("app.repositories.admin_audit_repository.AdminAuditRepository.record", _fake_record), \
             patch("app.services.escalation_delivery_service.time.sleep"):
            svc.deliver(
                event_id=30,
                admin_id="admin-1",
                luciel_instance_id=None,
                session_id="sess-fail",
                signal=SIGNAL_CANNOT_CONFIDENTLY_ANSWER,
                gate=GATE_OUTCOME,
                contact=_make_contact(TIER_FREE),
            )

        self.assertIn(ACTION_ESCALATION_DELIVERY_FAILED, audit_actions)


# ===========================================================================
# Enterprise chain walker
# ===========================================================================


class TestEnterpriseChain(unittest.TestCase):

    def _make_chain(self):
        return [
            {"channel": "email", "value": "step0@example.com", "sla_minutes": 5},
            {"channel": "sms", "value": "+16135550000", "sla_minutes": 5},
        ]

    def test_enterprise_chain_step_audit_written_on_step0(self):
        """Enterprise step 0 notification writes escalation_chain_step audit."""
        audit_actions = []
        escalation_config = {"chains": self._make_chain()}
        email_adapter = _AlwaysSucceedAdapter(channel="email")

        class _DBEnt(_FakeDeliveryDB):
            def execute(self_, stmt, *args, **kwargs):
                stmt_str = str(stmt)
                class _ScalarRes:
                    def __init__(self__, value):
                        self__._value = value
                    def scalar_one_or_none(self__):
                        return self__._value
                    def scalars(self__):
                        class _S:
                            def first(self___):
                                return "billing@example.com"
                        return _S()
                if "delivery_status" in stmt_str and "UPDATE" not in stmt_str.upper():
                    return _ScalarRes(DELIVERY_STATUS_PENDING)
                if "escalation_config" in stmt_str:
                    return _ScalarRes(escalation_config)
                if "customer_email" in stmt_str:
                    class _Sub:
                        def scalars(self__):
                            class _S:
                                def first(self___):
                                    return "billing@example.com"
                            return _S()
                    return _Sub()
                return _ScalarRes(None)

        def _fake_record(_self_repo, *, action, **kwargs):
            audit_actions.append(action)

        db = _DBEnt(tier=TIER_ENTERPRISE)
        svc = EscalationDeliveryService(
            session_factory=lambda: db,
            email_adapter=email_adapter,
        )

        with patch("app.repositories.admin_audit_repository.AdminAuditRepository.record", _fake_record), \
             patch.object(svc, "_enqueue_chain_advance"):
            svc.deliver(
                event_id=50,
                admin_id="admin-1",
                luciel_instance_id=7,
                session_id="sess-ent",
                signal=SIGNAL_HIGH_VALUE_LEAD,
                gate=GATE_OUTCOME,
                contact=_make_contact(TIER_ENTERPRISE),
            )

        self.assertIn(ACTION_ESCALATION_NOTIFICATION_SENT, audit_actions)
        self.assertIn(ACTION_ESCALATION_CHAIN_STEP, audit_actions)

    def test_enterprise_chain_enqueues_sla_task(self):
        """Enterprise step 0 enqueues a Celery SLA advance task."""
        enqueue_calls = []
        escalation_config = {"chains": self._make_chain()}
        email_adapter = _AlwaysSucceedAdapter(channel="email")

        class _DBEnt2(_FakeDeliveryDB):
            def execute(self_, stmt, *args, **kwargs):
                stmt_str = str(stmt)
                class _ScalarRes:
                    def __init__(self__, value):
                        self__._value = value
                    def scalar_one_or_none(self__):
                        return self__._value
                    def scalars(self__):
                        class _S:
                            def first(self___):
                                return "billing@example.com"
                        return _S()
                if "delivery_status" in stmt_str and "UPDATE" not in stmt_str.upper():
                    return _ScalarRes(DELIVERY_STATUS_PENDING)
                if "escalation_config" in stmt_str:
                    return _ScalarRes(escalation_config)
                if "customer_email" in stmt_str:
                    class _Sub:
                        def scalars(self__):
                            class _S:
                                def first(self___):
                                    return "billing@example.com"
                            return _S()
                    return _Sub()
                return _ScalarRes(None)

        db = _DBEnt2(tier=TIER_ENTERPRISE)
        svc = EscalationDeliveryService(
            session_factory=lambda: db,
            email_adapter=email_adapter,
        )

        def _fake_enqueue(**kwargs):
            enqueue_calls.append(kwargs)

        with patch("app.repositories.admin_audit_repository.AdminAuditRepository.record", lambda *a, **k: None), \
             patch.object(svc, "_enqueue_chain_advance", side_effect=_fake_enqueue):
            svc.deliver(
                event_id=51,
                admin_id="admin-1",
                luciel_instance_id=7,
                session_id="sess-ent2",
                signal=SIGNAL_HIGH_VALUE_LEAD,
                gate=GATE_OUTCOME,
                contact=_make_contact(TIER_ENTERPRISE),
            )

        self.assertEqual(len(enqueue_calls), 1)
        call_kw = enqueue_calls[0]
        self.assertEqual(call_kw["event_id"], 51)
        self.assertEqual(call_kw["current_step"], 0)

    def test_enterprise_no_chain_degrades_to_pro_fanout(self):
        """Enterprise with no chains config degrades to Pro fan-out."""
        email_adapter = _AlwaysSucceedAdapter(channel="email")

        class _DBEntNoCfg(_FakeDeliveryDB):
            def execute(self_, stmt, *args, **kwargs):
                stmt_str = str(stmt)
                class _ScalarRes:
                    def __init__(self__, value):
                        self__._value = value
                    def scalar_one_or_none(self__):
                        return self__._value
                    def scalars(self__):
                        class _S:
                            def first(self___):
                                return "billing@example.com"
                        return _S()
                if "delivery_status" in stmt_str and "UPDATE" not in stmt_str.upper():
                    return _ScalarRes(DELIVERY_STATUS_PENDING)
                if "escalation_config" in stmt_str:
                    return _ScalarRes(None)  # No escalation config
                if "customer_email" in stmt_str:
                    class _Sub:
                        def scalars(self__):
                            class _S:
                                def first(self___):
                                    return "billing@example.com"
                            return _S()
                    return _Sub()
                return _ScalarRes(None)

        db = _DBEntNoCfg(tier=TIER_ENTERPRISE)
        svc = EscalationDeliveryService(
            session_factory=lambda: db,
            email_adapter=email_adapter,
        )

        with patch("app.repositories.admin_audit_repository.AdminAuditRepository.record", lambda *a, **k: None):
            svc.deliver(
                event_id=52,
                admin_id="admin-1",
                luciel_instance_id=7,
                session_id="sess-ent-nocfg",
                signal=SIGNAL_HIGH_VALUE_LEAD,
                gate=GATE_OUTCOME,
                contact=_make_contact(TIER_ENTERPRISE),
            )

        # Degraded to email fan-out.
        self.assertEqual(len(email_adapter.results), 1)


# ===========================================================================
# Orchestrator integration: high_value_lead triggers delivery
# ===========================================================================


class TestOrchestratorIntegration(unittest.TestCase):
    """Firing a high_value_lead signal triggers delivery + audit row (dry-run)."""

    def test_high_value_lead_fires_delivery_service(self):
        from app.policy.escalation import EscalationDecision, EscalationRouting
        from app.runtime.orchestrator import LucielOrchestrator

        delivered_calls = []

        class _FakeDeliverySvc:
            def deliver(self_, **kwargs):
                delivered_calls.append(kwargs)

        class _FakeEscalationSvc:
            def record_escalation(self_, decision):
                return EscalationRouting(
                    tier=TIER_FREE,
                    channels=(NOTIFY_EMAIL,),
                    notified_live=False,
                    event_id=101,
                )

        decision = EscalationDecision(
            signal=SIGNAL_HIGH_VALUE_LEAD,
            gate=GATE_OUTCOME,
            admin_id="admin-1",
            session_id="sess-orch",
            luciel_instance_id=5,
        )

        orch = LucielOrchestrator(
            escalation_service=_FakeEscalationSvc(),
            escalation_delivery_service=_FakeDeliverySvc(),
        )
        orch._record_escalation_best_effort(decision)

        self.assertEqual(len(delivered_calls), 1)
        call_kw = delivered_calls[0]
        self.assertEqual(call_kw["signal"], SIGNAL_HIGH_VALUE_LEAD)
        self.assertEqual(call_kw["session_id"], "sess-orch")
        self.assertEqual(call_kw["event_id"], 101)

    def test_budget_exhausted_signal_does_not_fire_delivery_service(self):
        """budget_exhausted uses the existing budget alert path, NOT delivery."""
        from app.policy.escalation import EscalationDecision, EscalationRouting
        from app.runtime.orchestrator import LucielOrchestrator

        delivered_calls = []

        class _FakeDeliverySvc:
            def deliver(self_, **kwargs):
                delivered_calls.append(kwargs)

        class _FakeEscalationSvc:
            def record_escalation(self_, decision):
                return EscalationRouting(
                    tier=TIER_FREE,
                    channels=(NOTIFY_EMAIL,),
                    event_id=200,
                )

        decision = EscalationDecision(
            signal=SIGNAL_BUDGET_EXHAUSTED,
            gate=GATE_INTAKE,
            admin_id="admin-2",
            session_id="sess-budget",
        )

        orch = LucielOrchestrator(
            escalation_service=_FakeEscalationSvc(),
            escalation_delivery_service=_FakeDeliverySvc(),
        )
        orch._record_escalation_best_effort(decision)

        # Delivery service NOT called for budget_exhausted.
        self.assertEqual(len(delivered_calls), 0)

    def test_delivery_never_crashes_turn_on_exception(self):
        """If delivery service raises, the turn is unaffected."""
        from app.policy.escalation import EscalationDecision, EscalationRouting
        from app.runtime.orchestrator import LucielOrchestrator

        class _BoomDeliverySvc:
            def deliver(self_, **kwargs):
                raise RuntimeError("delivery exploded")

        class _FakeEscalationSvc:
            def record_escalation(self_, decision):
                return EscalationRouting(
                    tier=TIER_FREE,
                    channels=(NOTIFY_EMAIL,),
                    event_id=300,
                )

        decision = EscalationDecision(
            signal=SIGNAL_EXPLICIT_HUMAN_REQUEST,
            gate=GATE_INTAKE,
            admin_id="admin-3",
            session_id="sess-crash",
        )

        orch = LucielOrchestrator(
            escalation_service=_FakeEscalationSvc(),
            escalation_delivery_service=_BoomDeliverySvc(),
        )
        # Must not raise.
        orch._record_escalation_best_effort(decision)

    def test_escalation_notification_sent_audit_emitted_in_dry_run(self):
        """Dry-run: delivery service records escalation_notification_sent audit."""
        audit_actions = []

        def _fake_record(_self_repo, *, action, **kwargs):
            audit_actions.append(action)

        email_adapter = _AlwaysSucceedAdapter(channel="email")
        # email_adapter.send returns sent=True (simulates log-transport via patch)

        class _PendingDB(_FakeDeliveryDB):
            pass

        db = _PendingDB(tier=TIER_FREE, event_delivery_status=DELIVERY_STATUS_PENDING)
        svc = EscalationDeliveryService(
            session_factory=lambda: db,
            email_adapter=email_adapter,
        )

        with patch("app.repositories.admin_audit_repository.AdminAuditRepository.record", _fake_record):
            svc.deliver(
                event_id=400,
                admin_id="admin-4",
                luciel_instance_id=None,
                session_id="sess-audit",
                signal=SIGNAL_HIGH_VALUE_LEAD,
                gate=GATE_OUTCOME,
                contact=_make_contact(TIER_FREE),
            )

        self.assertIn(ACTION_ESCALATION_NOTIFICATION_SENT, audit_actions)


# ===========================================================================
# Chain walker unit tests
# ===========================================================================


class TestChainWalker(unittest.TestCase):
    """Unit tests for the Celery chain walker logic."""

    def test_ack_stops_chain(self):
        """If delivery_status='acked', chain walker writes ack audit and stops."""
        from app.worker.tasks.escalation_chain_walker import _advance

        audit_actions = []

        def _fake_record(_self_repo, *, action, **kwargs):
            audit_actions.append(action)

        chain = [
            {"channel": "email", "value": "step0@example.com", "sla_minutes": 5},
            {"channel": "email", "value": "step1@example.com", "sla_minutes": 5},
        ]

        class _AckedDB:
            def execute(self, stmt, *args, **kwargs):
                stmt_str = str(stmt)
                class _ScalarRes:
                    def scalar_one_or_none(self_):
                        return DELIVERY_STATUS_ACKED
                return _ScalarRes()
            def commit(self): pass
            def close(self): pass

        with patch("app.db.session.SessionLocal", return_value=_AckedDB()), \
             patch("app.repositories.admin_audit_repository.AdminAuditRepository.record", _fake_record):
            _advance(
                event_id=501,
                admin_id="admin-5",
                luciel_instance_id=None,
                session_id="sess-ack",
                signal=SIGNAL_HIGH_VALUE_LEAD,
                gate=GATE_OUTCOME,
                current_step=0,
                chain=chain,
                email_to="fallback@example.com",
                subject="Test",
                body="body",
            )

        self.assertIn(ACTION_ESCALATION_ACKED, audit_actions)
        # escalation_chain_step NOT called (chain stopped at ack).
        self.assertNotIn(ACTION_ESCALATION_CHAIN_STEP, audit_actions)

    def test_chain_exhausted_writes_fallback_audit(self):
        """Chain walker at last step writes escalation_chain_end_fallback."""
        from app.worker.tasks.escalation_chain_walker import _advance

        audit_actions = []

        def _fake_record(_self_repo, *, action, **kwargs):
            audit_actions.append(action)

        # Single-step chain; current_step=0, next_step=1 = exhausted.
        chain = [
            {"channel": "email", "value": "step0@example.com", "sla_minutes": 5},
        ]

        class _PendingDB:
            def execute(self, stmt, *args, **kwargs):
                class _ScalarRes:
                    def scalar_one_or_none(self_):
                        return DELIVERY_STATUS_PENDING
                return _ScalarRes()
            def commit(self): pass
            def close(self): pass

        with patch("app.db.session.SessionLocal", return_value=_PendingDB()), \
             patch("app.repositories.admin_audit_repository.AdminAuditRepository.record", _fake_record), \
             patch.dict("os.environ", {"LUCIEL_EMAIL_TRANSPORT": "log"}):
            _advance(
                event_id=502,
                admin_id="admin-5",
                luciel_instance_id=None,
                session_id="sess-end",
                signal=SIGNAL_HIGH_VALUE_LEAD,
                gate=GATE_OUTCOME,
                current_step=0,
                chain=chain,
                email_to="fallback@example.com",
                subject="Test",
                body="body",
            )

        self.assertIn(ACTION_ESCALATION_CHAIN_END_FALLBACK, audit_actions)

    def test_chain_advance_writes_chain_step_audit(self):
        """Chain advance writes escalation_chain_step audit."""
        from app.worker.tasks.escalation_chain_walker import _advance

        audit_actions = []

        def _fake_record(_self_repo, *, action, **kwargs):
            audit_actions.append(action)

        chain = [
            {"channel": "email", "value": "step0@example.com", "sla_minutes": 5},
            {"channel": "email", "value": "step1@example.com", "sla_minutes": 5},
        ]

        class _PendingDB2:
            def execute(self, stmt, *args, **kwargs):
                class _ScalarRes:
                    def scalar_one_or_none(self_):
                        return DELIVERY_STATUS_PENDING
                return _ScalarRes()
            def commit(self): pass
            def close(self): pass

        with patch("app.db.session.SessionLocal", return_value=_PendingDB2()), \
             patch("app.repositories.admin_audit_repository.AdminAuditRepository.record", _fake_record), \
             patch("app.worker.tasks.escalation_chain_walker.advance_escalation_chain.apply_async"), \
             patch.dict("os.environ", {"LUCIEL_EMAIL_TRANSPORT": "log"}):
            _advance(
                event_id=503,
                admin_id="admin-5",
                luciel_instance_id=None,
                session_id="sess-advance",
                signal=SIGNAL_HIGH_VALUE_LEAD,
                gate=GATE_OUTCOME,
                current_step=0,
                chain=chain,
                email_to="fallback@example.com",
                subject="Test",
                body="body",
            )

        self.assertIn(ACTION_ESCALATION_CHAIN_STEP, audit_actions)
        self.assertIn(ACTION_ESCALATION_NOTIFICATION_SENT, audit_actions)


# ===========================================================================
# Delivery best-effort: never raises
# ===========================================================================


class TestDeliveryBestEffort(unittest.TestCase):

    def test_no_db_session_does_not_crash(self):
        """Delivery service with no DB still completes without raising."""
        def _boom():
            raise RuntimeError("no db available")

        email_adapter = _AlwaysSucceedAdapter()
        svc = EscalationDeliveryService(
            session_factory=_boom,
            email_adapter=email_adapter,
        )
        # Must not raise.
        svc.deliver(
            event_id=None,
            admin_id="admin-x",
            luciel_instance_id=None,
            session_id="sess-nodb",
            signal=SIGNAL_HIGH_VALUE_LEAD,
            gate=GATE_OUTCOME,
            contact=_make_contact(TIER_FREE),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
