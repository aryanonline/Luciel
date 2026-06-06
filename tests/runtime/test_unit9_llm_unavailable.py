"""Unit 9 (part 2) — llm_unavailable escalation (Architecture line 1354).

When BOTH LLM providers are down the router raises
``RuntimeError("All LLM providers failed ...")``. The orchestrator's PLAN
firewall must detect THAT specific case (not any LLM failure), and the
TURN layer must:

  * return the EXACT canonical phrase
    "I'm having trouble right now — I've let the team know.";
  * fire an escalation with signal=SIGNAL_LLM_UNAVAILABLE at GATE_OUTCOME;
  * set the response escalation flag True;
  * attempt admin notification via the delivery service (best-effort).

A DIFFERENT exception (e.g. a generic ValueError, or a RuntimeError whose
message is NOT "All LLM providers failed") must NOT fire llm_unavailable:
the generic degraded reply path still applies. This guards the
discrimination logic.

Plus a live-PG assertion that SIGNAL_LLM_UNAVAILABLE is in ALLOWED_SIGNALS
and an escalation_event row carrying it persists without a CHECK violation.

Deterministic where it can be: fake router/broker/judge/escalation-service,
no live Redis, billing-context + DB session patched (mirrors
tests/runtime/test_unit7_loop_order_budget_routing.py). The CHECK-violation
test is @skipUnless(_LIVE) so it RUNS under run_tests.sh's live PG.
"""
from __future__ import annotations

import os
import unittest
import uuid
from unittest.mock import patch

os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("OPENAI_API_KEY", "dummy")

from app.policy.entitlements import CADENCE_MONTHLY, TIER_PRO
from app.runtime.billing_period import BillingContext
from app.billing.metering import BudgetMeter, InMemoryBackend
from app.runtime.classifiers import (
    INTENT_OTHER,
    IntentResult,
    SentimentResult,
)
from app.runtime.contracts import RuntimeRequest
from app.runtime.escalation_judge import EscalationJudge
from app.runtime.orchestrator import LucielOrchestrator

_DB_URL = os.environ.get("DATABASE_URL", "")
_LIVE = _DB_URL.startswith("postgresql+psycopg://") or bool(
    os.environ.get("LUCIEL_LIVE_POSTGRES_URL")
)


# ---------------------------------------------------------------------
# Test doubles.
# ---------------------------------------------------------------------


class _BoomRouter:
    """ModelRouter stand-in whose generate() always raises a supplied
    exception, so the PLAN firewall is exercised on every iteration."""

    def __init__(self, exc: Exception):
        self._exc = exc
        self.calls = 0

    def generate(self, request, *, preferred_provider=None, **kwargs):
        self.calls += 1
        raise self._exc


class _Broker:
    def execute_tool(self, tool_name, parameters=None, *, context=None, **extra):
        raise AssertionError("no tool should run when PLAN never produces one")


class _StubTrace:
    def __init__(self):
        self.calls = []

    def record_trace(self, **kwargs):
        self.calls.append(kwargs)
        return "trace-fixed-id"


class _RoutingFake:
    """EscalationService stand-in: records the decision and returns a
    routing object with a non-empty channel set so
    _record_escalation_best_effort proceeds to call the delivery spy."""

    def __init__(self):
        self.recorded = []

    def record_escalation(self, decision):
        from app.policy.escalation import EscalationRouting

        self.recorded.append(decision)
        return EscalationRouting(
            tier=TIER_PRO, channels=("email", "sms"), event_id=4242
        )


class _DeliverySpy:
    def __init__(self):
        self.delivered = []

    def deliver(self, **kwargs):
        self.delivered.append(kwargs)


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


def _judge():
    # Non-firing judge: no intake signal, and the outcome gate (if it ran)
    # would not fire either. The point is to prove llm_unavailable fires
    # independently of the judge.
    return EscalationJudge(
        intent_classifier=_FixedIntent(INTENT_OTHER, 0.0),
        sentiment_classifier=_FixedSentiment(0.0),
    )


def _request(message="hello", session_id="sess-llm", instance_id=7):
    return RuntimeRequest(
        message=message,
        session_id=session_id,
        user_id="user-1",
        admin_id="admin-1",
        channel="widget",
        luciel_instance_id=instance_id,
    )


