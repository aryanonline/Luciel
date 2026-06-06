"""Unit 7 — loop order, budget-increment timing, and tier-aware routing.

Regression coverage for the three §3.4.1 alignment changes:

  1. Loop order BUDGET GATE → INTAKE GATE 1 → RETRIEVE → PLAN. Retrieval
     is NOT called when the budget gate short-circuits (Free at cap) nor
     when the intake gate fires — the diagram's whole point is that a
     short-circuited turn pays ZERO retrieval cost.

  2. Budget increment timing (§3.4.1b line 454). The budget gate is now a
     READ-ONLY peek; the increment fires on the FIRST model call in the
     PLAN path. A Free-at-cap turn makes NO LLM call AND does NOT
     increment. A Free-under-cap turn increments exactly once even across
     multiple PLAN iterations. A Pro turn increments once + proceeds.

  3. Tier wiring. The PLAN path calls ``router.generate`` WITH a ``tier``
     kwarg (the test that would have caught the pre-Unit-7 wiring gap).

All deterministic: InMemoryBackend meter, scripted/recording router,
fake broker — no live Redis, no DB (billing context + DB session patched).
Pattern mirrors tests/runtime/test_arc14_u1_agentic_loop.py +
tests/runtime/test_arc18_budget_gate.py.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.integrations.llm.base import LLMResponse
from app.policy.entitlements import CADENCE_MONTHLY, TIER_FREE, TIER_PRO
from app.runtime.billing_period import BillingContext
from app.billing.metering import BudgetMeter, InMemoryBackend
from app.runtime.classifiers import (
    INTENT_OTHER,
    INTENT_REQUEST_HUMAN,
    IntentResult,
    SentimentResult,
)
from app.runtime.contracts import RuntimeRequest
from app.runtime.escalation_judge import EscalationJudge
from app.runtime.orchestrator import LucielOrchestrator
from app.tools.base import ToolResult


# ---------------------------------------------------------------------
# Test doubles.
# ---------------------------------------------------------------------


class _RecordingRouter:
    """ModelRouter stand-in that records the kwargs of every generate
    call (so a test can assert ``tier`` reached the router) and returns a
    scripted sequence of PLAN responses, last entry reused."""

    def __init__(self, contents, *, provider="stub", model="stub-x"):
        self._contents = contents
        self._provider = provider
        self._model = model
        self.calls = []
        self.call_kwargs = []

    def generate(self, request, *, preferred_provider=None, **kwargs):
        idx = min(len(self.calls), len(self._contents) - 1)
        self.calls.append(request)
        self.call_kwargs.append(dict(kwargs, preferred_provider=preferred_provider))
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


class _FakeAlertSvc:
    def __init__(self):
        self.alerts = []

    def send_budget_alert(self, **kwargs):
        self.alerts.append(kwargs)


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


def _plan_json(reply="planned answer", tool_calls=None, confidence=0.9):
    return json.dumps(
        {"reply": reply, "tool_calls": tool_calls or [], "confidence": confidence}
    )


def _request(message="hello", session_id="sess-1", instance_id=7):
    return RuntimeRequest(
        message=message,
        session_id=session_id,
        user_id="user-1",
        admin_id="admin-1",
        channel="widget",
        luciel_instance_id=instance_id,
    )


def _orch(*, router, meter, alert_svc=None, judge=None, esc=None, broker=None):
    return LucielOrchestrator(
        trace_service=_StubTrace(),
        model_router=router,
        tool_broker=broker or _Broker(),
        escalation_judge=judge or _judge(),
        escalation_service=esc or _FakeEscalationSvc(),
        budget_meter=meter,
        budget_alert_service=alert_svc or _FakeAlertSvc(),
    )


class _FakeSession:
    def execute(self, *a, **k):
        raise RuntimeError("no DB in this test")

    def close(self):
        pass


class _Ctx:
    """Patch helper: fixed tier (resolve_tier + billing context), retrieval
    flag ON so a RETRIEVE call WOULD happen unless short-circuited, and a
    recording stub for ``_retrieve`` so the test can assert it was/wasn't
    called. The no-op DB session keeps best-effort audit/notify paths from
    touching a real engine."""

    def __init__(self, orch, tier, *, period_start="2026-06-01"):
        self.orch = orch
        self.tier = tier
        self.ctx = BillingContext(
            tier=tier, cadence=CADENCE_MONTHLY, period_start=period_start
        )
        self.retrieve_calls = []

    def _record_retrieve(self, req):
        self.retrieve_calls.append(req)
        return []

    def __enter__(self):
        self._patches = [
            # Retrieval flag ON: if the loop reached RETRIEVE it WOULD call
            # _retrieve (our spy). A short-circuit leaves retrieve_calls empty.
            patch("app.core.config.settings.knowledge_retrieval_enabled", True),
            patch(
                "app.runtime.billing_period.resolve_billing_context",
                return_value=self.ctx,
            ),
            patch.object(self.orch, "_resolve_tier", return_value=self.tier),
            patch.object(self.orch, "_retrieve", side_effect=self._record_retrieve),
            patch("app.db.session.SessionLocal", side_effect=_FakeSession),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


def _count(meter, period_start="2026-06-01"):
    return meter.current_count(
        admin_id="admin-1", instance_id=7, period_start=period_start
    )


# =====================================================================
# CHANGE 1 — loop order: RETRIEVE runs AFTER the gates.
# =====================================================================


class TestLoopOrderShortCircuitSkipsRetrieve(unittest.TestCase):

    def test_free_at_cap_short_circuit_does_not_retrieve(self):
        meter = BudgetMeter(backend=InMemoryBackend())
        for i in range(200):  # seed to the Free cap (200)
            meter.count_session_once(
                admin_id="admin-1", instance_id=7,
                period_start="2026-06-01", session_id=f"seed-{i}",
            )
        router = _RecordingRouter([_plan_json(reply="SHOULD NOT BE USED")])
        orch = _orch(router=router, meter=meter)

        with _Ctx(orch, TIER_FREE) as ctx:
            resp = orch.run(_request(session_id="over"))

        # Budget gate fired FIRST → no retrieval, no LLM call.
        self.assertEqual(ctx.retrieve_calls, [])
        self.assertEqual(len(router.calls), 0)
        self.assertTrue(resp.escalation_flag)
        self.assertEqual(resp.source_ids_used, [])

    def test_intake_escalation_does_not_retrieve(self):
        meter = BudgetMeter(backend=InMemoryBackend())
        router = _RecordingRouter([_plan_json()])
        orch = _orch(
            router=router,
            meter=meter,
            judge=_judge(intent_class=INTENT_REQUEST_HUMAN, confidence=0.9),
        )

        with _Ctx(orch, TIER_FREE) as ctx:
            resp = orch.run(_request("I want a human"))

        # Intake gate fired before RETRIEVE → no retrieval, no LLM call.
        self.assertTrue(resp.escalation_flag)
        self.assertEqual(ctx.retrieve_calls, [])
        self.assertEqual(len(router.calls), 0)
        self.assertEqual(resp.source_ids_used, [])


# =====================================================================
# CHANGE 2 — budget increment timing (§3.4.1b line 454).
# =====================================================================


class TestBudgetIncrementTiming(unittest.TestCase):

    def test_free_at_cap_does_not_increment_and_makes_no_llm_call(self):
        meter = BudgetMeter(backend=InMemoryBackend())
        for i in range(200):
            meter.count_session_once(
                admin_id="admin-1", instance_id=7,
                period_start="2026-06-01", session_id=f"seed-{i}",
            )
        self.assertEqual(_count(meter), 200)
        router = _RecordingRouter([_plan_json(reply="SHOULD NOT BE USED")])
        orch = _orch(router=router, meter=meter)

        with _Ctx(orch, TIER_FREE):
            orch.run(_request(session_id="over"))

        # Read-only peek denied the turn; counter UNCHANGED, no LLM call.
        self.assertEqual(_count(meter), 200)
        self.assertEqual(len(router.calls), 0)

    def test_free_under_cap_increments_exactly_once_across_iterations(self):
        # A multi-iteration PLAN loop (tool fails → REFLECT re-enters) must
        # still count the conversation exactly ONCE (idempotency holds even
        # though the increment now lives in the loop).
        meter = BudgetMeter(backend=InMemoryBackend())
        failing = ToolResult(success=False, output="", error="boom")
        router = _RecordingRouter(
            [_plan_json(tool_calls=[{"tool": "push_to_crm", "parameters": {}}])]
        )
        orch = _orch(
            router=router,
            meter=meter,
            broker=_Broker({"push_to_crm": failing}),
        )

        with _Ctx(orch, TIER_FREE):
            resp = orch.run(_request(session_id="multi"))

        # The tool keeps failing → 5 PLAN iterations, but ONE budget unit.
        self.assertEqual(resp.iterations, 5)
        self.assertGreater(len(router.calls), 1)
        self.assertEqual(_count(meter), 1)

    def test_pro_increments_once_and_proceeds(self):
        meter = BudgetMeter(backend=InMemoryBackend())
        router = _RecordingRouter([_plan_json(reply="served", confidence=0.95)])
        orch = _orch(router=router, meter=meter)

        with _Ctx(orch, TIER_PRO):
            resp = orch.run(_request(session_id="pro-1"))

        self.assertEqual(resp.message, "served")
        self.assertFalse(resp.escalation_flag)
        self.assertEqual(len(router.calls), 1)
        self.assertEqual(_count(meter), 1)

    def test_pro_over_cap_increments_and_fires_alerts_after_increment(self):
        backend = InMemoryBackend()
        meter = BudgetMeter(backend=backend)
        # Seed just under the Pro cap (1000) so this session tips to 1001.
        backend._store["luciel:budget:count:admin-1:7:2026-06-01"] = (
            "1000", float("inf"),
        )
        router = _RecordingRouter([_plan_json(reply="served", confidence=0.95)])
        alert_svc = _FakeAlertSvc()
        orch = _orch(router=router, meter=meter, alert_svc=alert_svc)

        with _Ctx(orch, TIER_PRO):
            resp = orch.run(_request(session_id="pro-over"))

        # Pro is never blocked; the increment fired (1001) and BOTH the
        # 80% and 100% alerts fired once each on the post-increment count.
        self.assertEqual(resp.message, "served")
        self.assertEqual(_count(meter), 1001)
        thresholds = sorted(a["threshold"] for a in alert_svc.alerts)
        self.assertEqual(thresholds, [80, 100])


# =====================================================================
# CHANGE 3 — tier-aware routing reaches the router.
# =====================================================================


class TestTierWiringReachesRouter(unittest.TestCase):

    def test_free_plan_call_passes_tier_free(self):
        meter = BudgetMeter(backend=InMemoryBackend())
        router = _RecordingRouter([_plan_json(reply="ok", confidence=0.95)])
        orch = _orch(router=router, meter=meter)

        with _Ctx(orch, TIER_FREE):
            orch.run(_request(session_id="free-1"))

        self.assertEqual(len(router.call_kwargs), 1)
        kw = router.call_kwargs[0]
        # The PLAN path passes tier (the wiring-gap regression) + signals.
        self.assertEqual(kw.get("tier"), "free")
        self.assertIn("user_message", kw)
        self.assertIn("context_token_estimate", kw)
        self.assertIn("has_tools", kw)

    def test_pro_plan_call_passes_tier_pro(self):
        meter = BudgetMeter(backend=InMemoryBackend())
        router = _RecordingRouter([_plan_json(reply="ok", confidence=0.95)])
        orch = _orch(router=router, meter=meter)

        with _Ctx(orch, TIER_PRO):
            orch.run(_request(session_id="pro-w"))

        self.assertEqual(router.call_kwargs[0].get("tier"), "pro")


# =====================================================================
# CHANGE 3 (follow-up) — instance-level has_tools reaches the router.
#
# has_tools means "does this instance have ANY authorized tools?",
# resolved ONCE by the budget gate (on its already-open billing-context
# session) and threaded to the router via _BudgetPlanContext. An instance
# with zero authorized tools can never need a tool this turn, so fast
# routing is provably safe → has_tools=False. One with ≥1 stays
# conservative → has_tools=True. We patch the repo's
# ``list_authorized_tool_ids`` so the real _instance_has_authorized_tools
# path runs (instance-id resolution + len()>0) without a live DB row.
# =====================================================================


class TestHasToolsReachesRouter(unittest.TestCase):

    _REPO = (
        "app.repositories.instance_tool_authorization_repository"
        ".InstanceToolAuthorizationRepository.list_authorized_tool_ids"
    )

    def test_zero_authorized_tools_passes_has_tools_false(self):
        meter = BudgetMeter(backend=InMemoryBackend())
        router = _RecordingRouter([_plan_json(reply="ok", confidence=0.95)])
        orch = _orch(router=router, meter=meter)

        with _Ctx(orch, TIER_PRO):
            with patch(self._REPO, return_value=set()):
                orch.run(_request(session_id="no-tools"))

        self.assertEqual(router.call_kwargs[0].get("has_tools"), False)

    def test_one_authorized_tool_passes_has_tools_true(self):
        meter = BudgetMeter(backend=InMemoryBackend())
        router = _RecordingRouter([_plan_json(reply="ok", confidence=0.95)])
        orch = _orch(router=router, meter=meter)

        with _Ctx(orch, TIER_PRO):
            with patch(self._REPO, return_value={"push_to_crm"}):
                orch.run(_request(session_id="has-tools"))

        self.assertEqual(router.call_kwargs[0].get("has_tools"), True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
