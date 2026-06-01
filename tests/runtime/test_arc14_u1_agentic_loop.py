"""Arc 14 U1 — agentic-loop skeleton tests.

Scope (U1 only): the loop runs end-to-end against a STUB LLM; the
5-iteration bound is enforced and hitting it is NOT recorded as
escalation; tool dispatch goes through the broker and gate-2 refusals
(structured ``ToolResult(success=False)``) are reasoned about by
REFLECT; the trace is populated with provider/model/tool_called.

Founder decision #2: all cognition is driven through injected fakes —
deterministic, no network, no API cost. No DB is touched (the trace
service is a stub; the broker is a fake).

The escalation gates are PASS-THROUGH in U1 (real signals = U2); these
tests pin that pass-through behaviour so U2's drop-in is observable as
a behaviour change rather than a silent one.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.integrations.llm.base import LLMResponse
from app.runtime.contracts import RuntimeRequest
from app.runtime.orchestrator import MAX_LOOP_ITERATIONS, LucielOrchestrator
from app.tools.base import ToolResult


# =====================================================================
# Test doubles — deterministic, no network, no DB.
# =====================================================================


class _ScriptedRouter:
    """ModelRouter stand-in returning a scripted sequence of PLAN
    responses, one per ``generate`` call. The last script entry is
    reused if the loop calls more times than scripted (so a runaway
    loop is observable rather than IndexError-crashing the test)."""

    def __init__(self, contents: list[str], *, provider="stub", model="stub-x"):
        self._contents = contents
        self._provider = provider
        self._model = model
        self.calls: list = []

    def generate(self, request, *, preferred_provider=None) -> LLMResponse:
        idx = min(len(self.calls), len(self._contents) - 1)
        self.calls.append(request)
        return LLMResponse(
            content=self._contents[idx],
            model=self._model,
            provider=self._provider,
        )


class _RecordingBroker:
    """ToolBroker stand-in. Returns a scripted ``ToolResult`` keyed by
    tool name, defaulting to success. Records every dispatch so tests
    can assert the loop went THROUGH the broker (gates 1+2 live inside
    the real broker; the fake stands in for them)."""

    def __init__(self, results: dict[str, ToolResult] | None = None):
        self._results = results or {}
        self.dispatched: list[tuple[str, dict]] = []

    def execute_tool(self, tool_name, parameters=None, *, context=None, **extra):
        self.dispatched.append((tool_name, dict(parameters or {})))
        return self._results.get(
            tool_name,
            ToolResult(success=True, output=f"{tool_name} ok", metadata={}),
        )


class _StubTrace:
    def __init__(self):
        self.calls: list[dict] = []

    def record_trace(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return "trace-fixed-id"


# Neutral judge for loop-focused tests: the INTAKE gate must not consume
# router calls or fire while we're asserting loop behaviour. Injecting a
# judge with fake non-firing classifiers keeps these tests about the loop
# (the §3.4.5 signal behaviour has its own dedicated U2 suite).
class _NeutralIntent:
    def classify_intent(self, message):
        from app.runtime.classifiers import INTENT_OTHER, IntentResult

        return IntentResult(intent_class=INTENT_OTHER, confidence=0.0)


class _NeutralSentiment:
    def score_sentiment(self, message):
        from app.runtime.classifiers import SentimentResult

        return SentimentResult(score=0.0)


def _neutral_judge():
    from app.runtime.escalation_judge import EscalationJudge

    return EscalationJudge(
        intent_classifier=_NeutralIntent(),
        sentiment_classifier=_NeutralSentiment(),
    )


def _request(message="hello", instance_id=7):
    return RuntimeRequest(
        message=message,
        session_id="sess-1",
        user_id="user-1",
        admin_id="admin-1",
        channel="widget",
        luciel_instance_id=instance_id,
    )


def _plan_json(reply="done", tool_calls=None, confidence=0.9) -> str:
    import json

    return json.dumps(
        {
            "reply": reply,
            "tool_calls": tool_calls or [],
            "confidence": confidence,
        }
    )


# Run with the retrieval flag closed by default so these tests focus on
# the loop, not the (separately-tested) ARC 11 retrieve path.
def _run(orch, req):
    with patch(
        "app.core.config.settings.knowledge_retrieval_enabled", False
    ):
        return orch.run(req)


# =====================================================================
# Loop end-to-end against a stub LLM
# =====================================================================


class TestLoopEndToEnd(unittest.TestCase):

    def test_no_tool_reply_runs_one_iteration_and_returns_plan_reply(self):
        router = _ScriptedRouter([_plan_json(reply="Hi there", confidence=0.91)])
        trace = _StubTrace()
        orch = LucielOrchestrator(
            trace_service=trace,
            model_router=router,
            tool_broker=_RecordingBroker(),
            escalation_judge=_neutral_judge(),
        )

        resp = _run(orch, _request())

        self.assertEqual(resp.message, "Hi there")
        self.assertEqual(resp.confidence, 0.91)
        self.assertEqual(resp.iterations, 1)
        self.assertFalse(resp.bound_hit)
        self.assertFalse(resp.tool_called)
        self.assertEqual(len(router.calls), 1)

    def test_parse_failure_degrades_to_low_confidence_no_tool_reply(self):
        # Plain prose, not JSON — tolerant parse degrades gracefully.
        router = _ScriptedRouter(["I cannot produce JSON but here is text."])
        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=router,
            tool_broker=_RecordingBroker(),
        )

        resp = _run(orch, _request())

        self.assertIn("here is text", resp.message)
        self.assertLess(resp.confidence, 0.6)
        self.assertFalse(resp.tool_called)
        self.assertEqual(resp.iterations, 1)

    def test_llm_failure_degrades_without_crashing_turn(self):
        class _BoomRouter:
            def generate(self, request, *, preferred_provider=None):
                raise RuntimeError("all providers failed")

        # Inject a fake escalation service so the (now-firing) U2 OUTCOME
        # gate touches no DB.
        class _FakeEscalationSvc:
            def __init__(self):
                self.recorded = []

            def record_escalation(self, decision):
                self.recorded.append(decision)

        esc = _FakeEscalationSvc()
        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=_BoomRouter(),
            tool_broker=_RecordingBroker(),
            escalation_service=esc,
        )

        resp = _run(orch, _request())

        # Turn survives: a customer-facing reply is produced, confidence
        # floors at 0.0, no tool dispatched. The boom router degrades the
        # intake classifiers to neutral (no live call, no intake fire),
        # but a 0.0-confidence ungrounded answer DOES fire the U2 OUTCOME
        # cannot-confidently-answer signal — which is correct doctrine.
        self.assertTrue(resp.message)
        self.assertEqual(resp.confidence, 0.0)
        self.assertFalse(resp.tool_called)
        self.assertTrue(resp.escalation_flag)
        self.assertEqual(esc.recorded[0].signal, "cannot_confidently_answer")


# =====================================================================
# Tool dispatch goes through the broker; gate-2 refusal handled
# =====================================================================


class TestToolDispatch(unittest.TestCase):

    def test_tool_call_dispatched_through_broker(self):
        router = _ScriptedRouter(
            [
                _plan_json(
                    reply="looking that up",
                    tool_calls=[{"tool": "lookup_property", "parameters": {"id": 5}}],
                ),
                # Synthesis pass: no tool, so the loop stops after one
                # dispatch (otherwise the scripted router would re-issue
                # the same tool call and dispatch again).
                _plan_json(reply="here is what I found", confidence=0.9),
            ]
        )
        broker = _RecordingBroker()
        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=router,
            tool_broker=broker,
            escalation_judge=_neutral_judge(),
        )

        resp = _run(orch, _request())

        # The loop dispatched through the broker (the fake stands in for
        # gates 1+2) exactly once, and the trace records the tool name.
        self.assertEqual(broker.dispatched, [("lookup_property", {"id": 5})])
        self.assertTrue(resp.tool_called)
        self.assertEqual(resp.tool_name, "lookup_property")

    def test_tool_context_carries_admin_and_instance(self):
        router = _ScriptedRouter(
            [
                _plan_json(tool_calls=[{"tool": "send_sms", "parameters": {}}]),
                _plan_json(reply="sent", confidence=0.9),
            ]
        )

        seen = {}

        class _CtxBroker(_RecordingBroker):
            def execute_tool(self, tool_name, parameters=None, *, context=None, **extra):
                seen["context"] = context
                return ToolResult(success=True, output="ok")

        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=router,
            tool_broker=_CtxBroker(),
            escalation_judge=_neutral_judge(),
        )
        _run(orch, _request(instance_id=7))

        self.assertEqual(seen["context"].admin_id, "admin-1")
        self.assertEqual(seen["context"].instance_id, 7)

    def test_gate2_refusal_is_reasoned_about_and_reflects(self):
        # Gate-2 refusal shape: ToolResult(success=False) with an
        # authorization-denied metadata payload (same shape the real
        # DefaultDenyToolAuthorizer returns). REFLECT sees the failure
        # and (budget remaining) re-enters PLAN; the 2nd PLAN reply,
        # informed by the refusal, returns no tools and ends the turn.
        refusal = ToolResult(
            success=False,
            output="",
            error="Tool not authorised for this instance.",
            metadata={"authorization": "denied", "authorization_reason": "no_grant"},
        )
        router = _ScriptedRouter(
            [
                _plan_json(
                    reply="trying the tool",
                    tool_calls=[{"tool": "push_to_crm", "parameters": {}}],
                ),
                _plan_json(
                    reply="I can't do that, but here's what I can offer.",
                    tool_calls=[],
                    confidence=0.8,
                ),
            ]
        )
        broker = _RecordingBroker({"push_to_crm": refusal})
        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=router,
            tool_broker=broker,
            escalation_judge=_neutral_judge(),
        )

        resp = _run(orch, _request())

        # Re-entered PLAN exactly once after the refusal: 2 PLAN calls,
        # 2 iterations, final reply is the post-refusal plan.
        self.assertEqual(len(router.calls), 2)
        self.assertEqual(resp.iterations, 2)
        self.assertEqual(resp.message, "I can't do that, but here's what I can offer.")
        self.assertTrue(resp.tool_called)
        # The refusal text was fed back into the 2nd PLAN prompt.
        second_prompt = router.calls[1].messages[0].content
        self.assertIn("not authorised", second_prompt)

    def test_successful_tool_triggers_one_synthesis_pass(self):
        # U4 carry-forward (behaviour legitimately moved from U1): a
        # SUCCESSFUL tool now triggers exactly ONE synthesis PLAN pass so
        # the emitted reply incorporates the tool output (the U1 pre-tool
        # draft was composed before the tool ran). The synthesis reply
        # carries no tool_calls, so the loop stops after the 2nd pass.
        router = _ScriptedRouter(
            [
                _plan_json(
                    reply="let me look that up",
                    tool_calls=[{"tool": "lookup_property", "parameters": {}}],
                ),
                _plan_json(reply="The property is listed at $500k.", confidence=0.9),
            ]
        )
        broker = _RecordingBroker(
            {"lookup_property": ToolResult(success=True, output="price=$500k")}
        )
        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=router,
            tool_broker=broker,
            escalation_judge=_neutral_judge(),
        )

        resp = _run(orch, _request())

        # 2 PLAN calls (initial + synthesis), 2 iterations, final reply is
        # the synthesised answer that wove in the tool output.
        self.assertEqual(len(router.calls), 2)
        self.assertEqual(resp.iterations, 2)
        self.assertEqual(resp.message, "The property is listed at $500k.")
        # The successful tool output was fed back into the synthesis prompt.
        synthesis_prompt = router.calls[1].messages[0].content
        self.assertIn("price=$500k", synthesis_prompt)


# =====================================================================
# 5-iteration bound — hard stop, NOT an escalation trigger
# =====================================================================


class TestIterationBound(unittest.TestCase):

    def test_bound_is_five(self):
        self.assertEqual(MAX_LOOP_ITERATIONS, 5)

    def test_loop_stops_at_five_when_tool_keeps_failing(self):
        # Every PLAN asks for a tool; the tool ALWAYS fails → REFLECT
        # keeps wanting to re-enter, but the bound caps it at 5.
        failing = ToolResult(success=False, output="", error="boom")
        router = _ScriptedRouter(
            [_plan_json(tool_calls=[{"tool": "push_to_crm", "parameters": {}}])]
        )
        broker = _RecordingBroker({"push_to_crm": failing})
        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=router,
            tool_broker=broker,
            escalation_judge=_neutral_judge(),
        )

        resp = _run(orch, _request())

        self.assertEqual(resp.iterations, MAX_LOOP_ITERATIONS)
        self.assertEqual(len(router.calls), MAX_LOOP_ITERATIONS)
        self.assertEqual(len(broker.dispatched), MAX_LOOP_ITERATIONS)
        self.assertTrue(resp.bound_hit)

    def test_hitting_bound_is_NOT_recorded_as_escalation(self):
        failing = ToolResult(success=False, output="", error="boom")
        router = _ScriptedRouter(
            [_plan_json(tool_calls=[{"tool": "push_to_crm", "parameters": {}}])]
        )
        trace = _StubTrace()
        orch = LucielOrchestrator(
            trace_service=trace,
            model_router=router,
            tool_broker=_RecordingBroker({"push_to_crm": failing}),
        )

        resp = _run(orch, _request())

        # The doctrinal invariant (§3.4.1 locked #17): the bound is
        # cost-control, NEVER an escalation trigger.
        self.assertTrue(resp.bound_hit)
        self.assertFalse(resp.escalation_flag)
        self.assertFalse(trace.calls[0]["escalated"])


# =====================================================================
# Trace populated with provider / model / tool_called (FINALIZE)
# =====================================================================


class TestTraceFinalization(unittest.TestCase):

    def test_trace_fields_filled_for_no_tool_turn(self):
        router = _ScriptedRouter(
            [_plan_json(reply="hi")], provider="anthropic", model="claude-x"
        )
        trace = _StubTrace()
        orch = LucielOrchestrator(
            trace_service=trace, model_router=router, tool_broker=_RecordingBroker()
        )

        resp = _run(orch, _request())

        call = trace.calls[0]
        self.assertEqual(call["llm_provider"], "anthropic")
        self.assertEqual(call["llm_model"], "claude-x")
        self.assertFalse(call["tool_called"])
        self.assertIsNone(call["tool_name"])
        self.assertFalse(call["escalated"])
        # Same provenance surfaced on the response.
        self.assertEqual(resp.llm_provider, "anthropic")
        self.assertEqual(resp.llm_model, "claude-x")

    def test_trace_records_tool_called_and_name(self):
        router = _ScriptedRouter(
            [
                _plan_json(tool_calls=[{"tool": "book_appointment", "parameters": {}}]),
                _plan_json(reply="booked for you", confidence=0.9),
            ],
            provider="openai",
            model="gpt-x",
        )
        trace = _StubTrace()
        orch = LucielOrchestrator(
            trace_service=trace,
            model_router=router,
            tool_broker=_RecordingBroker(
                {"book_appointment": ToolResult(success=True, output="booked")}
            ),
            escalation_judge=_neutral_judge(),
        )

        _run(orch, _request())

        call = trace.calls[0]
        self.assertTrue(call["tool_called"])
        self.assertEqual(call["tool_name"], "book_appointment")
        self.assertEqual(call["llm_provider"], "openai")
        self.assertEqual(call["llm_model"], "gpt-x")

    def test_degraded_turn_records_none_provider(self):
        class _BoomRouter:
            def generate(self, request, *, preferred_provider=None):
                raise RuntimeError("down")

        trace = _StubTrace()
        orch = LucielOrchestrator(
            trace_service=trace,
            model_router=_BoomRouter(),
            tool_broker=_RecordingBroker(),
        )

        _run(orch, _request())

        call = trace.calls[0]
        self.assertIsNone(call["llm_provider"])
        self.assertIsNone(call["llm_model"])


# =====================================================================
# Escalation gates with NO classifier signal — U2 drop-in still passes
# through cleanly when nothing fires.
#
# These were U1 pass-through pins. U2 replaces the pass-through with the
# real §3.4.5 gates; the gates are wired to the SAME injected router as
# PLAN, so a scripted/boom router that returns plan-shaped (not
# classifier-shaped) JSON yields a neutral classification and the INTAKE
# gate does NOT fire — proving the gate is signal-driven, not a blanket
# short-circuit. The detailed signal-boundary + firing behaviour lives
# in the dedicated U2 suite (test_arc14_u2_escalation_*).
# =====================================================================


class TestEscalationGatesNeutralPassThrough(unittest.TestCase):

    def test_intake_gate_neutral_signal_does_not_short_circuit_plan(self):
        # The injected scripted router returns PLAN-shaped JSON for every
        # call, including the intent classifier's call. That parses to a
        # neutral intent ("other") so the explicit-human-request signal
        # does NOT fire and PLAN runs normally.
        router = _ScriptedRouter([_plan_json(reply="planned")])
        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=router,
            tool_broker=_RecordingBroker(),
        )

        resp = _run(orch, _request(message="I want a human right now!!"))

        self.assertEqual(resp.message, "planned")
        self.assertFalse(resp.escalation_flag)

    def test_outcome_low_confidence_with_no_retrieval_now_escalates(self):
        # U2 behaviour change (observable, not silent): a low-confidence
        # answer with NO grounding (retrieval flag off ⇒ grounding None ⇒
        # treated as below every tier floor) fires the cannot-confidently-
        # answer OUTCOME signal. The customer-facing reply is still
        # emitted; escalation is an additive side-effect on the turn.
        router = _ScriptedRouter([_plan_json(reply="unsure", confidence=0.1)])
        # Inject a fake escalation service so no DB is touched and we can
        # assert the decision was recorded.
        recorded: list = []

        class _FakeEscalationSvc:
            def record_escalation(self, decision):
                recorded.append(decision)

        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=router,
            tool_broker=_RecordingBroker(),
            escalation_service=_FakeEscalationSvc(),
        )

        resp = _run(orch, _request())

        self.assertTrue(resp.escalation_flag)
        self.assertEqual(resp.message, "unsure")
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0].signal, "cannot_confidently_answer")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
