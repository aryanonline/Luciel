"""Arc 14 U2 — §3.4.5 EscalationJudge signal-boundary tests.

Pure-decision tests: the judge reads classifier outputs + loop state and
returns an ``EscalationDecision`` (or None). No DB, no network — the
classifiers are deterministic fakes (founder decision #2). Each of the
four FIXED signals is exercised at its exact doctrinal boundary.
"""
from __future__ import annotations

import unittest

from app.models.escalation_event import (
    GATE_INTAKE,
    GATE_OUTCOME,
    SIGNAL_CANNOT_CONFIDENTLY_ANSWER,
    SIGNAL_EXPLICIT_HUMAN_REQUEST,
    SIGNAL_HIGH_VALUE_LEAD,
    SIGNAL_STRONG_NEGATIVE_SENTIMENT,
)
from app.runtime.classifiers import (
    INTENT_OTHER,
    INTENT_REQUEST_HUMAN,
    IntentResult,
    SentimentResult,
)
from app.runtime.contracts import RuntimeRequest
from app.runtime.escalation_judge import (
    FREE_LEAD_BUDGET_THRESHOLD,
    EscalationJudge,
    OutcomeContext,
)


# ---------------------------------------------------------------------
# Deterministic fake classifiers.
# ---------------------------------------------------------------------


class _FixedIntent:
    def __init__(self, intent_class, confidence):
        self._r = IntentResult(intent_class=intent_class, confidence=confidence)

    def classify_intent(self, message):
        return self._r


class _FixedSentiment:
    """Returns a per-message score from a dict, defaulting to 0.0."""

    def __init__(self, scores=None, default=0.0):
        self._scores = scores or {}
        self._default = default

    def score_sentiment(self, message):
        return SentimentResult(score=self._scores.get(message, self._default))


def _req(message="hello", recent=None):
    return RuntimeRequest(
        message=message,
        session_id="sess-1",
        user_id="user-1",
        admin_id="admin-1",
        channel="widget",
        luciel_instance_id=7,
        recent_customer_messages=recent or [],
    )


def _judge(*, intent=None, sentiment=None):
    return EscalationJudge(
        intent_classifier=intent or _FixedIntent(INTENT_OTHER, 0.0),
        sentiment_classifier=sentiment or _FixedSentiment(),
    )


# =====================================================================
# (a) EXPLICIT HUMAN REQUEST — intent==request_human AND conf >= 0.85
# =====================================================================


class TestExplicitHumanRequest(unittest.TestCase):

    def test_fires_at_exactly_0_85(self):
        judge = _judge(intent=_FixedIntent(INTENT_REQUEST_HUMAN, 0.85))
        decision = judge.evaluate_intake(_req("get me a person"))
        self.assertIsNotNone(decision)
        self.assertEqual(decision.signal, SIGNAL_EXPLICIT_HUMAN_REQUEST)
        self.assertEqual(decision.gate, GATE_INTAKE)
        self.assertEqual(decision.signal_confidence, 0.85)

    def test_does_not_fire_just_below_at_0_84(self):
        judge = _judge(intent=_FixedIntent(INTENT_REQUEST_HUMAN, 0.84))
        self.assertIsNone(judge.evaluate_intake(_req("maybe a person?")))

    def test_does_not_fire_for_other_intent_even_high_conf(self):
        judge = _judge(intent=_FixedIntent(INTENT_OTHER, 0.99))
        self.assertIsNone(judge.evaluate_intake(_req("what are your hours")))

    def test_decision_carries_scope_and_reasoning(self):
        judge = _judge(intent=_FixedIntent(INTENT_REQUEST_HUMAN, 0.9))
        d = judge.evaluate_intake(_req("human please"))
        self.assertEqual(d.admin_id, "admin-1")
        self.assertEqual(d.session_id, "sess-1")
        self.assertEqual(d.luciel_instance_id, 7)
        self.assertEqual(d.user_id, "user-1")
        self.assertIn("request_human", d.reasoning_excerpt)
        self.assertEqual(d.signal_inputs["confidence"], 0.9)


