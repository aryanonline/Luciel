"""RESCAN TIER-C — Grounding fidelity tests (§3.4.13 + §9 items 21-23).

Covers:
  G1  Composite grounding = 0.5 * retrieval_relevance + 0.5 * citation_overlap.
  G2  Empty chunks → None (unchanged).
  G3  Chunks with no distance → retrieval_relevance=0.0, citation overlap drives score.
  G4  Citation overlap: answer with no vocabulary match → 0.0 overlap.
  G5  Citation overlap: answer fully paraphrasing chunk content → non-zero overlap.
  G6  Per-tier floor escalation boundary: 0.47 fires on Pro+Enterprise but NOT Free.
  G7  Per-tier floor escalation boundary: 0.52 fires on Enterprise only.
  G8  Score 0.35 fires on both tiers (Free + Pro).
  G9  cannot_answer reply contains the exact canonical §3.4.13 phrase.
  G10 handoff_ack.CANNOT_ANSWER_REPLY is the canonical phrase.
  G11 cannot_answer_reply() function returns the canonical phrase.
  G12 Orchestrator uses canonical phrase when SIGNAL_CANNOT_CONFIDENTLY_ANSWER fires.
"""
from __future__ import annotations

import types
import unittest


# ---------------------------------------------------------------------------
# Helpers: lightweight fake chunk
# ---------------------------------------------------------------------------

def _chunk(content: str, distance: float | None = 0.1) -> object:
    """Return a minimal duck-typed chunk for testing."""
    c = types.SimpleNamespace()
    c.content = content
    c.distance = distance
    c.formatted = f"[luciel_knowledge] {content}"
    return c


# ---------------------------------------------------------------------------
# G1–G5: Composite grounding score (_grounding_from_chunks)
# ---------------------------------------------------------------------------

class TestCompositeGroundingScore(unittest.TestCase):
    """§3.4.13: grounding = 0.5 * retrieval_relevance + 0.5 * citation_overlap."""

    def _score(self, chunks, answer=""):
        from app.runtime.orchestrator import LucielOrchestrator
        return LucielOrchestrator._grounding_from_chunks(chunks, answer=answer)

    def test_g1_empty_chunks_returns_none(self):
        """G2: no chunks → None (retrieval did not run)."""
        self.assertIsNone(self._score([]))

    def test_g2_composite_formula_weights(self):
        """G1: score = 0.5 * retrieval_relevance + 0.5 * citation_overlap.

        With distance=0.2 → retrieval_relevance=0.8.
        Answer that exactly matches chunk vocabulary → citation_overlap=1.0.
        Expected = 0.5*0.8 + 0.5*1.0 = 0.9.
        """
        content = "the quick brown fox jumps over the lazy dog"
        chunk = _chunk(content, distance=0.2)
        # Answer that shares almost all vocabulary with the chunk.
        answer = "the quick brown fox jumps over the lazy dog"
        score = self._score([chunk], answer=answer)
        self.assertIsNotNone(score)
        # citation_overlap should be ~1.0 (exact match), retrieval_relevance=0.8
        self.assertAlmostEqual(score, 0.9, places=2)

    def test_g3_no_overlap_in_answer_citation_is_zero(self):
        """G4: answer with totally different vocabulary → citation_overlap≈0,
        score ≈ 0.5 * retrieval_relevance."""
        chunk = _chunk("quantum mechanics wave function eigenvalue", distance=0.2)
        answer = "The weather outside is delightful today."
        score = self._score([chunk], answer=answer)
        self.assertIsNotNone(score)
        # citation_overlap should be 0.0 (no vocabulary overlap above 0.10)
        # retrieval_relevance = 0.8, so score ≈ 0.5*0.8 + 0.5*0.0 = 0.4
        self.assertAlmostEqual(score, 0.4, places=1)

    def test_g4_no_distance_on_chunks_retrieval_relevance_zero(self):
        """G3: chunks exist but none carry distance → retrieval_relevance=0.0.
        Citation overlap drives the score."""
        content = "customer service hours monday friday nine five"
        chunk = _chunk(content, distance=None)
        # Answer closely paraphrasing the chunk.
        answer = "customer service hours monday friday nine five"
        score = self._score([chunk], answer=answer)
        self.assertIsNotNone(score)
        # retrieval_relevance=0.0, citation_overlap≈1.0 → score ≈ 0.5
        self.assertGreater(score, 0.3)

    def test_g5_score_clamped_to_0_1(self):
        """Score is always in [0, 1] regardless of inputs."""
        chunk = _chunk("hello world", distance=0.0)  # relevance = 1.0
        answer = "hello world"
        score = self._score([chunk], answer=answer)
        self.assertIsNotNone(score)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_g6_partial_citation_overlap(self):
        """Two-sentence answer where one sentence overlaps and one doesn't.
        citation_overlap should be ≈0.5."""
        content = "our refund policy allows thirty days"
        chunk = _chunk(content, distance=0.0)  # relevance=1.0
        # First sentence overlaps; second does not.
        answer = "Our refund policy allows thirty days. The capital of France is Paris."
        score = self._score([chunk], answer=answer)
        self.assertIsNotNone(score)
        # retrieval_relevance=1.0, citation_overlap≈0.5 → score ≈ 0.75
        self.assertGreater(score, 0.5)
        self.assertLess(score, 1.0)

    def test_g7_empty_answer_citation_overlap_is_zero(self):
        """An empty answer contributes citation_overlap=0.0."""
        chunk = _chunk("some content here", distance=0.2)  # relevance=0.8
        score = self._score([chunk], answer="")
        self.assertIsNotNone(score)
        # score = 0.5*0.8 + 0.5*0.0 = 0.4
        self.assertAlmostEqual(score, 0.4, places=2)

    def test_g8_malformed_chunk_does_not_raise(self):
        """A completely broken chunk object degrades gracefully to None."""
        class _Broken:
            @property
            def distance(self):
                raise RuntimeError("broken")
            @property
            def content(self):
                raise RuntimeError("broken")
        from app.runtime.orchestrator import LucielOrchestrator
        # Should not raise; returns None
        result = LucielOrchestrator._grounding_from_chunks([_Broken()], answer="ok")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# G6–G8: Per-tier escalation floors (§9 items 21-23)
