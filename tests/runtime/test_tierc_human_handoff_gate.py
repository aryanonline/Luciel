"""Rescan Tier-C §3.4.12 — human-controlled session gate tests.

LOAD-BEARING property:

  * An inbound message on a human_controlled session makes ZERO LLM
    calls (the agentic loop is never entered).
  * The turn does NOT increment the LLM call count (§9 item 24/33).
  * A human_controlled turn returns with intent_summary='human_controlled'
    and 0 iterations.
  * The gate triggers BEFORE context assembly, budget gate, and
    PLAN/ACT/REFLECT — nothing else runs.

Luciel-initiated path:

  * When escalation_judge fires INTENT_REQUEST_HUMAN (INTAKE gate fires),
    _finalize_intake_escalation calls _set_session_human_controlled_best_effort
    with trigger='luciel_escalated'.
  * Subsequent calls on the same session return zero LLM calls.

All tests are deterministic (no DB, no network). The human_controlled
gate check is injected by patching _is_session_human_controlled so these
tests run without Postgres.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from app.integrations.llm.base import LLMResponse
from app.runtime.classifiers import (
    INTENT_OTHER,
    INTENT_REQUEST_HUMAN,
    IntentResult,
    SentimentResult,
)
from app.runtime.contracts import RuntimeRequest, RuntimeResponse
from app.runtime.escalation_judge import EscalationJudge
from app.runtime.orchestrator import LucielOrchestrator
from app.tools.base import ToolResult


# =====================================================================
# Test doubles — mirror test_arc14_u2_escalation_gates.py style.
# =====================================================================


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
        return "trace-human-ctrl-test"


class _FakeEscalationSvc:
    def __init__(self):
        self.recorded = []

    def record_escalation(self, decision):
        self.recorded.append(decision)
        return None


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


def _request(message="hello", session_id="sess-1"):
    return RuntimeRequest(
        message=message,
        session_id=session_id,
        user_id="user-1",
        admin_id="admin-1",
        channel="widget",
        luciel_instance_id=7,
    )


def _run(orch, req):
    with patch("app.core.config.settings.knowledge_retrieval_enabled", False):
        return orch.run(req)


# =====================================================================
# HUMAN-CONTROLLED GATE — the load-bearing property.
# =====================================================================


class TestHumanControlledGateZeroLLMCalls(unittest.TestCase):
    """When control_mode='human_controlled', the orchestrator short-circuits
    before any LLM call. The gate must fire before budget, context assembly,
    and PLAN/ACT/REFLECT."""

    def _make_orch(self, router=None):
        if router is None:
            router = _ScriptedRouter([_plan_json(reply="should never be called")])
        return LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=router,
            tool_broker=_Broker(),
            escalation_judge=_judge(intent_class=INTENT_OTHER, confidence=0.0),
            escalation_service=_FakeEscalationSvc(),
        )

    def test_human_controlled_session_zero_llm_calls(self):
        """LOAD-BEARING: human_controlled session → zero LLM calls."""
        router = _ScriptedRouter([_plan_json(reply="should not be used")])
        orch = self._make_orch(router)

        req = _request("I have a question")

        # Patch the gate check to return True (session is human_controlled).
        with patch.object(orch, "_is_session_human_controlled", return_value=True), \
             patch("app.core.config.settings.knowledge_retrieval_enabled", False):
            resp = orch.run(req)

        # ZERO LLM calls — the agentic loop never ran.
        self.assertEqual(len(router.calls), 0, "LLM must not be called on human_controlled session")

    def test_human_controlled_turn_returns_zero_iterations(self):
        """human_controlled turn: iterations=0, llm_provider=None."""
        orch = self._make_orch()
        req = _request("another message")

        with patch.object(orch, "_is_session_human_controlled", return_value=True), \
             patch("app.core.config.settings.knowledge_retrieval_enabled", False):
            resp = orch.run(req)

        self.assertEqual(resp.iterations, 0)
        self.assertIsNone(resp.llm_provider)
        self.assertIsNone(resp.llm_model)
        self.assertFalse(resp.tool_called)

    def test_human_controlled_turn_intent_summary_marker(self):
        """human_controlled turn: intent_summary='human_controlled' so the
        dashboard live feed can identify these non-LLM turns."""
        orch = self._make_orch()
        req = _request()

        with patch.object(orch, "_is_session_human_controlled", return_value=True), \
             patch("app.core.config.settings.knowledge_retrieval_enabled", False):
            resp = orch.run(req)

        self.assertEqual(resp.intent_summary, "human_controlled")

    def test_human_controlled_turn_empty_reply_not_escalation(self):
        """human_controlled turn: no automated reply (empty string), not
        an escalation flag — the human admin replies manually."""
        orch = self._make_orch()
        req = _request()

        with patch.object(orch, "_is_session_human_controlled", return_value=True), \
             patch("app.core.config.settings.knowledge_retrieval_enabled", False):
            resp = orch.run(req)

        self.assertEqual(resp.message, "")
        self.assertFalse(resp.escalation_flag)

    def test_human_controlled_turn_session_id_preserved(self):
        """human_controlled turn: response carries the correct session_id."""
        orch = self._make_orch()
        req = _request(session_id="sess-special-42")

        with patch.object(orch, "_is_session_human_controlled", return_value=True), \
             patch("app.core.config.settings.knowledge_retrieval_enabled", False):
            resp = orch.run(req)

        self.assertEqual(resp.session_id, "sess-special-42")

    def test_normal_session_still_calls_llm(self):
        """Non-human_controlled session proceeds to the LLM as normal.
        Verifies the gate does NOT fire for control_mode='luciel'."""
        router = _ScriptedRouter([_plan_json(reply="hello from llm")])
        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=router,
            tool_broker=_Broker(),
            escalation_judge=_judge(intent_class=INTENT_OTHER, confidence=0.0),
            escalation_service=_FakeEscalationSvc(),
        )
        req = _request("just a normal message")

        with patch.object(orch, "_is_session_human_controlled", return_value=False), \
             patch("app.core.config.settings.knowledge_retrieval_enabled", False):
            resp = orch.run(req)

        # The LLM was called at least once (PLAN ran).
        self.assertGreater(len(router.calls), 0, "Normal session must call the LLM")
        self.assertEqual(resp.message, "hello from llm")

    def test_human_controlled_gate_fires_before_budget_meter(self):
        """The human_controlled gate short-circuits BEFORE the budget gate.
        Verifies that _budget_gate is never called on a human_controlled session."""
        orch = self._make_orch()
        req = _request()

        with patch.object(orch, "_is_session_human_controlled", return_value=True), \
             patch.object(orch, "_budget_gate") as mock_budget, \
             patch("app.core.config.settings.knowledge_retrieval_enabled", False):
            orch.run(req)

        mock_budget.assert_not_called()

    def test_human_controlled_gate_fires_before_intake_gate(self):
        """The human_controlled gate short-circuits BEFORE the intake gate.
        Verifies that _intake_gate is never called on a human_controlled session."""
        orch = self._make_orch()
        req = _request()

        with patch.object(orch, "_is_session_human_controlled", return_value=True), \
             patch.object(orch, "_intake_gate") as mock_intake, \
             patch("app.core.config.settings.knowledge_retrieval_enabled", False):
            orch.run(req)

        mock_intake.assert_not_called()


# =====================================================================
# _is_session_human_controlled — unit tests with injected DB check.
# =====================================================================


class TestIsSessionHumanControlled(unittest.TestCase):
    """Unit tests for the _is_session_human_controlled helper."""

    def _make_orch(self):
        return LucielOrchestrator(
            trace_service=_StubTrace(),
        )

    def test_returns_false_for_empty_session_id(self):
        orch = self._make_orch()
        result = orch._is_session_human_controlled("")
        self.assertFalse(result)

    def test_returns_false_for_none_session_id(self):
        orch = self._make_orch()
        result = orch._is_session_human_controlled(None)
        self.assertFalse(result)

    def test_returns_false_when_db_raises(self):
        """Degradation: DB failure → False (safe, never blocks a turn)."""
        orch = self._make_orch()
        with patch("app.db.session.SessionLocal", side_effect=Exception("db down")):
            result = orch._is_session_human_controlled("sess-xyz")
        self.assertFalse(result)

    def test_returns_false_when_session_not_found(self):
        """Session not in DB → False (never falsely gates)."""
        orch = self._make_orch()
        fake_db = MagicMock()
        fake_db.get.return_value = None
        with patch("app.db.session.SessionLocal", return_value=fake_db):
            result = orch._is_session_human_controlled("sess-missing")
        self.assertFalse(result)

    def test_returns_false_when_control_mode_luciel(self):
        """control_mode='luciel' → False (normal session)."""
        orch = self._make_orch()
        fake_session = MagicMock()
        fake_session.control_mode = "luciel"
        fake_db = MagicMock()
        fake_db.get.return_value = fake_session
        with patch("app.db.session.SessionLocal", return_value=fake_db):
            result = orch._is_session_human_controlled("sess-ok")
        self.assertFalse(result)

    def test_returns_true_when_control_mode_human_controlled(self):
        """control_mode='human_controlled' → True (gate fires)."""
        orch = self._make_orch()
        fake_session = MagicMock()
        fake_session.control_mode = "human_controlled"
        fake_db = MagicMock()
        fake_db.get.return_value = fake_session
        with patch("app.db.session.SessionLocal", return_value=fake_db):
            result = orch._is_session_human_controlled("sess-taken-over")
        self.assertTrue(result)


# =====================================================================
# Luciel-initiated path: explicit_human_request → human_controlled.
# =====================================================================


class TestLucielInitiatedTakeover(unittest.TestCase):
    """When INTAKE gate fires INTENT_REQUEST_HUMAN, the orchestrator:
      1. Emits the handoff ack reply (as before).
      2. Calls _set_session_human_controlled_best_effort with
         trigger='luciel_escalated'.
    """

    def test_explicit_human_request_calls_set_human_controlled(self):
        """Luciel-initiated path: _set_session_human_controlled_best_effort
        is called with trigger='luciel_escalated' when INTAKE fires."""
        router = _ScriptedRouter([_plan_json(reply="should not be used")])
        esc = _FakeEscalationSvc()
        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=router,
            tool_broker=_Broker(),
            escalation_judge=_judge(
                intent_class=INTENT_REQUEST_HUMAN, confidence=0.9
            ),
            escalation_service=esc,
        )
        req = _request("I want to speak to a human")

        with patch.object(orch, "_set_session_human_controlled_best_effort") as mock_set, \
             patch("app.core.config.settings.knowledge_retrieval_enabled", False):
            resp = orch.run(req)

        # PLAN never ran.
        self.assertEqual(len(router.calls), 0)
        # The session update was requested.
        mock_set.assert_called_once()
        call_kwargs = mock_set.call_args.kwargs
        self.assertEqual(call_kwargs["trigger"], "luciel_escalated")
        self.assertEqual(call_kwargs["session_id"], "sess-1")
        self.assertIsNone(call_kwargs["actor_user_id"])

    def test_non_human_request_does_not_call_set_human_controlled(self):
        """Other signals (e.g. INTENT_OTHER) do NOT trigger human_controlled."""
        router = _ScriptedRouter([_plan_json(reply="hello")])
        esc = _FakeEscalationSvc()
        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=router,
            tool_broker=_Broker(),
            escalation_judge=_judge(intent_class=INTENT_OTHER, confidence=0.0),
            escalation_service=esc,
        )
        req = _request("what are your hours?")

        with patch.object(orch, "_set_session_human_controlled_best_effort") as mock_set, \
             patch("app.core.config.settings.knowledge_retrieval_enabled", False):
            resp = orch.run(req)

        mock_set.assert_not_called()

    def test_explicit_human_request_ack_is_handoff_template(self):
        """Luciel-initiated path: the reply is the handoff ack template, not
        an LLM response."""
        from app.runtime.handoff_ack import handoff_acknowledgement

        router = _ScriptedRouter([_plan_json(reply="llm response that must not appear")])
        esc = _FakeEscalationSvc()
        orch = LucielOrchestrator(
            trace_service=_StubTrace(),
            model_router=router,
            tool_broker=_Broker(),
            escalation_judge=_judge(
                intent_class=INTENT_REQUEST_HUMAN, confidence=0.95
            ),
            escalation_service=esc,
        )
        req = _request("please connect me to someone")

        with patch.object(orch, "_set_session_human_controlled_best_effort"), \
             patch("app.core.config.settings.knowledge_retrieval_enabled", False):
            resp = orch.run(req)

        expected_ack = handoff_acknowledgement()
        self.assertEqual(resp.message, expected_ack)
        self.assertTrue(resp.escalation_flag)


# =====================================================================
# Audit constants — verify ACTION_HUMAN_TAKEOVER_STARTED/ENDED exist
# in the ALLOWED_ACTIONS tuple.
# =====================================================================


class TestAuditConstants(unittest.TestCase):

    def test_action_human_takeover_started_exists(self):
        from app.models.admin_audit_log import ACTION_HUMAN_TAKEOVER_STARTED
        self.assertEqual(ACTION_HUMAN_TAKEOVER_STARTED, "human_takeover_started")

    def test_action_human_takeover_ended_exists(self):
        from app.models.admin_audit_log import ACTION_HUMAN_TAKEOVER_ENDED
        self.assertEqual(ACTION_HUMAN_TAKEOVER_ENDED, "human_takeover_ended")

    def test_both_constants_in_allowed_actions(self):
        from app.models.admin_audit_log import (
            ACTION_HUMAN_TAKEOVER_STARTED,
            ACTION_HUMAN_TAKEOVER_ENDED,
            ALLOWED_ACTIONS,
        )
        self.assertIn(ACTION_HUMAN_TAKEOVER_STARTED, ALLOWED_ACTIONS)
        self.assertIn(ACTION_HUMAN_TAKEOVER_ENDED, ALLOWED_ACTIONS)


# =====================================================================
# Session model — verify new columns exist.
# =====================================================================


class TestSessionModelColumns(unittest.TestCase):

    def test_control_mode_column_exists(self):
        from app.models.session import SessionModel
        self.assertTrue(hasattr(SessionModel, "control_mode"))

    def test_taken_over_by_user_id_column_exists(self):
        from app.models.session import SessionModel
        self.assertTrue(hasattr(SessionModel, "taken_over_by_user_id"))

    def test_taken_over_at_column_exists(self):
        from app.models.session import SessionModel
        self.assertTrue(hasattr(SessionModel, "taken_over_at"))

    def test_handed_back_at_column_exists(self):
        from app.models.session import SessionModel
        self.assertTrue(hasattr(SessionModel, "handed_back_at"))

    def test_control_mode_default_is_luciel(self):
        """The default for control_mode is 'luciel' so existing sessions
        are unaffected by the migration."""
        from app.models.session import SessionModel
        col = SessionModel.__table__.c["control_mode"]
        # server_default should be 'luciel'
        self.assertIn("luciel", str(col.server_default.arg))


if __name__ == "__main__":
    unittest.main()
