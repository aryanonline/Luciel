"""Arc 14 U2 — §3.4.5 Escalation Judgment Module.
RESCAN TIER-C: domain-agnostic weighted lead scoring.

Two gates, four FIXED signals (NOT admin-configurable — the doctrinal
thresholds are pinned here in code):

  Gate 1 — INTAKE (pre-PLAN; knowable from the inbound message alone)
    a. EXPLICIT HUMAN REQUEST
         NLU intent ``request_human`` AND confidence ``>= 0.85``.
    b. STRONG NEGATIVE SENTIMENT
         latest customer-message sentiment ``<= -0.7`` AND consistent
         (negative) polarity across ``>= 2`` of the trailing 3 customer
         messages.

  Gate 2 — OUTCOME (post-REFLECT; needs the loop output)
    c. CANNOT CONFIDENTLY ANSWER
         loop confidence ``< 0.6`` AND grounding below the per-tier
         floor. Retrieval FAILURE in the CONTEXT step is a contributing
         signal (spec item 5): a turn that retrieved nothing is treated
         as below the grounding floor.
    d. HIGH-VALUE LEAD
         §3.4.5 weighted composite lead score >= LEAD_SCORE_THRESHOLD.
         Score = budget×0.5 + time_constraint×0.3 + purchase_intent×0.4,
         capped at 1.0, normalized [0,1].
         Pro/Enterprise admins may define custom value rules via the
         business-context field that raise the score further.
         Free uses the built-in general-purpose heuristic only.
         The score is emitted as ``signal_confidence``.

The judge is PURE DECISION: it reads classifier outputs + loop state and
returns an ``EscalationDecision`` (or ``None``). It does NOT touch the
DB or send notifications — that is ``EscalationService.record_escalation``
(event store + tier routing + audit). Splitting decision from
side-effect keeps the judge unit-testable with no DB.

Doctrine guard: hitting the loop iteration bound is cost-control, NEVER
an escalation trigger (§3.4.1 locked #17). The judge NEVER reads
``loop.bound_hit``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.models.escalation_event import (
    GATE_INTAKE,
    GATE_OUTCOME,
    SIGNAL_CANNOT_CONFIDENTLY_ANSWER,
    SIGNAL_EXPLICIT_HUMAN_REQUEST,
    SIGNAL_HIGH_VALUE_LEAD,
    SIGNAL_STRONG_NEGATIVE_SENTIMENT,
)
from app.policy.escalation import EscalationDecision
from app.runtime.classifiers import (
    INTENT_REQUEST_HUMAN,
    IntentClassifier,
    SentimentClassifier,
)

logger = logging.getLogger(__name__)


# --- Doctrinal thresholds (§3.4.5). Pinned constants, not settings. ---

# (a) explicit human request
INTENT_CONFIDENCE_THRESHOLD: float = 0.85
# (b) strong negative sentiment
SENTIMENT_NEGATIVE_THRESHOLD: float = -0.7
SENTIMENT_POLARITY_WINDOW: int = 3       # trailing customer messages
SENTIMENT_POLARITY_MIN_CONSISTENT: int = 2  # of the window
# (c) cannot confidently answer
LOW_CONFIDENCE_THRESHOLD: float = 0.6
# Per-tier grounding floor: the minimum grounding score below which a
# low-confidence answer is treated as ungrounded. §9 items 21-23 specify
# Free 0.45 / Pro 0.50 / Enterprise 0.55 — tighter floors on higher tiers
# reflect the stronger anti-hallucination promise of paid plans (Vision §1).
# Cognition-parity doctrine: the MECHANISM is identical across tiers; only
# the floor VALUE differs. The per-tier dict structure means tuning is a
# value change, not a refactor. A turn that retrieved nothing scores
# grounding 0.0 and is below every floor.
GROUNDING_FLOOR_BY_TIER: dict[str, float] = {
    "free": 0.45,       # §9 item 21
    "pro": 0.50,        # §9 item 22
    # Enterprise tier deferred (Open Decision #8); removed in Unit 1.
}
_DEFAULT_GROUNDING_FLOOR = 0.45  # fail-open to the most permissive floor
# (d) high-value lead — §3.4.5 weighted composite scoring (RESCAN TIER-C).
# The normalized lead score fires when it reaches this threshold
# (budget alone = 0.5; purchase-intent alone = 0.4; time-constraint = 0.3).
LEAD_SCORE_THRESHOLD: float = 0.3

# LEGACY COMPAT: FREE_LEAD_BUDGET_THRESHOLD is kept so existing tests that
# import it do not break. It is no longer used for the scoring decision
# (replaced by LEAD_SCORE_THRESHOLD on the normalized score). Its presence
# documents the old real-estate-biased drift; scoring moved from
# 'lead_value >= 750_000' to 'lead_score >= LEAD_SCORE_THRESHOLD'.
FREE_LEAD_BUDGET_THRESHOLD: float = 750_000.0


@dataclass
class OutcomeContext:
    """What the OUTCOME gate needs from the finished loop + context step.

    ``confidence`` is the loop's final confidence. ``grounding_score`` is
    a [0,1] measure of how well the answer was grounded in retrieved
    knowledge (``None`` when retrieval did not run). ``retrieval_failed``
    is True when the CONTEXT step's Retrieve leg errored or returned
    nothing — a contributing signal per spec item 5. ``tier`` shapes the
    grounding floor.

    Lead scoring inputs (§3.4.5 RESCAN TIER-C):
    ``lead_score`` is the weighted composite lead score [0, 1] produced
    by ``lead_capture.score_lead`` (budget×0.5 + time×0.3 + intent×0.4,
    capped 1.0). This replaces the old binary ``lead_value >= 750k`` gate.
    ``lead_value`` is retained for backward compatibility (still used by
    the lead row) but the HIGH-VALUE LEAD gate now reads ``lead_score``.
    ``business_context_rules`` is the list of Pro/Enterprise custom-value
    rules (each {"pattern": str, "weight_boost": float}) from the admin's
    business-context field; Free always passes None/[].
    """

    confidence: float
    tier: str = "free"
    grounding_score: float | None = None
    retrieval_failed: bool = False
    lead_value: float | None = None
    lead_score: float = 0.0
    business_context_rules: list[dict] | None = None


class EscalationJudge:
    """Evaluates the §3.4.5 signals. Pure decision, no side-effects.

    Classifiers are injected so tests drive deterministic fakes (founder
    decision #2). When none are supplied they are built lazily over the
    provider-agnostic LLM channel — a classifier that cannot reach a
    provider degrades to a neutral, non-firing result (see
    ``app.runtime.classifiers``), so a missing provider NEVER invents an
    escalation.
    """

    def __init__(
        self,
        *,
        intent_classifier: IntentClassifier | None = None,
        sentiment_classifier: SentimentClassifier | None = None,
        model_router=None,
    ) -> None:
        self._intent = intent_classifier
        self._sentiment = sentiment_classifier
        # When classifiers are not injected they are built lazily over
        # this router. Threading the orchestrator's router here means a
        # test that injects a stub/boom router never makes a live API
        # call from the judge's classifiers (and a boom router degrades
        # the classifier to neutral, so the gate does not fire).
        self._model_router = model_router

    # ------------------------------------------------------------------
    # Gate 1 — INTAKE
    # ------------------------------------------------------------------

    def evaluate_intake(self, req) -> EscalationDecision | None:
        """Evaluate the two intake signals. Returns the firing decision
        (explicit-human-request takes precedence over sentiment), or
        ``None`` to proceed into PLAN."""
        decision = self._explicit_human_request(req)
        if decision is not None:
            return decision
        return self._strong_negative_sentiment(req)

    def _explicit_human_request(self, req) -> EscalationDecision | None:
        result = self._intent_classifier().classify_intent(req.message)
        fired = (
            result.intent_class == INTENT_REQUEST_HUMAN
            and result.confidence >= INTENT_CONFIDENCE_THRESHOLD
        )
        if not fired:
            return None
        return EscalationDecision(
            signal=SIGNAL_EXPLICIT_HUMAN_REQUEST,
            gate=GATE_INTAKE,
            admin_id=req.admin_id,
            session_id=req.session_id,
            luciel_instance_id=req.luciel_instance_id,
            user_id=req.user_id,
            signal_confidence=result.confidence,
            reasoning_excerpt=(
                f"intent={result.intent_class} confidence={result.confidence:.2f} "
                f">= {INTENT_CONFIDENCE_THRESHOLD}"
            ),
            signal_inputs={
                "message": req.message,
                "intent_class": result.intent_class,
                "confidence": result.confidence,
                "threshold": INTENT_CONFIDENCE_THRESHOLD,
            },
        )

    def _strong_negative_sentiment(self, req) -> EscalationDecision | None:
        clf = self._sentiment_classifier()

        # Build the trailing window oldest→newest: prior customer turns
        # (if the caller supplied them) followed by the current message.
        prior = list(getattr(req, "recent_customer_messages", []) or [])
        window_msgs = (prior + [req.message])[-SENTIMENT_POLARITY_WINDOW:]
        scores = [clf.score_sentiment(m).score for m in window_msgs]

        latest = scores[-1]
        # The latest message must itself be strongly negative.
        if latest > SENTIMENT_NEGATIVE_THRESHOLD:
            return None
        # AND consistent negative polarity across >= 2 of the window.
        negative_count = sum(
            1 for s in scores if s <= SENTIMENT_NEGATIVE_THRESHOLD
        )
        if negative_count < SENTIMENT_POLARITY_MIN_CONSISTENT:
            return None

        return EscalationDecision(
            signal=SIGNAL_STRONG_NEGATIVE_SENTIMENT,
            gate=GATE_INTAKE,
            admin_id=req.admin_id,
            session_id=req.session_id,
            luciel_instance_id=req.luciel_instance_id,
            user_id=req.user_id,
            signal_confidence=latest,
            reasoning_excerpt=(
                f"latest sentiment {latest:.2f} <= {SENTIMENT_NEGATIVE_THRESHOLD}; "
                f"{negative_count}/{len(scores)} of trailing window negative "
                f"(>= {SENTIMENT_POLARITY_MIN_CONSISTENT} required)"
            ),
            signal_inputs={
                "window_scores": scores,
                "latest": latest,
                "threshold": SENTIMENT_NEGATIVE_THRESHOLD,
                "negative_count": negative_count,
                "min_consistent": SENTIMENT_POLARITY_MIN_CONSISTENT,
            },
        )

    # ------------------------------------------------------------------
    # Gate 2 — OUTCOME
    # ------------------------------------------------------------------

    def evaluate_outcome(
        self, req, outcome: OutcomeContext
    ) -> EscalationDecision | None:
        """Evaluate the two outcome signals. Returns the firing decision
        (cannot-confidently-answer takes precedence over high-value-lead),
        or ``None``.

        NEVER reads any iteration-bound state — hitting the loop bound is
        cost-control, not an escalation trigger (§3.4.1 locked #17)."""
        decision = self._cannot_confidently_answer(req, outcome)
        if decision is not None:
            return decision
        return self._high_value_lead(req, outcome)

    def _cannot_confidently_answer(
        self, req, outcome: OutcomeContext
    ) -> EscalationDecision | None:
        if outcome.confidence >= LOW_CONFIDENCE_THRESHOLD:
            return None

        floor = GROUNDING_FLOOR_BY_TIER.get(outcome.tier, _DEFAULT_GROUNDING_FLOOR)
        # Retrieval failure (errored OR returned nothing) is a
        # contributing signal: treat grounding as 0.0 (below every floor).
        grounding = (
            0.0
            if outcome.retrieval_failed or outcome.grounding_score is None
            else outcome.grounding_score
        )
        if grounding >= floor:
            return None

        return EscalationDecision(
            signal=SIGNAL_CANNOT_CONFIDENTLY_ANSWER,
            gate=GATE_OUTCOME,
            admin_id=req.admin_id,
            session_id=req.session_id,
            luciel_instance_id=req.luciel_instance_id,
            user_id=req.user_id,
            signal_confidence=outcome.confidence,
            reasoning_excerpt=(
                f"confidence {outcome.confidence:.2f} < {LOW_CONFIDENCE_THRESHOLD} "
                f"AND grounding {grounding:.2f} < tier floor {floor:.2f}"
                + (" (retrieval failed)" if outcome.retrieval_failed else "")
            ),
            signal_inputs={
                "confidence": outcome.confidence,
                "confidence_threshold": LOW_CONFIDENCE_THRESHOLD,
                "grounding": grounding,
                "grounding_floor": floor,
                "tier": outcome.tier,
                "retrieval_failed": outcome.retrieval_failed,
            },
        )

    def _high_value_lead(
        self, req, outcome: OutcomeContext
    ) -> EscalationDecision | None:
        """HIGH-VALUE LEAD gate — §3.4.5 weighted composite scoring.

        Fires when the normalized lead score >= LEAD_SCORE_THRESHOLD.
        The score = budget×0.5 + time_constraint×0.3 + purchase_intent×0.4,
        capped at 1.0 (see ``lead_capture.score_lead``).

        Pro/Enterprise: ``outcome.business_context_rules`` may carry
        custom-value rules that boost the score beyond the built-in
        heuristic. Free tier: no rules → built-in heuristic only.

        The normalized score is emitted as ``signal_confidence`` so the
        audit trail records it (Customer Journey Sarah Phase 6
        "lead-score 0.91").

        RESCAN TIER-C: replaces the old binary
        ``lead_value >= FREE_LEAD_BUDGET_THRESHOLD`` (750k) gate which
        was real-estate-biased and binary.
        """
        from app.cognition.lead_capture import score_lead

        # Re-compute final score applying any Pro/Enterprise custom rules.
        # When outcome.lead_score was set by the orchestrator (from the
        # LeadCandidate), we reuse it as the base and apply context rules.
        # When it was not set (legacy path, or no lead detected), score = 0.
        lead_candidate_mock = _LeadScoreHolder(
            lead_score=outcome.lead_score,
        )
        final_score = score_lead(
            lead_candidate_mock,
            business_context_rules=outcome.business_context_rules,
        )

        if final_score < LEAD_SCORE_THRESHOLD:
            return None

        return EscalationDecision(
            signal=SIGNAL_HIGH_VALUE_LEAD,
            gate=GATE_OUTCOME,
            admin_id=req.admin_id,
            session_id=req.session_id,
            luciel_instance_id=req.luciel_instance_id,
            user_id=req.user_id,
            signal_confidence=final_score,
            reasoning_excerpt=(
                f"lead_score {final_score:.2f} >= threshold {LEAD_SCORE_THRESHOLD:.2f}"
                + (
                    f" (budget={outcome.lead_value:.0f})"
                    if outcome.lead_value is not None
                    else ""
                )
                + (
                    " [custom rules applied]"
                    if outcome.business_context_rules
                    else ""
                )
            ),
            signal_inputs={
                "lead_score": final_score,
                "lead_score_threshold": LEAD_SCORE_THRESHOLD,
                "lead_value": outcome.lead_value,
                "tier": outcome.tier,
                "business_context_rules_applied": bool(
                    outcome.business_context_rules
                ),
            },
        )

    # ------------------------------------------------------------------
    # Lazy classifier builders
    # ------------------------------------------------------------------

    def _intent_classifier(self) -> IntentClassifier:
        if self._intent is None:
            from app.runtime.classifiers import LLMIntentClassifier

            self._intent = LLMIntentClassifier(model_router=self._model_router)
        return self._intent

    def _sentiment_classifier(self) -> SentimentClassifier:
        if self._sentiment is None:
            from app.runtime.classifiers import LLMSentimentClassifier

            self._sentiment = LLMSentimentClassifier(model_router=self._model_router)
        return self._sentiment


class _LeadScoreHolder:
    """Minimal duck-type accepted by ``score_lead`` from lead_capture.

    ``score_lead`` reads ``.lead_score`` and the ``key_facts`` /
    ``intent`` fields to match business-context rules. When the judge
    builds this from ``OutcomeContext`` (which only carries the scalar
    score), the text fields are empty — context rules that need them must
    operate on ``outcome.lead_score`` directly (which they do, because
    ``score_lead`` just adds the boost to ``candidate.lead_score``).
    """

    def __init__(self, *, lead_score: float) -> None:
        self.lead_score = lead_score
        self.key_facts: list[str] = []
        self.intent: str | None = None