# =====================================================================
# (b) STRONG NEGATIVE SENTIMENT — latest <= -0.7 AND >= 2 of trailing 3
# =====================================================================


class TestStrongNegativeSentiment(unittest.TestCase):

    def test_fires_at_exactly_minus_0_7_with_two_negative(self):
        # window = [prior, current]; both at exactly -0.7.
        sent = _FixedSentiment({"m1": -0.7, "now": -0.7})
        judge = _judge(sentiment=sent)
        d = judge.evaluate_intake(_req("now", recent=["m1"]))
        self.assertIsNotNone(d)
        self.assertEqual(d.signal, SIGNAL_STRONG_NEGATIVE_SENTIMENT)
        self.assertEqual(d.gate, GATE_INTAKE)

    def test_latest_just_above_threshold_does_not_fire(self):
        # latest -0.69 is NOT strongly negative even with a negative prior.
        sent = _FixedSentiment({"m1": -0.9, "now": -0.69})
        judge = _judge(sentiment=sent)
        self.assertIsNone(judge.evaluate_intake(_req("now", recent=["m1"])))

    def test_only_one_negative_in_window_does_not_fire(self):
        # latest is negative but polarity NOT consistent (1 of window).
        sent = _FixedSentiment({"m1": 0.2, "now": -0.8})
        judge = _judge(sentiment=sent)
        self.assertIsNone(judge.evaluate_intake(_req("now", recent=["m1"])))

    def test_two_of_three_negative_fires(self):
        sent = _FixedSentiment({"a": -0.8, "b": 0.1, "now": -0.75})
        judge = _judge(sentiment=sent)
        d = judge.evaluate_intake(_req("now", recent=["a", "b"]))
        self.assertIsNotNone(d)
        self.assertEqual(d.signal_inputs["negative_count"], 2)

    def test_window_capped_at_three(self):
        # Four customer turns; only the trailing 3 count. The oldest
        # (very negative) falls out of the window.
        sent = _FixedSentiment(
            {"oldest": -1.0, "a": 0.5, "b": 0.5, "now": -0.8}
        )
        judge = _judge(sentiment=sent)
        # window = [a, b, now] → only 1 negative → no fire.
        self.assertIsNone(
            judge.evaluate_intake(_req("now", recent=["oldest", "a", "b"]))
        )


# =====================================================================
# Intake precedence: explicit-human beats sentiment.
# =====================================================================


class TestIntakePrecedence(unittest.TestCase):

    def test_explicit_human_takes_precedence_over_sentiment(self):
        judge = _judge(
            intent=_FixedIntent(INTENT_REQUEST_HUMAN, 0.95),
            sentiment=_FixedSentiment({"now": -0.95}, default=-0.95),
        )
        d = judge.evaluate_intake(_req("now", recent=["angry"]))
        self.assertEqual(d.signal, SIGNAL_EXPLICIT_HUMAN_REQUEST)


# =====================================================================
# (c) CANNOT CONFIDENTLY ANSWER — conf < 0.6 AND grounding < tier floor
# =====================================================================


