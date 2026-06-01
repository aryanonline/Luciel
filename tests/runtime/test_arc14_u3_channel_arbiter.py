"""Arc 14 U3 — §3.4.2 Channel Arbiter tests.

Two layers, all hermetic (no DB, no network):

  * Pure decision-tree tests over ``ChannelArbiter.pick`` — every branch,
    customer-switch-wins beating every other rule, the >500-char SMS→email
    rule (only when email enabled, else fall through), the urgent-escalation
    voice-deferred fall-through (SMS then email), disabled-preferred-channel
    fallback to inbound, and default same-as-inbound.

  * Orchestrator-wiring tests through ``LucielOrchestrator.run`` with an
    injected arbiter + stub LLM, asserting the RESPOND step threads the
    arbiter's pick onto the response and degrades safely.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.integrations.llm.base import LLMResponse
from app.policy.entitlements import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    CHANNEL_WIDGET,
)
from app.runtime.channel_arbiter import (
    SMS_LENGTH_SWITCH_THRESHOLD,
    ArbiterInput,
    ChannelArbiter,
    ChannelChoice,
)
from app.runtime.contracts import RuntimeRequest
from app.runtime.orchestrator import LucielOrchestrator
from app.tools.base import ToolResult


def _arb() -> ChannelArbiter:
    return ChannelArbiter()


# =====================================================================
# Rule 4 — DEFAULT same channel as inbound.
# =====================================================================


class TestDefaultSameAsInbound(unittest.TestCase):

    def test_default_picks_inbound(self):
        choice = _arb().pick(
            ArbiterInput(
                inbound_channel=CHANNEL_WIDGET,
                enabled_channels={CHANNEL_WIDGET},
            )
        )
        self.assertEqual(choice.channel, CHANNEL_WIDGET)
        self.assertEqual(choice.reason, "default_inbound")
        self.assertFalse(choice.prompt_channel_switch)
        self.assertIsNone(choice.switched_from)

    def test_default_sms_stays_sms_when_reply_short(self):
        choice = _arb().pick(
            ArbiterInput(
                inbound_channel=CHANNEL_SMS,
                enabled_channels={CHANNEL_SMS, CHANNEL_EMAIL},
                response_length=10,
            )
        )
        self.assertEqual(choice.channel, CHANNEL_SMS)

    def test_sparse_enabled_set_degrades_to_inbound(self):
        # Empty enabled set: the arbiter still serves the inbound channel.
        choice = _arb().pick(ArbiterInput(inbound_channel=CHANNEL_EMAIL))
        self.assertEqual(choice.channel, CHANNEL_EMAIL)


# =====================================================================
# Rule 1 — CUSTOMER-INITIATED SWITCH always wins.
# =====================================================================


class TestCustomerSwitchWins(unittest.TestCase):

    def test_customer_request_honoured_when_enabled(self):
        choice = _arb().pick(
            ArbiterInput(
                inbound_channel=CHANNEL_WIDGET,
                enabled_channels={CHANNEL_WIDGET, CHANNEL_EMAIL},
                customer_requested_channel=CHANNEL_EMAIL,
            )
        )
        self.assertEqual(choice.channel, CHANNEL_EMAIL)
        self.assertEqual(choice.reason, "customer_requested")
        self.assertEqual(choice.switched_from, CHANNEL_WIDGET)

    def test_customer_request_beats_long_sms_rule(self):
        # Long SMS reply (rule 2 would pick email) BUT the customer asked
        # to stay on SMS → customer wins, no permission prompt.
        choice = _arb().pick(
            ArbiterInput(
                inbound_channel=CHANNEL_SMS,
                enabled_channels={CHANNEL_SMS, CHANNEL_EMAIL},
                response_length=SMS_LENGTH_SWITCH_THRESHOLD + 100,
                customer_requested_channel=CHANNEL_SMS,
            )
        )
        self.assertEqual(choice.channel, CHANNEL_SMS)
        self.assertEqual(choice.reason, "customer_requested")
        self.assertFalse(choice.prompt_channel_switch)

    def test_customer_request_beats_escalation_rule(self):
        # Escalation fired (rule 3 would pick SMS) BUT the customer asked
        # for email → customer wins.
        choice = _arb().pick(
            ArbiterInput(
                inbound_channel=CHANNEL_WIDGET,
                enabled_channels={CHANNEL_WIDGET, CHANNEL_SMS, CHANNEL_EMAIL},
                escalation_fired=True,
                customer_requested_channel=CHANNEL_EMAIL,
            )
        )
        self.assertEqual(choice.channel, CHANNEL_EMAIL)
        self.assertEqual(choice.reason, "customer_requested")

    def test_customer_request_disabled_falls_back_to_inbound(self):
        # Customer asked for SMS but SMS is not enabled → fall back to
        # the inbound channel (the enablement constraint binds even the
        # customer-initiated switch).
        choice = _arb().pick(
            ArbiterInput(
                inbound_channel=CHANNEL_WIDGET,
                enabled_channels={CHANNEL_WIDGET},
                customer_requested_channel=CHANNEL_SMS,
            )
        )
        self.assertEqual(choice.channel, CHANNEL_WIDGET)
        self.assertEqual(
            choice.reason, "customer_requested_disabled_fallback_inbound"
        )


# =====================================================================
# Rule 2 — LONG SMS REPLY → email (if enabled), with permission prompt.
# =====================================================================


class TestLongSmsReplySwitch(unittest.TestCase):

    def test_long_sms_switches_to_email_when_enabled(self):
        choice = _arb().pick(
            ArbiterInput(
                inbound_channel=CHANNEL_SMS,
                enabled_channels={CHANNEL_SMS, CHANNEL_EMAIL},
                response_length=SMS_LENGTH_SWITCH_THRESHOLD + 1,
            )
        )
        self.assertEqual(choice.channel, CHANNEL_EMAIL)
        self.assertTrue(choice.prompt_channel_switch)
        self.assertEqual(choice.reason, "long_sms_reply_switch_email")
        self.assertEqual(choice.switched_from, CHANNEL_SMS)

    def test_exactly_500_does_not_switch(self):
        # Threshold is strict ">": exactly 500 chars stays on SMS.
        choice = _arb().pick(
            ArbiterInput(
                inbound_channel=CHANNEL_SMS,
                enabled_channels={CHANNEL_SMS, CHANNEL_EMAIL},
                response_length=SMS_LENGTH_SWITCH_THRESHOLD,
            )
        )
        self.assertEqual(choice.channel, CHANNEL_SMS)
        self.assertFalse(choice.prompt_channel_switch)

    def test_long_sms_falls_through_when_email_disabled(self):
        # Email not enabled → no switch; fall through to default (SMS).
        choice = _arb().pick(
            ArbiterInput(
                inbound_channel=CHANNEL_SMS,
                enabled_channels={CHANNEL_SMS},
                response_length=SMS_LENGTH_SWITCH_THRESHOLD + 999,
            )
        )
        self.assertEqual(choice.channel, CHANNEL_SMS)
        self.assertFalse(choice.prompt_channel_switch)

    def test_long_reply_on_non_sms_does_not_switch(self):
        # The rule is SMS-specific: a long widget reply stays on widget.
        choice = _arb().pick(
            ArbiterInput(
                inbound_channel=CHANNEL_WIDGET,
                enabled_channels={CHANNEL_WIDGET, CHANNEL_EMAIL},
                response_length=SMS_LENGTH_SWITCH_THRESHOLD + 999,
            )
        )
        self.assertEqual(choice.channel, CHANNEL_WIDGET)


# =====================================================================
# Rule 3 — URGENT ESCALATION → voice > SMS > email; voice deferred.
# =====================================================================


class TestEscalationPriority(unittest.TestCase):

    def test_escalation_picks_sms_when_voice_deferred_and_sms_enabled(self):
        # Voice is ARC 14b deferred (never enabled) → highest ENABLED is
        # SMS even though voice leads the priority list.
        choice = _arb().pick(
            ArbiterInput(
                inbound_channel=CHANNEL_WIDGET,
                enabled_channels={CHANNEL_WIDGET, CHANNEL_SMS, CHANNEL_EMAIL},
                escalation_fired=True,
            )
        )
        self.assertEqual(choice.channel, CHANNEL_SMS)
        self.assertEqual(choice.reason, "escalation_priority")

    def test_escalation_falls_to_email_when_sms_disabled(self):
        choice = _arb().pick(
            ArbiterInput(
                inbound_channel=CHANNEL_WIDGET,
                enabled_channels={CHANNEL_WIDGET, CHANNEL_EMAIL},
                escalation_fired=True,
            )
        )
        self.assertEqual(choice.channel, CHANNEL_EMAIL)
        self.assertEqual(choice.reason, "escalation_priority")

    def test_voice_is_never_selected(self):
        # Even if "voice" were somehow in the enabled set, the deferred
        # marker means the priority loop would pick it — but v1 can never
        # enable voice. Assert that with only widget enabled the
        # escalation falls back to inbound (no SMS/email available).
        choice = _arb().pick(
            ArbiterInput(
                inbound_channel=CHANNEL_WIDGET,
                enabled_channels={CHANNEL_WIDGET},
                escalation_fired=True,
            )
        )
        self.assertEqual(choice.channel, CHANNEL_WIDGET)
        self.assertEqual(
            choice.reason, "escalation_no_priority_channel_fallback_inbound"
        )

    def test_escalation_prefers_sms_over_email_when_both_enabled(self):
        choice = _arb().pick(
            ArbiterInput(
                inbound_channel=CHANNEL_EMAIL,
                enabled_channels={CHANNEL_SMS, CHANNEL_EMAIL},
                escalation_fired=True,
            )
        )
        self.assertEqual(choice.channel, CHANNEL_SMS)


# =====================================================================
# Orchestrator RESPOND wiring.
# =====================================================================


class _ScriptedRouter:
    def __init__(self, content, *, provider="stub", model="stub-x"):
        self._content = content
        self._provider = provider
        self._model = model

    def generate(self, request, *, preferred_provider=None):
        return LLMResponse(
            content=self._content, model=self._model, provider=self._provider
        )


class _Broker:
    def execute_tool(self, tool_name, parameters=None, *, context=None, **extra):
        return ToolResult(success=True, output=f"{tool_name} ok")


class _StubTrace:
    def record_trace(self, **kwargs):
        return "trace-fixed-id"


class _StubArbiter:
    def __init__(self, choice):
        self._choice = choice
        self.inputs = []

    def pick(self, data):
        self.inputs.append(data)
        return self._choice


def _plan_json(reply="done", confidence=0.9):
    return json.dumps({"reply": reply, "tool_calls": [], "confidence": confidence})


def _request(channel="sms", requested=None):
    return RuntimeRequest(
        message="hello",
        session_id="sess-1",
        user_id="user-1",
        admin_id="admin-1",
        channel=channel,
        luciel_instance_id=None,  # skip DB lookup; arbiter gets {inbound, widget}
        customer_requested_channel=requested,
    )


def _run(orch, req):
    with patch("app.core.config.settings.knowledge_retrieval_enabled", False):
        return orch.run(req)


class TestOrchestratorRespondWiring(unittest.TestCase):

    def test_response_carries_arbiter_pick(self):
        arb = _StubArbiter(
            ChannelChoice(channel=CHANNEL_EMAIL, prompt_channel_switch=True)
        )
        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=_ScriptedRouter(_plan_json()),
            tool_broker=_Broker(),
            channel_arbiter=arb,
        )
        resp = _run(orch, _request(channel=CHANNEL_SMS))
        self.assertEqual(resp.response_channel, CHANNEL_EMAIL)
        self.assertTrue(resp.prompt_channel_switch)
        # The arbiter saw the inbound channel + reply length.
        self.assertEqual(arb.inputs[0].inbound_channel, CHANNEL_SMS)
        self.assertFalse(arb.inputs[0].escalation_fired)

    def test_default_real_arbiter_emits_on_inbound(self):
        # No injected arbiter → real ChannelArbiter, no instance id →
        # enabled set degrades to {inbound, widget} → default same-channel.
        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=_ScriptedRouter(_plan_json()),
            tool_broker=_Broker(),
        )
        resp = _run(orch, _request(channel=CHANNEL_WIDGET))
        self.assertEqual(resp.response_channel, CHANNEL_WIDGET)
        self.assertFalse(resp.prompt_channel_switch)

    def test_customer_request_threaded_into_arbiter(self):
        arb = _StubArbiter(ChannelChoice(channel=CHANNEL_EMAIL))
        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=_ScriptedRouter(_plan_json()),
            tool_broker=_Broker(),
            channel_arbiter=arb,
        )
        _run(orch, _request(channel=CHANNEL_SMS, requested=CHANNEL_EMAIL))
        self.assertEqual(
            arb.inputs[0].customer_requested_channel, CHANNEL_EMAIL
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