def _orch(*, router, esc, delivery):
    return LucielOrchestrator(
        trace_service=_StubTrace(),
        model_router=router,
        tool_broker=_Broker(),
        escalation_judge=_judge(),
        escalation_service=esc,
        escalation_delivery_service=delivery,
        budget_meter=BudgetMeter(backend=InMemoryBackend()),
    )


class _FakeSession:
    def execute(self, *a, **k):
        raise RuntimeError("no DB in this test")

    def close(self):
        pass


class _Ctx:
    """Patch helper: fixed tier, retrieval flag OFF (no retrieval needed),
    no-op DB session so best-effort audit/notify never touch a real engine."""

    def __init__(self, orch, tier=TIER_PRO, *, period_start="2026-06-01"):
        self.orch = orch
        self.tier = tier
        self.ctx = BillingContext(
            tier=tier, cadence=CADENCE_MONTHLY, period_start=period_start
        )

    def __enter__(self):
        self._patches = [
            patch("app.core.config.settings.knowledge_retrieval_enabled", False),
            patch(
                "app.runtime.billing_period.resolve_billing_context",
                return_value=self.ctx,
            ),
            patch.object(self.orch, "_resolve_tier", return_value=self.tier),
            patch("app.db.session.SessionLocal", side_effect=_FakeSession),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


# =====================================================================
# All-providers-down → llm_unavailable escalation fires.
# =====================================================================


class TestLlmUnavailableFires(unittest.TestCase):

    def test_all_providers_down_fires_escalation_and_canonical_reply(self):
        from app.models.escalation_event import (
            GATE_OUTCOME,
            SIGNAL_LLM_UNAVAILABLE,
        )
        from app.runtime.handoff import llm_unavailable_reply

        router = _BoomRouter(
            RuntimeError("All LLM providers failed. Errors: anthropic: X; openai: Y")
        )
        esc = _RoutingFake()
        delivery = _DeliverySpy()
        orch = _orch(router=router, esc=esc, delivery=delivery)

        with _Ctx(orch):
            resp = orch.run(_request(session_id="all-down"))

        # (a) EXACT canonical phrase.
        self.assertEqual(resp.message, llm_unavailable_reply())
        self.assertEqual(
            resp.message, "I'm having trouble right now — I've let the team know."
        )
        # (b) escalation recorded with SIGNAL_LLM_UNAVAILABLE @ GATE_OUTCOME.
        self.assertEqual(len(esc.recorded), 1)
        decision = esc.recorded[0]
        self.assertEqual(decision.signal, SIGNAL_LLM_UNAVAILABLE)
        self.assertEqual(decision.gate, GATE_OUTCOME)
        self.assertEqual(decision.signal_confidence, 1.0)
        # (c) response escalation flag True.
        self.assertTrue(resp.escalation_flag)
        # (d) admin notification attempted via the delivery service.
        self.assertEqual(len(delivery.delivered), 1)
        self.assertEqual(
            delivery.delivered[0]["signal"], SIGNAL_LLM_UNAVAILABLE
        )

    def test_fires_exactly_once_despite_multiple_plan_iterations(self):
        # The firewall sets the flag every iteration, but the escalation is
        # at the TURN layer (post-loop) so it records exactly once.
        router = _BoomRouter(RuntimeError("All LLM providers failed. Errors: ..."))
        esc = _RoutingFake()
        delivery = _DeliverySpy()
        orch = _orch(router=router, esc=esc, delivery=delivery)

        with _Ctx(orch):
            orch.run(_request(session_id="once"))

        self.assertEqual(len(esc.recorded), 1)
        self.assertEqual(len(delivery.delivered), 1)


# =====================================================================
# A DIFFERENT exception must NOT fire llm_unavailable.
# =====================================================================


class TestDiscrimination(unittest.TestCase):

    def _assert_not_llm_unavailable(self, resp, esc):
        from app.models.escalation_event import SIGNAL_LLM_UNAVAILABLE

        # The canonical llm_unavailable phrase must NOT be the reply.
        self.assertNotEqual(
            resp.message, "I'm having trouble right now — I've let the team know."
        )
        # No escalation recorded carries the llm_unavailable signal. (The
        # ordinary OUTCOME gate may STILL fire cannot_confidently_answer
        # off the degraded confidence/grounding — that is correct existing
        # behavior; what must NOT happen is the llm_unavailable branch.)
        for decision in esc.recorded:
            self.assertNotEqual(decision.signal, SIGNAL_LLM_UNAVAILABLE)

    def test_generic_runtime_error_does_not_fire_llm_unavailable(self):
        # A RuntimeError whose message is NOT "All LLM providers failed"
        # is an ordinary degraded turn — no llm_unavailable escalation.
        router = _BoomRouter(RuntimeError("connection reset by peer"))
        esc = _RoutingFake()
        delivery = _DeliverySpy()
        orch = _orch(router=router, esc=esc, delivery=delivery)

        with _Ctx(orch):
            resp = orch.run(_request(session_id="generic-rt"))

        self._assert_not_llm_unavailable(resp, esc)
        for d in delivery.delivered:
            from app.models.escalation_event import SIGNAL_LLM_UNAVAILABLE

            self.assertNotEqual(d["signal"], SIGNAL_LLM_UNAVAILABLE)

    def test_non_runtime_exception_does_not_fire_llm_unavailable(self):
        # A non-RuntimeError (even if the message mentions providers) must
        # not match the isinstance(exc, RuntimeError) discrimination.
        router = _BoomRouter(ValueError("All LLM providers failed lookalike"))
        esc = _RoutingFake()
        delivery = _DeliverySpy()
        orch = _orch(router=router, esc=esc, delivery=delivery)

        with _Ctx(orch):
            resp = orch.run(_request(session_id="value-err"))

        self._assert_not_llm_unavailable(resp, esc)


# =====================================================================
# Signal vocabulary + DB CHECK.
# =====================================================================


class TestSignalVocabulary(unittest.TestCase):

    def test_signal_in_allowed_signals(self):
        from app.models.escalation_event import (
            ALLOWED_SIGNALS,
            SIGNAL_LLM_UNAVAILABLE,
        )

        self.assertIn(SIGNAL_LLM_UNAVAILABLE, ALLOWED_SIGNALS)


@unittest.skipUnless(
    _LIVE,
    "Requires DATABASE_URL=postgresql+psycopg://... or LUCIEL_LIVE_POSTGRES_URL",
)
class TestLlmUnavailablePersists(unittest.TestCase):
    """Live-PG: an escalation_event row with signal='llm_unavailable'
    must persist without tripping ck_escalation_events_signal (proves the
    widening migration ran)."""

    @classmethod
    def setUpClass(cls) -> None:
        from app.db.session import SessionLocal

        cls.SessionLocal = SessionLocal

    def setUp(self) -> None:
        self.db = self.SessionLocal()
        self._admin_ids: list[str] = []
        self._event_ids: list[int] = []

    def tearDown(self) -> None:
        self.db.rollback()
        self.db.close()
        self._purge()

    def _purge(self) -> None:
        from app.models.escalation_event import EscalationEvent
        from app.models.instance import Instance

        cleanup = self.SessionLocal()
        try:
            if self._event_ids:
                cleanup.query(EscalationEvent).filter(
                    EscalationEvent.id.in_(self._event_ids)
                ).delete(synchronize_session=False)
            if self._admin_ids:
                cleanup.query(Instance).filter(
                    Instance.admin_id.in_(self._admin_ids)
                ).delete(synchronize_session=False)
            cleanup.commit()
        except Exception:
            cleanup.rollback()
        finally:
            cleanup.close()

    def test_escalation_event_with_llm_unavailable_persists(self):
        from app.models.admin import Admin
        from app.models.escalation_event import (
            EscalationEvent,
            GATE_OUTCOME,
            SIGNAL_LLM_UNAVAILABLE,
        )
        from app.models.instance import Instance

        admin_id = f"unit9llm-{uuid.uuid4().hex[:10]}"
        self._admin_ids.append(admin_id)
        self.db.add(Admin(id=admin_id, name="unit9 llm", tier="pro", active=True))
        self.db.flush()
        inst = Instance(
            admin_id=admin_id,
            instance_slug=f"i-{uuid.uuid4().hex[:8]}",
            display_name="unit9 llm instance",
        )
        self.db.add(inst)
        self.db.flush()

        row = EscalationEvent(
            admin_id=admin_id,
            luciel_instance_id=inst.id,
            session_id=str(uuid.uuid4()),
            signal=SIGNAL_LLM_UNAVAILABLE,
            gate=GATE_OUTCOME,
            signal_confidence=1.0,
            reasoning_excerpt="All LLM providers unavailable",
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        self._event_ids.append(row.id)

        self.assertIsNotNone(row.id)
        self.assertEqual(row.signal, SIGNAL_LLM_UNAVAILABLE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