class TestCannotConfidentlyAnswer(unittest.TestCase):

    def test_fires_low_conf_and_below_floor(self):
        judge = _judge()
        out = OutcomeContext(confidence=0.59, tier="free", grounding_score=0.4)
        d = judge.evaluate_outcome(_req(), out)
        self.assertIsNotNone(d)
        self.assertEqual(d.signal, SIGNAL_CANNOT_CONFIDENTLY_ANSWER)
        self.assertEqual(d.gate, GATE_OUTCOME)

    def test_confidence_exactly_0_6_does_not_fire(self):
        judge = _judge()
        out = OutcomeContext(confidence=0.6, tier="free", grounding_score=0.0)
        self.assertIsNone(judge.evaluate_outcome(_req(), out))

    def test_grounded_low_conf_does_not_fire(self):
        # Low confidence but grounding AT the floor (0.5 for free) → the
        # answer is grounded enough; not a cannot-answer escalation.
        judge = _judge()
        out = OutcomeContext(confidence=0.3, tier="free", grounding_score=0.5)
        self.assertIsNone(judge.evaluate_outcome(_req(), out))

    def test_retrieval_failure_is_below_every_floor(self):
        # Retrieval failed → grounding treated as 0.0 even on Enterprise
        # (lowest floor 0.3). Low confidence + retrieval failure fires.
        judge = _judge()
        out = OutcomeContext(
            confidence=0.5, tier="enterprise", retrieval_failed=True
        )
        d = judge.evaluate_outcome(_req(), out)
        self.assertIsNotNone(d)
        self.assertTrue(d.signal_inputs["retrieval_failed"])
        self.assertIn("retrieval failed", d.reasoning_excerpt)

    def test_none_grounding_treated_as_zero(self):
        judge = _judge()
        out = OutcomeContext(confidence=0.4, tier="pro", grounding_score=None)
        d = judge.evaluate_outcome(_req(), out)
        self.assertIsNotNone(d)
        self.assertEqual(d.signal_inputs["grounding"], 0.0)

    def test_per_tier_floor_enterprise_more_lenient(self):
        # grounding 0.35: below free floor (0.5) but above enterprise
        # floor (0.3). Same grounding fires on free, not on enterprise.
        judge = _judge()
        free_out = OutcomeContext(confidence=0.4, tier="free", grounding_score=0.35)
        ent_out = OutcomeContext(
            confidence=0.4, tier="enterprise", grounding_score=0.35
        )
        self.assertIsNotNone(judge.evaluate_outcome(_req(), free_out))
        self.assertIsNone(judge.evaluate_outcome(_req(), ent_out))


# =====================================================================
# (d) HIGH-VALUE LEAD — Free real-estate budget heuristic.
# =====================================================================


class TestHighValueLead(unittest.TestCase):

    def test_fires_at_threshold(self):
        judge = _judge()
        out = OutcomeContext(
            confidence=0.9, tier="free", lead_value=FREE_LEAD_BUDGET_THRESHOLD
        )
        d = judge.evaluate_outcome(_req(), out)
        self.assertIsNotNone(d)
        self.assertEqual(d.signal, SIGNAL_HIGH_VALUE_LEAD)
        self.assertIsNone(d.signal_confidence)

    def test_below_threshold_does_not_fire(self):
        judge = _judge()
        out = OutcomeContext(
            confidence=0.9, tier="free", lead_value=FREE_LEAD_BUDGET_THRESHOLD - 1
        )
        self.assertIsNone(judge.evaluate_outcome(_req(), out))

    def test_no_lead_value_does_not_fire(self):
        judge = _judge()
        out = OutcomeContext(confidence=0.9, tier="free", lead_value=None)
        self.assertIsNone(judge.evaluate_outcome(_req(), out))


# =====================================================================
# Outcome precedence + the bound invariant.
# =====================================================================


class TestOutcomePrecedenceAndBoundInvariant(unittest.TestCase):

    def test_cannot_answer_takes_precedence_over_high_value_lead(self):
        judge = _judge()
        out = OutcomeContext(
            confidence=0.2,
            tier="free",
            grounding_score=0.0,
            lead_value=FREE_LEAD_BUDGET_THRESHOLD + 1,
        )
        d = judge.evaluate_outcome(_req(), out)
        self.assertEqual(d.signal, SIGNAL_CANNOT_CONFIDENTLY_ANSWER)

    def test_outcome_context_has_no_bound_hit_field(self):
        # The doctrinal guard (§3.4.1 locked #17) made structural: the
        # OUTCOME-gate input type carries NO iteration-bound state, so
        # the judge structurally cannot read it.
        out = OutcomeContext(confidence=0.9)
        self.assertFalse(hasattr(out, "bound_hit"))
        self.assertFalse(hasattr(out, "iterations"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
