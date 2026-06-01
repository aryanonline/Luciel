"""Arc 14 U2 — intake-signal classifiers (intent + sentiment).

The §3.4.5 Escalation Judgment Module needs two classifications it can
read from the inbound message ALONE, before PLAN runs:

  * **intent** — is the customer explicitly asking for a human?
    (``intent_class == "request_human"`` AND ``confidence >= 0.85``)
  * **sentiment** — is the customer strongly negative across the
    trailing window? (``sentiment <= -0.7`` with consistent polarity
    across ``>= 2`` of the trailing 3 messages)

Both are expressed as small ``Protocol`` seams (``IntentClassifier`` /
``SentimentClassifier``) so the orchestrator depends on the INTERFACE,
not a concrete provider. App code wires the LLM-backed implementations
(``LLMIntentClassifier`` / ``LLMSentimentClassifier``) which ride on the
existing provider-agnostic ``ModelRouter.generate`` text channel +
tolerant JSON parse (the same pattern PLAN uses — §5.4: we do NOT add
structured fields to ``LLMBase``). Tests inject deterministic fakes so
no network / API-cost is incurred (founder decision #2).

Doctrine: a classifier must NEVER crash the turn. Any LLM/parse failure
degrades to a neutral, non-firing result (intent ``"other"`` conf 0.0;
sentiment 0.0). The escalation module fails CLOSED on the escalation
decision in the sense that it does not invent an escalation signal from
a degraded classification — a degraded classifier simply does not fire.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

from app.integrations.llm.base import LLMMessage, LLMRequest

logger = logging.getLogger(__name__)


# The doctrinal intent label that the explicit-human-request signal
# keys off. Pinned as a module constant so the classifier, the judge,
# and the tests share one source of truth.
INTENT_REQUEST_HUMAN: str = "request_human"
INTENT_OTHER: str = "other"


@dataclass(frozen=True)
class IntentResult:
    """One intent classification of the latest inbound message.

    ``intent_class`` is a short label; the explicit-human-request signal
    fires only when it equals :data:`INTENT_REQUEST_HUMAN` AND
    ``confidence >= 0.85`` (the doctrinal threshold, §3.4.5).
    """

    intent_class: str
    confidence: float


@dataclass(frozen=True)
class SentimentResult:
    """Sentiment over a single customer message.

    ``score`` is in ``[-1.0, 1.0]`` where ``-1.0`` is maximally negative.
    The strong-negative-sentiment signal evaluates the trailing 3
    customer messages and fires when the latest score ``<= -0.7`` with
    consistent (negative) polarity across ``>= 2`` of those messages.
    """

    score: float


@runtime_checkable
class IntentClassifier(Protocol):
    """Classify the customer's latest message into an intent label."""

    def classify_intent(self, message: str) -> IntentResult:  # pragma: no cover - protocol
        ...


@runtime_checkable
class SentimentClassifier(Protocol):
    """Score the sentiment of a single customer message in [-1, 1]."""

    def score_sentiment(self, message: str) -> SentimentResult:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------
# LLM-backed implementations (real providers in app code only).
# ---------------------------------------------------------------------

_INTENT_INSTRUCTION = (
    "You are an intent classifier for a customer-support assistant. "
    "Classify ONLY the customer's latest message. Decide whether the "
    'customer is explicitly asking to be handed off to a human ("'
    f'{INTENT_REQUEST_HUMAN}") or anything else ("{INTENT_OTHER}").\n'
    "Respond with a SINGLE JSON object and nothing else, matching:\n"
    '{"intent_class": "request_human" | "other", '
    '"confidence": <number between 0 and 1>}'
)

_SENTIMENT_INSTRUCTION = (
    "You are a sentiment classifier for a customer-support assistant. "
    "Score ONLY the customer's latest message on a scale from -1.0 "
    "(extremely negative / angry / frustrated) through 0.0 (neutral) "
    "to 1.0 (very positive).\n"
    "Respond with a SINGLE JSON object and nothing else, matching:\n"
    '{"score": <number between -1 and 1>}'
)


class _LLMClassifierBase:
    """Shared LLM plumbing: one ``generate`` call + tolerant JSON parse.

    The router is injectable; when none is supplied it is built lazily
    so app-code call sites need no LLM kwarg (mirrors the orchestrator's
    lazy router). A lazily-built router with no configured provider
    raises inside ``generate`` — callers catch that and degrade.
    """

    def __init__(self, *, model_router=None) -> None:
        self._model_router = model_router

    def _router(self):
        if self._model_router is None:
            from app.integrations.llm.router import ModelRouter

            self._model_router = ModelRouter()
        return self._model_router

    def _generate_json(self, instruction: str, message: str) -> dict | None:
        request = LLMRequest(
            messages=[
                LLMMessage(role="system", content=instruction),
                LLMMessage(role="user", content=message),
            ],
            temperature=0.0,
        )
        try:
            response = self._router().generate(request)
        except Exception as exc:  # noqa: BLE001 — never crash the turn
            logger.warning(
                "classifier LLM call failed: exc_class=%s — degrading",
                type(exc).__name__,
            )
            return None
        return _load_json_object(response.content)


class LLMIntentClassifier(_LLMClassifierBase):
    """NLU intent classifier over the provider-agnostic LLM channel."""

    def classify_intent(self, message: str) -> IntentResult:
        obj = self._generate_json(_INTENT_INSTRUCTION, message)
        if obj is None:
            return IntentResult(intent_class=INTENT_OTHER, confidence=0.0)
        intent_class = obj.get("intent_class")
        if intent_class not in (INTENT_REQUEST_HUMAN, INTENT_OTHER):
            intent_class = INTENT_OTHER
        confidence = _clamp_unit(obj.get("confidence"), default=0.0)
        return IntentResult(intent_class=intent_class, confidence=confidence)


class LLMSentimentClassifier(_LLMClassifierBase):
    """Sentiment classifier over the provider-agnostic LLM channel."""

    def score_sentiment(self, message: str) -> SentimentResult:
        obj = self._generate_json(_SENTIMENT_INSTRUCTION, message)
        if obj is None:
            return SentimentResult(score=0.0)
        score = _clamp_signed_unit(obj.get("score"), default=0.0)
        return SentimentResult(score=score)


# ---------------------------------------------------------------------
# Parse helpers (tolerant — mirror plan_parser._load_json_object).
# ---------------------------------------------------------------------


def _load_json_object(raw_text: str) -> dict | None:
    text = (raw_text or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _clamp_unit(value, *, default: float) -> float:
    """Coerce to a float clamped to [0, 1]; ``default`` on bad input."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return default
    return max(0.0, min(1.0, float(value)))


def _clamp_signed_unit(value, *, default: float) -> float:
    """Coerce to a float clamped to [-1, 1]; ``default`` on bad input."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return default
    return max(-1.0, min(1.0, float(value)))


__all__ = [
    "INTENT_REQUEST_HUMAN",
    "INTENT_OTHER",
    "IntentResult",
    "SentimentResult",
    "IntentClassifier",
    "SentimentClassifier",
    "LLMIntentClassifier",
    "LLMSentimentClassifier",
]
