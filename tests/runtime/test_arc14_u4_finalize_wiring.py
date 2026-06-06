"""Arc 14 U4 — orchestrator COGNITION FINALIZATION wiring.

Asserts the agentic loop INVOKES the cognition finalizer in its FINALIZE
step (both the normal post-loop path and the Gate-1 intake short-circuit
path), threads the right arguments, and derives the §3.4.6 live-takeover
signal (``handoff_requested``) only from an EXPLICIT HUMAN REQUEST.

Hermetic: a fake finalizer captures the call (no DB, no LLM, no network);
a stub trace + neutral/explicit judge keep the rest of the loop
deterministic (founder decision #2).
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.integrations.llm.base import LLMResponse
from app.runtime.contracts import RuntimeRequest
from app.runtime.escalation_judge import EscalationJudge
from app.runtime.orchestrator import LucielOrchestrator


class _ScriptedRouter:
    def __init__(self, contents):
        self._contents = contents
        self.calls = []

    def generate(self, request, *, preferred_provider=None, **kwargs):
        idx = min(len(self.calls), len(self._contents) - 1)
        self.calls.append(request)
        return LLMResponse(
            content=self._contents[idx], model="stub-x", provider="stub"
        )


class _RecordingBroker:
    def execute_tool(self, tool_name, parameters=None, *, context=None, **extra):
        from app.tools.base import ToolResult

        return ToolResult(success=True, output=f"{tool_name} ok", metadata={})


class _StubTrace:
    def record_trace(self, **kwargs):
        return "trace-fixed-id"


class _FakeFinalizer:
    def __init__(self):
        self.calls = []

    def finalize(self, **kwargs):
        self.calls.append(kwargs)
        from app.cognition.finalizer import FinalizationResult

        return FinalizationResult()


class _NeutralIntent:
    def classify_intent(self, message):
        from app.runtime.classifiers import INTENT_OTHER, IntentResult

        return IntentResult(intent_class=INTENT_OTHER, confidence=0.0)


class _ExplicitIntent:
    def classify_intent(self, message):
        from app.runtime.classifiers import (
            INTENT_REQUEST_HUMAN,
            IntentResult,
        )

        return IntentResult(
            intent_class=INTENT_REQUEST_HUMAN, confidence=0.99
        )


class _NeutralSentiment:
    def score_sentiment(self, message):
        from app.runtime.classifiers import SentimentResult

        return SentimentResult(score=0.0)


def _judge(intent):
    return EscalationJudge(
        intent_classifier=intent, sentiment_classifier=_NeutralSentiment()
    )


def _request(message="hello"):
    return RuntimeRequest(
        message=message,
        session_id="sess-1",
        user_id="user-1",
        admin_id="admin-1",
        channel="widget",
        luciel_instance_id=7,
        recent_customer_messages=["earlier turn"],
    )


def _plan(reply="done", confidence=0.9):
    return json.dumps({"reply": reply, "tool_calls": [], "confidence": confidence})


def _run(orch, req):
    with patch("app.core.config.settings.knowledge_retrieval_enabled", False):
        return orch.run(req)


class TestFinalizeWiring(unittest.TestCase):
    def test_finalizer_invoked_on_normal_path(self):
        fin = _FakeFinalizer()
        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=_ScriptedRouter([_plan(reply="Hi there")]),
            tool_broker=_RecordingBroker(),
            escalation_judge=_judge(_NeutralIntent()),
            cognition_finalizer=fin,
        )
        _run(orch, _request())

        self.assertEqual(len(fin.calls), 1)
        call = fin.calls[0]
        self.assertEqual(call["admin_id"], "admin-1")
        self.assertEqual(call["session_id"], "sess-1")
        self.assertEqual(call["luciel_instance_id"], 7)
        self.assertEqual(call["current_message"], "hello")
        self.assertEqual(call["assistant_reply"], "Hi there")
        self.assertEqual(call["inbound_channel"], "widget")
        self.assertEqual(call["prior_customer_messages"], ["earlier turn"])
        # Neutral judge → no escalation → no handoff.
        self.assertFalse(call["escalation_fired"])
        self.assertFalse(call["handoff_requested"])

    def test_finalizer_invoked_on_intake_shortcircuit_with_handoff(self):
        # Explicit human request fires Gate-1 (intake short-circuit). The
        # finalizer must still run, with escalation_fired AND
        # handoff_requested both True (a person was asked for).
        fin = _FakeFinalizer()

        class _FakeEscalationSvc:
            def record_escalation(self, decision):
                return None

        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=_ScriptedRouter([_plan()]),
            tool_broker=_RecordingBroker(),
            escalation_judge=_judge(_ExplicitIntent()),
            escalation_service=_FakeEscalationSvc(),
            cognition_finalizer=fin,
        )
        resp = _run(orch, _request(message="I want to talk to a human"))

        self.assertTrue(resp.escalation_flag)
        self.assertEqual(resp.iterations, 0)  # short-circuit, loop never ran
        self.assertEqual(len(fin.calls), 1)
        call = fin.calls[0]
        self.assertTrue(call["escalation_fired"])
        self.assertTrue(call["handoff_requested"])

    def test_handoff_warranted_only_for_explicit_human_request(self):
        from app.policy.escalation import EscalationDecision
        from app.models.escalation_event import (
            GATE_OUTCOME,
            SIGNAL_EXPLICIT_HUMAN_REQUEST,
            SIGNAL_HIGH_VALUE_LEAD,
        )

        explicit = EscalationDecision(
            signal=SIGNAL_EXPLICIT_HUMAN_REQUEST,
            gate=GATE_OUTCOME,
            admin_id="a",
            session_id="s",
        )
        lead = EscalationDecision(
            signal=SIGNAL_HIGH_VALUE_LEAD,
            gate=GATE_OUTCOME,
            admin_id="a",
            session_id="s",
        )
        self.assertTrue(LucielOrchestrator._handoff_warranted(explicit))
        self.assertFalse(LucielOrchestrator._handoff_warranted(lead))
        self.assertFalse(LucielOrchestrator._handoff_warranted(None))

    def test_finalize_failure_does_not_crash_turn(self):
        class _BoomFinalizer:
            def finalize(self, **kwargs):
                raise RuntimeError("finalize boom")

        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=_ScriptedRouter([_plan(reply="still works")]),
            tool_broker=_RecordingBroker(),
            escalation_judge=_judge(_NeutralIntent()),
            cognition_finalizer=_BoomFinalizer(),
        )
        resp = _run(orch, _request())
        self.assertEqual(resp.message, "still works")


if __name__ == "__main__":
    unittest.main()