# ---------------------------------------------------------------------------

class TestPerTierGroundingFloors(unittest.TestCase):
    """§9 items 21-22: Free 0.45 / Pro 0.50. (Enterprise deferred.)"""

    def setUp(self):
        from app.runtime.classifiers import INTENT_OTHER, IntentResult, SentimentResult

        class _NoOpIntent:
            def classify_intent(self, _m):
                return IntentResult(intent_class=INTENT_OTHER, confidence=0.0)

        class _NoOpSentiment:
            def score_sentiment(self, _m):
                return SentimentResult(score=0.0)

        from app.runtime.escalation_judge import EscalationJudge
        from app.runtime.contracts import RuntimeRequest
        self._judge = EscalationJudge(
            intent_classifier=_NoOpIntent(),
            sentiment_classifier=_NoOpSentiment(),
        )
        self._req = RuntimeRequest(
            message="hello",
            session_id="sess-tier",
            user_id="u1",
            admin_id="a1",
            channel="widget",
        )

    def _make_ctx(self, tier, grounding):
        from app.runtime.escalation_judge import OutcomeContext
        return OutcomeContext(confidence=0.4, tier=tier, grounding_score=grounding)

    # --- 0.47: below Pro+Enterprise, above Free ---
    def test_0_47_does_not_escalate_on_free(self):
        """0.47 >= Free floor 0.45 → must NOT fire on free."""
        self.assertIsNone(
            self._judge.evaluate_outcome(self._req, self._make_ctx("free", 0.47))
        )

    def test_0_47_escalates_on_pro(self):
        """0.47 < Pro floor 0.50 → must fire on pro."""
        from app.models.escalation_event import SIGNAL_CANNOT_CONFIDENTLY_ANSWER
        d = self._judge.evaluate_outcome(self._req, self._make_ctx("pro", 0.47))
        self.assertIsNotNone(d)
        self.assertEqual(d.signal, SIGNAL_CANNOT_CONFIDENTLY_ANSWER)

    # test_0_47_escalates_on_enterprise removed: Enterprise tier excised in Unit 1.

    # --- 0.52: at/above Pro floor (Enterprise tier deferred) ---
    def test_0_52_does_not_escalate_on_pro(self):
        """0.52 >= Pro floor 0.50 → must NOT fire on pro."""
        self.assertIsNone(
            self._judge.evaluate_outcome(self._req, self._make_ctx("pro", 0.52))
        )

    # test_0_52_escalates_on_enterprise removed: Enterprise tier excised in Unit 1.

    # --- 0.35: below every floor ---
    def test_0_35_escalates_on_all_tiers(self):
        """0.35 < all floors (0.45/0.50) → fires on both tiers."""
        from app.models.escalation_event import SIGNAL_CANNOT_CONFIDENTLY_ANSWER
        for tier in ("free", "pro"):
            d = self._judge.evaluate_outcome(self._req, self._make_ctx(tier, 0.35))
            self.assertIsNotNone(d, f"Expected escalation on {tier}")
            self.assertEqual(d.signal, SIGNAL_CANNOT_CONFIDENTLY_ANSWER)

    # --- exact floor boundaries ---
    def test_free_floor_at_exactly_0_45(self):
        """At exactly 0.45 (free floor) → no escalation (>= floor means grounded)."""
        self.assertIsNone(
            self._judge.evaluate_outcome(self._req, self._make_ctx("free", 0.45))
        )

    def test_pro_floor_at_exactly_0_50(self):
        """At exactly 0.50 (pro floor) → no escalation."""
        self.assertIsNone(
            self._judge.evaluate_outcome(self._req, self._make_ctx("pro", 0.50))
        )

    # test_enterprise_floor_at_exactly_0_55 removed: Enterprise tier excised in Unit 1.

    def test_cognition_parity_algorithm_identical_across_tiers(self):
        """Cognition-parity: the judge calls _cannot_confidently_answer the same
        way for all tiers; only the floor value in GROUNDING_FLOOR_BY_TIER differs.
        Verified by checking both tiers share the SAME judge method."""
        # The judge is a single instance handling all tiers — no tier-branching
        # in the algorithm itself.
        from app.runtime.escalation_judge import EscalationJudge
        import inspect
        source = inspect.getsource(EscalationJudge._cannot_confidently_answer)
        # The algorithm must not contain 'if.*tier' branching in its body
        # (it reads GROUNDING_FLOOR_BY_TIER dict which IS allowed).
        # Confirm no explicit tier-conditional branching.
        self.assertNotIn("elif", source,
            "Algorithm must not branch on tier; only floor lookup is allowed")


