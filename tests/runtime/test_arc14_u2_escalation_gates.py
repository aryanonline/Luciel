"""Arc 14 U2 — orchestrator escalation-gate wiring tests.

End-to-end through ``LucielOrchestrator.run`` with deterministic fakes
(no DB, no network):

  * INTAKE fires BEFORE plan/act/reflect — the LLM is never called and a
    templated handoff acknowledgement is emitted instead.
  * OUTCOME fires AFTER reflect — the loop runs. When the signal is
    CANNOT_CONFIDENTLY_ANSWER the LLM reply is replaced by the §3.4.13
    canonical phrase (anti-hallucination promise). Other outcome signals
    leave the reply unchanged. Escalation is a flag + recorded side-effect.
  * The 5-iteration bound is NEVER an escalation trigger (§3.4.1 #17),
    even when the loop hits it.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.integrations.llm.base import LLMResponse
from app.runtime.classifiers import (
    INTENT_OTHER,
    INTENT_REQUEST_HUMAN,
    IntentResult,
    SentimentResult,
)
from app.runtime.contracts import RuntimeRequest
from app.runtime.escalation_judge import EscalationJudge
from app.runtime.handoff_ack import CANNOT_ANSWER_REPLY, handoff_acknowledgement
from app.runtime.orchestrator import LucielOrchestrator
from app.tools.base import ToolResult


# ---------------------------------------------------------------------
# Test doubles.
# ---------------------------------------------------------------------


class _ScriptedRouter:
    def __init__(self, contents, *, provider="stub", model="stub-x"):
        self._contents = contents
        self._provider = provider
        self._model = model
        self.calls = []

    def generate(self, request, *, preferred_provider=None, **kwargs):
        idx = min(len(self.calls), len(self._contents) - 1)
        self.calls.append(request)
        return LLMResponse(
            content=self._contents[idx], model=self._model, provider=self._provider
        )


class _Broker:
    def __init__(self, results=None):
        self._results = results or {}
        self.dispatched = []

    def execute_tool(self, tool_name, parameters=None, *, context=None, **extra):
        self.dispatched.append((tool_name, dict(parameters or {})))
        return self._results.get(
            tool_name, ToolResult(success=True, output=f"{tool_name} ok")
        )


class _StubTrace:
    def __init__(self):
        self.calls = []

    def record_trace(self, **kwargs):
        self.calls.append(kwargs)
        return "trace-fixed-id"


class _FakeEscalationSvc:
    def __init__(self):
        self.recorded = []

    def record_escalation(self, decision):
        self.recorded.append(decision)


class _FixedIntent:
    def __init__(self, intent_class, confidence):
        self._r = IntentResult(intent_class=intent_class, confidence=confidence)

    def classify_intent(self, message):
        return self._r


class _FixedSentiment:
    def __init__(self, score=0.0):
        self._score = score

    def score_sentiment(self, message):
        return SentimentResult(score=self._score)


def _judge(*, intent_class=INTENT_OTHER, confidence=0.0, sentiment=0.0):
    return EscalationJudge(
        intent_classifier=_FixedIntent(intent_class, confidence),
        sentiment_classifier=_FixedSentiment(sentiment),
    )


def _plan_json(reply="done", tool_calls=None, confidence=0.9):
    return json.dumps(
        {"reply": reply, "tool_calls": tool_calls or [], "confidence": confidence}
    )


def _request(message="hello"):
    return RuntimeRequest(
        message=message,
        session_id="sess-1",
        user_id="user-1",
        admin_id="admin-1",
        channel="widget",
        luciel_instance_id=7,
    )


def _run(orch, req):
    with patch("app.core.config.settings.knowledge_retrieval_enabled", False):
        return orch.run(req)


# =====================================================================
# INTAKE fires BEFORE plan/act/reflect.
# =====================================================================


class TestIntakeGateShortCircuits(unittest.TestCase):

    def test_explicit_human_request_skips_plan_and_emits_handoff_ack(self):
        router = _ScriptedRouter([_plan_json(reply="should not be used")])
        broker = _Broker()
        esc = _FakeEscalationSvc()
        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=router,
            tool_broker=broker,
            escalation_judge=_judge(intent_class=INTENT_REQUEST_HUMAN, confidence=0.9),
            escalation_service=esc,
        )

        resp = _run(orch, _request("I want to talk to a person"))

        # PLAN never ran (the LLM may be exactly what's failing the
        # customer); the reply is the templated handoff ack.
        self.assertEqual(len(router.calls), 0)
        self.assertEqual(len(broker.dispatched), 0)
        self.assertEqual(resp.message, handoff_acknowledgement())
        self.assertEqual(resp.iterations, 0)
        self.assertTrue(resp.escalation_flag)
        # The escalation was recorded with the explicit-human signal.
        self.assertEqual(len(esc.recorded), 1)
        self.assertEqual(esc.recorded[0].signal, "explicit_human_request")

    def test_intake_trace_records_escalated_and_no_provider(self):
        trace = _StubTrace()
        orch = LucielOrchestrator(
            trace_service=trace,
            model_router=_ScriptedRouter([_plan_json()]),
            tool_broker=_Broker(),
            escalation_judge=_judge(intent_class=INTENT_REQUEST_HUMAN, confidence=0.88),
            escalation_service=_FakeEscalationSvc(),
        )

        _run(orch, _request("human now"))

        call = trace.calls[0]
        self.assertTrue(call["escalated"])
        self.assertIsNone(call["llm_provider"])
        self.assertFalse(call["tool_called"])

    def test_no_intake_signal_proceeds_into_plan(self):
        router = _ScriptedRouter([_plan_json(reply="planned answer", confidence=0.9)])
        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=router,
            tool_broker=_Broker(),
            escalation_judge=_judge(),  # neutral
            escalation_service=_FakeEscalationSvc(),
        )

        resp = _run(orch, _request("what are your hours"))

        self.assertEqual(len(router.calls), 1)
        self.assertEqual(resp.message, "planned answer")
        self.assertFalse(resp.escalation_flag)


# =====================================================================
# OUTCOME fires AFTER reflect.
# =====================================================================


class TestOutcomeGateAfterReflect(unittest.TestCase):

    def test_low_confidence_ungrounded_escalates_after_loop_runs(self):
        router = _ScriptedRouter([_plan_json(reply="not sure", confidence=0.2)])
        esc = _FakeEscalationSvc()
        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=router,
            tool_broker=_Broker(),
            escalation_judge=_judge(),  # neutral intake
            escalation_service=esc,
        )

        resp = _run(orch, _request())

        # The loop ran (PLAN called). When SIGNAL_CANNOT_CONFIDENTLY_ANSWER
        # fires, the LLM reply is replaced by the §3.4.13 canonical phrase
        # so an ungrounded answer is never sent to the customer (Vision §1
        # anti-hallucination promise). Escalation flag is set and recorded.
        self.assertEqual(len(router.calls), 1)
        self.assertEqual(resp.message, CANNOT_ANSWER_REPLY)
        self.assertTrue(resp.escalation_flag)
        self.assertEqual(esc.recorded[0].signal, "cannot_confidently_answer")
        self.assertEqual(esc.recorded[0].gate, "outcome")

    def test_confident_answer_does_not_escalate(self):
        router = _ScriptedRouter([_plan_json(reply="here you go", confidence=0.95)])
        esc = _FakeEscalationSvc()
        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=router,
            tool_broker=_Broker(),
            escalation_judge=_judge(),
            escalation_service=esc,
        )

        resp = _run(orch, _request())

        self.assertFalse(resp.escalation_flag)
        self.assertEqual(len(esc.recorded), 0)


# =====================================================================
# Bound invariant — hitting the cap is NOT an escalation trigger.
# =====================================================================


class TestBoundIsNotEscalation(unittest.TestCase):

    def test_bound_hit_with_confident_plan_does_not_escalate(self):
        # Every PLAN asks for a tool that always fails → loop hits the
        # 5-iteration cap. Confidence stays 0.9 (>= 0.6) so the OUTCOME
        # cannot-answer signal does NOT fire. The bound itself must
        # never escalate.
        failing = ToolResult(success=False, output="", error="boom")
        router = _ScriptedRouter(
            [_plan_json(tool_calls=[{"tool": "push_to_crm", "parameters": {}}],
                        confidence=0.9)]
        )
        esc = _FakeEscalationSvc()
        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=router,
            tool_broker=_Broker({"push_to_crm": failing}),
            escalation_judge=_judge(),
            escalation_service=esc,
        )

        resp = _run(orch, _request())

        self.assertTrue(resp.bound_hit)
        self.assertFalse(resp.escalation_flag)
        self.assertEqual(len(esc.recorded), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
