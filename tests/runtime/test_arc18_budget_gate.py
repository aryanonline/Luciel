"""Arc 18 — orchestrator conversation-budget gate wiring (§3.4.1b).

End-to-end through ``LucielOrchestrator.run`` with deterministic fakes
(no DB, no network, InMemoryBackend meter):

  * Free at/over cap → graceful templated reply, NO LLM call, escalation
    flag set, llm_provider None, 0 iterations. The customer is never
    silently dropped.
  * A session WITHIN budget proceeds into PLAN normally and is counted
    once.
  * An INTAKE-escalated session (Gate 1) returns BEFORE the budget gate
    and therefore NEVER consumes budget (the meter is never incremented).
  * Pro over cap NEVER blocks — the loop runs, the customer reply is
    emitted, and the 80%/100% alerts fire once each.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.integrations.llm.base import LLMResponse
from app.policy.entitlements import (
    CADENCE_MONTHLY,
    TIER_FREE,
    TIER_PRO,
)
from app.runtime.billing_period import BillingContext
from app.runtime.budget_ack import budget_exhausted_acknowledgement
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
# Test doubles (mirror tests/runtime/test_arc14_u2_escalation_gates.py).
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


def _orch(*, router, meter, alert_svc, judge=None, esc=None, broker=None):
    return LucielOrchestrator(
        trace_service=_StubTrace(),
        model_router=router,
        tool_broker=broker or _Broker(),
        escalation_judge=judge or _judge(),
        escalation_service=esc or _FakeEscalationSvc(),
        budget_meter=meter,
        budget_alert_service=alert_svc,
    )


class _Ctx:
    """Patch helper: a fixed BillingContext + a no-op DB session, so the
    gate never touches a real database."""

    def __init__(self, tier, cadence=CADENCE_MONTHLY, period_start="2026-06-01"):
        self.ctx = BillingContext(tier=tier, cadence=cadence, period_start=period_start)

    def __enter__(self):
        self._patches = [
            patch("app.core.config.settings.knowledge_retrieval_enabled", False),
            patch(
                "app.runtime.billing_period.resolve_billing_context",
                return_value=self.ctx,
            ),
            patch("app.db.session.SessionLocal", side_effect=_FakeSession),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


class _FakeSession:
    """A DB session the best-effort audit/notify paths can open + close
    without a real engine; all queries no-op."""

    def execute(self, *a, **k):
        raise RuntimeError("no DB in this test")

    def close(self):
        pass


# =====================================================================
# Free at cap → graceful short-circuit, no LLM.
# =====================================================================


class TestFreeBudgetExhausted(unittest.TestCase):

    def test_free_at_cap_short_circuits_with_no_llm(self):
        # Pre-load the meter so this session pushes the count to 201 > 200.
        backend = InMemoryBackend()
        meter = BudgetMeter(backend=backend)
        for i in range(200):
            meter.count_session_once(
                admin_id="admin-1", instance_id=7,
                period_start="2026-06-01", session_id=f"seed-{i}",
            )
        router = _ScriptedRouter([_plan_json(reply="SHOULD NOT BE USED")])
        alert_svc = _FakeAlertSvc()
        orch = _orch(router=router, meter=meter, alert_svc=alert_svc)

        with _Ctx(TIER_FREE):
            resp = orch.run(_request(session_id="over"))

        # No LLM, no tool, templated graceful reply, escalation flag.
        self.assertEqual(len(router.calls), 0)
        self.assertEqual(resp.message, budget_exhausted_acknowledgement())
        self.assertTrue(resp.escalation_flag)
        self.assertIsNone(resp.llm_provider)
        self.assertEqual(resp.iterations, 0)
        # The Free admin was notified (exhausted) — never the customer.
        self.assertTrue(
            any(a.get("exhausted") for a in alert_svc.alerts),
            alert_svc.alerts,
        )

    def test_free_within_budget_proceeds_into_plan(self):
        meter = BudgetMeter(backend=InMemoryBackend())
        router = _ScriptedRouter([_plan_json(reply="real answer", confidence=0.95)])
        orch = _orch(router=router, meter=meter, alert_svc=_FakeAlertSvc())

        with _Ctx(TIER_FREE):
            resp = orch.run(_request(session_id="ok"))

        self.assertEqual(len(router.calls), 1)
        self.assertEqual(resp.message, "real answer")
        self.assertFalse(resp.escalation_flag)
        # Counted exactly once.
        self.assertEqual(
            meter.current_count(
                admin_id="admin-1", instance_id=7, period_start="2026-06-01"
            ),
            1,
        )


# =====================================================================
# Intake-escalated session NEVER consumes budget.
# =====================================================================


class TestIntakeBypassesBudget(unittest.TestCase):

    def test_intake_escalation_does_not_increment_meter(self):
        meter = BudgetMeter(backend=InMemoryBackend())
        router = _ScriptedRouter([_plan_json()])
        orch = _orch(
            router=router,
            meter=meter,
            alert_svc=_FakeAlertSvc(),
            judge=_judge(intent_class=INTENT_REQUEST_HUMAN, confidence=0.9),
        )

        with _Ctx(TIER_FREE):
            resp = orch.run(_request("I want a human"))

        self.assertTrue(resp.escalation_flag)
        self.assertEqual(len(router.calls), 0)
        # The budget gate was never reached → counter stays at 0.
        self.assertEqual(
            meter.current_count(
                admin_id="admin-1", instance_id=7, period_start="2026-06-01"
            ),
            0,
        )


# =====================================================================
# Pro over cap NEVER blocks; alerts fire once each.
# =====================================================================


class TestProOverCapContinues(unittest.TestCase):

    def test_pro_over_cap_runs_loop_and_fires_alerts(self):
        # Seed to cap so this session is the one that tips over 1000 (Pro Monthly).
        backend = InMemoryBackend()
        meter = BudgetMeter(backend=backend)
        # Cheat the counter directly to just under cap to keep the test fast.
        backend.incr_with_ttl(
            "luciel:budget:count:admin-1:7:2026-06-01", 999_999
        )
        backend._store["luciel:budget:count:admin-1:7:2026-06-01"] = (
            "1000",
            backend._store["luciel:budget:count:admin-1:7:2026-06-01"][1],
        )
        router = _ScriptedRouter([_plan_json(reply="served", confidence=0.95)])
        alert_svc = _FakeAlertSvc()
        orch = _orch(router=router, meter=meter, alert_svc=alert_svc)

        with _Ctx(TIER_PRO):
            resp = orch.run(_request(session_id="pro-over"))

        # Pro is NEVER blocked — the loop ran and the real reply is emitted.
        self.assertEqual(len(router.calls), 1)
        self.assertEqual(resp.message, "served")
        self.assertFalse(resp.escalation_flag)
        # At 1001/1000 both 80% and 100% thresholds fire, once each.
        thresholds = sorted(a["threshold"] for a in alert_svc.alerts)
        self.assertEqual(thresholds, [80, 100])

    def test_pro_alerts_are_idempotent_across_sessions(self):
        backend = InMemoryBackend()
        meter = BudgetMeter(backend=backend)
        backend._store["luciel:budget:count:admin-1:7:2026-06-01"] = (
            "1000", float("inf"),
        )
        alert_svc = _FakeAlertSvc()

        for sess in ("p1", "p2"):
            router = _ScriptedRouter([_plan_json(reply="served", confidence=0.95)])
            orch = _orch(router=router, meter=meter, alert_svc=alert_svc)
            with _Ctx(TIER_PRO):
                orch.run(_request(session_id=sess))

        # Two sessions both over cap, but each threshold alert fires ONCE.
        thresholds = sorted(a["threshold"] for a in alert_svc.alerts)
        self.assertEqual(thresholds, [80, 100])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