# ---------------------------------------------------------------------------
# G9–G11: cannot_answer canonical phrase
# ---------------------------------------------------------------------------

CANONICAL_PHRASE = "I don't have that information, let me get someone who does."


class TestCannotAnswerPhrase(unittest.TestCase):
    """§3.4.13 canonical cannot_answer phrase tests."""

    def test_g9_cannot_answer_reply_constant_is_canonical(self):
        """G10: CANNOT_ANSWER_REPLY matches the §3.4.13 canonical phrase."""
        from app.runtime.handoff import CANNOT_ANSWER_REPLY
        self.assertEqual(CANNOT_ANSWER_REPLY, CANONICAL_PHRASE)

    def test_g10_cannot_answer_reply_function_returns_canonical(self):
        """G11: cannot_answer_reply() returns the canonical phrase."""
        from app.runtime.handoff import cannot_answer_reply
        self.assertEqual(cannot_answer_reply(), CANONICAL_PHRASE)

    def test_g11_canonical_phrase_contains_exact_wording(self):
        """The phrase must contain the three key clauses from §3.4.13."""
        from app.runtime.handoff import CANNOT_ANSWER_REPLY
        self.assertIn("don't have that information", CANNOT_ANSWER_REPLY)
        self.assertIn("let me get someone", CANNOT_ANSWER_REPLY)
        self.assertIn("who does", CANNOT_ANSWER_REPLY)

    def test_g12_handoff_ack_distinct_from_cannot_answer(self):
        """Gate-1 handoff ack and cannot_answer reply must be distinct phrases
        (they handle different situations: explicit request vs. ungrounded answer)."""
        from app.runtime.handoff import CANNOT_ANSWER_REPLY, handoff_acknowledgement
        self.assertNotEqual(handoff_acknowledgement(), CANNOT_ANSWER_REPLY)


# ---------------------------------------------------------------------------
# G12: Orchestrator uses canonical phrase for cannot_answer escalation
# ---------------------------------------------------------------------------

class TestOrchestratorCannotAnswerReply(unittest.TestCase):
    """When the outcome gate fires SIGNAL_CANNOT_CONFIDENTLY_ANSWER,
    the orchestrator's RuntimeResponse.message must be the canonical phrase."""

    def _build_orchestrator(self):
        """Build orchestrator with a fake LLM that returns low confidence,
        injected judge that fires cannot_answer, and no DB calls."""
        from app.integrations.llm.base import LLMResponse
        from app.runtime.orchestrator import LucielOrchestrator
        from app.runtime.contracts import RuntimeRequest

        class _LowConfidenceRouter:
            def generate(self, request, *, preferred_provider=None, **kwargs) -> LLMResponse:
                # Confidence 0.3 < LOW_CONFIDENCE_THRESHOLD (0.6)
                return LLMResponse(
                    content='{"reply": "I think maybe...", "tool_calls": [], "confidence": 0.3}',
                    model="fake-model",
                    provider="fake",
                )

        return LucielOrchestrator(model_router=_LowConfidenceRouter())

    def test_g12_orchestrator_message_is_canonical_on_cannot_answer(self):
        """When SIGNAL_CANNOT_CONFIDENTLY_ANSWER fires (low confidence + low
        grounding), the orchestrator's message must equal CANNOT_ANSWER_REPLY."""
        from unittest.mock import patch, MagicMock
        from app.runtime.contracts import RuntimeRequest
        from app.runtime.handoff import CANNOT_ANSWER_REPLY
        from app.models.escalation_event import SIGNAL_CANNOT_CONFIDENTLY_ANSWER
        from app.policy.escalation import EscalationDecision
        from app.models.escalation_event import GATE_OUTCOME

        orch = self._build_orchestrator()

        # Build a fake decision that represents cannot_answer firing.
        fake_decision = EscalationDecision(
            signal=SIGNAL_CANNOT_CONFIDENTLY_ANSWER,
            gate=GATE_OUTCOME,
            admin_id="admin-1",
            session_id="sess-1",
            luciel_instance_id=None,
            user_id="user-1",
            signal_confidence=0.3,
            reasoning_excerpt="grounding 0.2 < floor 0.45",
            signal_inputs={},
        )

        req = RuntimeRequest(
            message="tell me something",
            session_id="sess-1",
            user_id="user-1",
            admin_id="admin-1",
            channel="api",
        )

        with patch(
            "app.core.config.settings.knowledge_retrieval_enabled", False,
        ), patch.object(
            orch, "_outcome_gate", return_value=fake_decision,
        ), patch.object(
            orch, "_record_escalation_best_effort", return_value=None,
        ), patch.object(
            orch, "_record_trace_best_effort", return_value="trace-x",
        ), patch.object(
            orch, "_finalize_cognition", return_value=None,
        ), patch.object(
            orch, "_arbitrate_channel",
            return_value=MagicMock(channel="api", prompt_channel_switch=None),
        ):
            resp = orch.run(req)

        self.assertEqual(
            resp.message, CANNOT_ANSWER_REPLY,
            f"Expected canonical phrase, got: {resp.message!r}",
        )
        self.assertTrue(resp.escalation_flag)

    def test_g13_orchestrator_message_is_llm_reply_when_no_cannot_answer(self):
        """When no cannot_answer escalation fires, the message is the LLM reply."""
        from unittest.mock import patch, MagicMock
        from app.runtime.contracts import RuntimeRequest
        from app.runtime.handoff import CANNOT_ANSWER_REPLY

        orch = self._build_orchestrator()
        req = RuntimeRequest(
            message="what are your hours",
            session_id="sess-2",
            user_id="user-2",
            admin_id="admin-2",
            channel="api",
        )

        with patch(
            "app.core.config.settings.knowledge_retrieval_enabled", False,
        ), patch.object(
            orch, "_outcome_gate", return_value=None,
        ), patch.object(
            orch, "_record_trace_best_effort", return_value="trace-y",
        ), patch.object(
            orch, "_finalize_cognition", return_value=None,
        ), patch.object(
            orch, "_arbitrate_channel",
            return_value=MagicMock(channel="api", prompt_channel_switch=None),
        ):
            resp = orch.run(req)

        self.assertNotEqual(
            resp.message, CANNOT_ANSWER_REPLY,
            "LLM reply should be used when no cannot_answer fires",
        )
        self.assertFalse(resp.escalation_flag)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
