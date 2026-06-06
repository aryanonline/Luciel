"""
Model router with automatic provider fallback and per-tier model-class
resolution (Architecture §3.4.3, Locked Decisions #7, #8, #9, #11).

=== Tier -> Model-Class Mapping (LOCKED, Decision #7 / #11) ===

The tier matrix (Vision §7) sells "model selection: base/mid/top" by tier.
What is LOCKED is the class assignment; specific version strings are
config-driven so ops can retune without a code change.

  Free       -> Anthropic small/fast (Haiku-class)    ; OpenAI small fallback
  Pro        -> Anthropic mid (Sonnet-class)           ; OpenAI mid fallback
  Enterprise -> Anthropic top (Sonnet/Opus per contract); OpenAI top fallback

Anthropic is ALWAYS the primary regardless of provider registration order
(Decision #8). OpenAI is the fallback, used only on primary non-200/timeout
with NO primary retry (preserving existing no-retry-on-primary behavior).

=== Intra-tier Fast Routing (Decision #9) ===

Within any tier, a message is routed to the tier's fast/cheap model
(Haiku-class) instead of the tier primary when ALL three conditions hold:

  (a) no tools are required for this message
  (b) retrieved context (prompt token estimate) <= 4 K tokens
  (c) query complexity score is below platform-tuned threshold

Complexity heuristic (deterministic):
  score = base_tokens / 50
        + question_mark_count * 2
        + clause_connector_count * 1.5
        + multi_part_indicator_count * 3

  where:
    base_tokens           = whitespace token count of the user message
    question_mark_count   = count of '?' characters
    clause_connector_count = count of clause connectors:
                             ('because', 'however', 'therefore', 'although',
                              'whereas', 'nevertheless', 'furthermore',
                              'additionally', 'consequently', 'moreover')
    multi_part_indicator  = count of list/multi-part markers:
                             ('first,', 'second,', 'third,', '1.', '2.',
                              '3.', 'a)', 'b)', 'c)')

  Default threshold: 10.0 (configurable via settings.llm_fast_route_complexity_threshold).
  Default context limit: 4096 tokens (configurable via settings.llm_fast_route_context_token_limit).

Fast routing is transparent: never surfaced to admins (Decision #11).

=== Fallback Behavior ===

Fallback order within a call:
  1. Tier-primary Anthropic model (always first — independent of registration)
  2. Tier-fallback OpenAI model (only on primary failure; no primary retry)

Provider fallback is logged (WARNING) but NEVER surfaced to the caller.
Prompt-caching behavior on AnthropicClient is preserved.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Generator, Optional

from app.core.config import settings
from app.integrations.llm.base import LLMBase, LLMRequest, LLMResponse
from app.integrations.llm.openai_client import OpenAIClient
from app.integrations.llm.anthropic_client import AnthropicClient
from app.integrations.llm.stub_client import StubLLMClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tier constants (canonical values from app.policy.entitlements)
# ---------------------------------------------------------------------------
_TIER_FREE = "free"
_TIER_PRO = "pro"
# Enterprise tier DEFERRED (Locked Decision #35); excised in Unit 1.

# Ordered so _resolve_model_pair can look up by index or key.
_KNOWN_TIERS = (_TIER_FREE, _TIER_PRO)


# ---------------------------------------------------------------------------
# Complexity heuristic
# ---------------------------------------------------------------------------

_CLAUSE_CONNECTORS: frozenset[str] = frozenset({
    "because", "however", "therefore", "although",
    "whereas", "nevertheless", "furthermore",
    "additionally", "consequently", "moreover",
})

_MULTI_PART_MARKERS: frozenset[str] = frozenset({
    "first,", "second,", "third,",
    "1.", "2.", "3.",
    "a)", "b)", "c)",
})


def _complexity_score(message: str) -> float:
    """
    Deterministic complexity heuristic for intra-tier fast routing.

    score = base_tokens / 50
          + question_mark_count * 2
          + clause_connector_count * 1.5
          + multi_part_indicator_count * 3

    Higher score = more complex. Scores >= threshold route to tier primary;
    scores < threshold route to the fast model (when context and tool
    conditions also hold).
    """
    tokens = message.split()
    base_tokens = len(tokens)

    question_marks = message.count("?")

    lowered_tokens = [t.lower() for t in tokens]
    clause_connectors = sum(1 for t in lowered_tokens if t in _CLAUSE_CONNECTORS)

    lowered = message.lower()
    multi_part = sum(1 for marker in _MULTI_PART_MARKERS if marker in lowered)

    score = (
        base_tokens / 50.0
        + question_marks * 2.0
        + clause_connectors * 1.5
        + multi_part * 3.0
    )
    return score


# ---------------------------------------------------------------------------
# Model pair resolution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _TierModelPair:
    """Primary (Anthropic) + fallback (OpenAI) concrete model IDs for a tier."""
    primary_model: str     # Anthropic model id for this tier
    fallback_model: str    # OpenAI model id for this tier
    fast_model_anthropic: str  # Anthropic fast model for intra-tier routing


def _resolve_tier_model_pair(tier: str) -> _TierModelPair:
    """
    Return the (Anthropic primary, OpenAI fallback, Anthropic fast) model IDs
    for the given tier.

    Tier is LOCKED to a class by Decisions #7/#11; concrete version strings
    are read from settings so ops can retune without a code change.

    Unknown tiers fall back to Free-tier (fail-closed to cheapest class).
    """
    tier_lower = (tier or "").lower()

    if tier_lower == _TIER_PRO:
        return _TierModelPair(
            primary_model=settings.anthropic_model_pro,
            fallback_model=settings.openai_model_pro,
            fast_model_anthropic=settings.anthropic_model_fast,
        )
    # Free tier (and unknown/unrecognised tiers — fail-closed to cheapest)
    return _TierModelPair(
        primary_model=settings.anthropic_model_free,
        fallback_model=settings.openai_model_free,
        fast_model_anthropic=settings.anthropic_model_fast,
    )


# ---------------------------------------------------------------------------
# Fast-route eligibility
# ---------------------------------------------------------------------------

def _qualifies_for_fast_route(
    *,
    message: str,
    context_token_estimate: int,
    has_tools: bool,
) -> bool:
    """
    Return True iff all three intra-tier fast-route conditions hold:
      (a) no tools required
      (b) context token estimate <= limit
      (c) complexity score < threshold

    Deterministic and side-effect free — safe to call from tests directly.
    """
    if has_tools:
        return False
    if context_token_estimate > settings.llm_fast_route_context_token_limit:
        return False
    score = _complexity_score(message)
    if score >= settings.llm_fast_route_complexity_threshold:
        return False
    return True


# ---------------------------------------------------------------------------
# ModelRouter
# ---------------------------------------------------------------------------

class ModelRouter:
    """
    Tier-aware LLM router with Anthropic-primary + OpenAI-fallback per tier
    and intra-tier fast routing.

    Public API:

        generate(request, *, tier=None, user_message=None,
                 context_token_estimate=0, has_tools=False,
                 preferred_provider=None) -> LLMResponse

        generate_stream(request, *, tier=None, user_message=None,
                        context_token_estimate=0, has_tools=False,
                        preferred_provider=None) -> Generator[str, None, None]

    The ``preferred_provider`` parameter is kept for back-compat with callers
    that pre-date tier routing; when supplied it overrides only the provider
    selection for the primary attempt (not the model id). In normal flow the
    tier determines everything.
    """

    def __init__(self) -> None:
        self._providers: dict[str, LLMBase] = {}
        # Registration order is preserved for back-compat (available_providers
        # property) but does NOT control primary/fallback for tier-aware calls.
        self._fallback_order: list[str] = []

        # Auto-register providers that have valid API keys configured.
        # Registration ORDER no longer determines Anthropic-primary status;
        # _tier_generate always tries anthropic first regardless of order.
        if settings.openai_api_key:
            self._register("openai", OpenAIClient())
        if settings.anthropic_api_key:
            self._register("anthropic", AnthropicClient())

        # Hermetic stub provider for the widget-e2e CI harness
        # (Step 30d Deliverable C). Off by default so production is
        # unaffected; the harness flips it via ENABLE_STUB_LLM_PROVIDER.
        # StubLLMClient logs a WARNING at construction so a deploy
        # that flips the flag in production is observable.
        if getattr(settings, "enable_stub_llm_provider", False):
            self._register("stub", StubLLMClient())

        self._default_provider = settings.default_llm_provider

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register(self, name: str, provider: LLMBase) -> None:
        """Register a provider. First registered = first in legacy fallback order."""
        self._providers[name] = provider
        if name not in self._fallback_order:
            self._fallback_order.append(name)
        logger.info("Registered LLM provider: %s", name)

    # ------------------------------------------------------------------
    # Tier-aware generation (primary path)
    # ------------------------------------------------------------------

    def _select_model_for_request(
        self,
        *,
        tier: Optional[str],
        user_message: str,
        context_token_estimate: int,
        has_tools: bool,
    ) -> tuple[str, str, str]:
        """
        Return (primary_provider, primary_model, fallback_model).

        primary_provider is always 'anthropic' (Decision #8).
        primary_model is the tier's class model, or the fast model when
        intra-tier fast routing conditions hold.
        fallback_model is the tier's OpenAI model.
        """
        pair = _resolve_tier_model_pair(tier or _TIER_FREE)

        if _qualifies_for_fast_route(
            message=user_message,
            context_token_estimate=context_token_estimate,
            has_tools=has_tools,
        ):
            logger.debug(
                "Intra-tier fast route: tier=%s fast_model=%s ctx_tokens=%d",
                tier,
                pair.fast_model_anthropic,
                context_token_estimate,
            )
            primary_model = pair.fast_model_anthropic
        else:
            primary_model = pair.primary_model

        return "anthropic", primary_model, pair.fallback_model

    def _tier_generate(
        self,
        request: LLMRequest,
        *,
        tier: Optional[str],
        user_message: str,
        context_token_estimate: int,
        has_tools: bool,
    ) -> LLMResponse:
        """
        Execute one blocking generation with tier-aware primary + single fallback.
        No retry on primary (preserved behavior). Fallback logged, not surfaced.
        """
        primary_provider, primary_model, fallback_model = self._select_model_for_request(
            tier=tier,
            user_message=user_message,
            context_token_estimate=context_token_estimate,
            has_tools=has_tools,
        )

        # ---- Primary attempt: Anthropic (LOCKED by Decision #8) ----
        primary = self._providers.get(primary_provider)
        if primary is not None:
            primary_request = LLMRequest(
                messages=request.messages,
                model=primary_model,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
            )
            try:
                logger.info(
                    "Tier %s: trying primary provider=%s model=%s",
                    tier, primary_provider, primary_model,
                )
                response = primary.generate(primary_request)
                return response
            except Exception as exc:
                logger.warning(
                    "Tier %s: primary provider=%s model=%s failed: %s — %s; "
                    "falling back to openai model=%s",
                    tier, primary_provider, primary_model,
                    type(exc).__name__, exc, fallback_model,
                )
                # No primary retry — fall through to fallback immediately.

        # ---- Fallback: OpenAI ----
        fallback_provider_name = "openai"
        fallback = self._providers.get(fallback_provider_name)
        if fallback is not None:
            fallback_request = LLMRequest(
                messages=request.messages,
                model=fallback_model,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
            )
            try:
                logger.info(
                    "Tier %s: trying fallback provider=%s model=%s",
                    tier, fallback_provider_name, fallback_model,
                )
                response = fallback.generate(fallback_request)
                logger.info(
                    "Tier %s: fallback provider=%s succeeded",
                    tier, fallback_provider_name,
                )
                return response
            except Exception as exc:
                logger.warning(
                    "Tier %s: fallback provider=%s model=%s failed: %s — %s",
                    tier, fallback_provider_name, fallback_model,
                    type(exc).__name__, exc,
                )

        # ---- Stub fallback (for harness / dev only) ----
        stub = self._providers.get("stub")
        if stub is not None:
            logger.info("Tier %s: using stub provider", tier)
            return stub.generate(request)

        raise RuntimeError(
            f"All LLM providers failed for tier={tier}. "
            "No anthropic, openai, or stub provider available."
        )

    def _tier_generate_stream(
        self,
        request: LLMRequest,
        *,
        tier: Optional[str],
        user_message: str,
        context_token_estimate: int,
        has_tools: bool,
    ) -> Generator[str, None, None]:
        """
        Execute one streaming generation with tier-aware primary + single fallback.
        No retry on primary (preserved behavior). Fallback logged, not surfaced.
        """
        primary_provider, primary_model, fallback_model = self._select_model_for_request(
            tier=tier,
            user_message=user_message,
            context_token_estimate=context_token_estimate,
            has_tools=has_tools,
        )

        # ---- Primary attempt: Anthropic ----
        primary = self._providers.get(primary_provider)
        if primary is not None:
            primary_request = LLMRequest(
                messages=request.messages,
                model=primary_model,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
            )
            try:
                logger.info(
                    "Tier %s: trying stream primary provider=%s model=%s",
                    tier, primary_provider, primary_model,
                )
                for token in primary.generate_stream(primary_request):
                    yield token
                return
            except Exception as exc:
                logger.warning(
                    "Tier %s: stream primary provider=%s model=%s failed: "
                    "%s — %s; falling back to openai model=%s",
                    tier, primary_provider, primary_model,
                    type(exc).__name__, exc, fallback_model,
                )

        # ---- Fallback: OpenAI ----
        fallback_provider_name = "openai"
        fallback = self._providers.get(fallback_provider_name)
        if fallback is not None:
            fallback_request = LLMRequest(
                messages=request.messages,
                model=fallback_model,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
            )
            try:
                logger.info(
                    "Tier %s: trying stream fallback provider=%s model=%s",
                    tier, fallback_provider_name, fallback_model,
                )
                for token in fallback.generate_stream(fallback_request):
                    yield token
                logger.info(
                    "Tier %s: stream fallback provider=%s succeeded",
                    tier, fallback_provider_name,
                )
                return
            except Exception as exc:
                logger.warning(
                    "Tier %s: stream fallback provider=%s model=%s failed: %s — %s",
                    tier, fallback_provider_name, fallback_model,
                    type(exc).__name__, exc,
                )

        # ---- Stub fallback (for harness / dev only) ----
        stub = self._providers.get("stub")
        if stub is not None:
            logger.info("Tier %s: using stub provider for stream", tier)
            for token in stub.generate_stream(request):
                yield token
            return

        raise RuntimeError(
            f"All LLM providers failed (stream) for tier={tier}. "
            "No anthropic, openai, or stub provider available."
        )

    # ------------------------------------------------------------------
    # Public API — generation with fallback
    # ------------------------------------------------------------------

    def generate(
        self,
        request: LLMRequest,
        *,
        preferred_provider: Optional[str] = None,
        tier: Optional[str] = None,
        user_message: Optional[str] = None,
        context_token_estimate: int = 0,
        has_tools: bool = False,
    ) -> LLMResponse:
        """
        Generate a response with tier-aware model selection and provider fallback.

        Args:
            request: The LLM request. model field may be overridden by tier
                     resolution; pass model=None to let tier routing decide.
            preferred_provider: BACK-COMPAT. When supplied alongside a tier,
                                 tier routing takes precedence. When supplied
                                 without a tier (legacy callers), falls back to
                                 the legacy _build_fallback_order path.
            tier: The admin's subscription tier ('free', 'pro', 'enterprise').
                  When None and no preferred_provider, uses _TIER_FREE (Haiku).
            user_message: The current user turn text, used for complexity
                          scoring. When None, complexity check is skipped
                          (scores 0 — qualifies for fast route if other
                          conditions hold).
            context_token_estimate: Approximate token count of retrieved
                                    context passed to the LLM. Used for the
                                    4K context check (Decision #9).
            has_tools: True if the current turn requires tool use. When True
                       the fast route is skipped (Decision #9 condition a).

        Returns:
            LLMResponse from whichever provider succeeded.

        Raises:
            RuntimeError: If ALL providers fail.
        """
        # When a tier is provided (or we can infer one), always use tier routing.
        # When neither tier nor preferred_provider is provided, default to free
        # tier routing (Anthropic primary, Haiku-class).
        effective_user_message = user_message or ""

        # If request already has a model pinned (legacy callers that pre-date
        # tier routing), honour it only if no tier is given. With a tier, the
        # tier determines the model.
        if tier is not None or preferred_provider is None:
            return self._tier_generate(
                request,
                tier=tier,
                user_message=effective_user_message,
                context_token_estimate=context_token_estimate,
                has_tools=has_tools,
            )

        # Legacy path: preferred_provider supplied, no tier. Preserve original
        # fallback-order behavior so existing call sites still work.
        order = self._build_fallback_order(preferred_provider)
        errors: list[tuple[str, Exception]] = []
        for provider_name in order:
            provider = self._providers.get(provider_name)
            if provider is None:
                continue
            try:
                logger.info("Trying provider: %s", provider_name)
                response = provider.generate(request)
                if errors:
                    logger.info(
                        "Provider %s succeeded after %d fallback(s)",
                        provider_name,
                        len(errors),
                    )
                return response
            except Exception as exc:
                logger.warning(
                    "Provider %s failed: %s — %s",
                    provider_name,
                    type(exc).__name__,
                    exc,
                )
                errors.append((provider_name, exc))

        error_summary = "; ".join(
            f"{name}: {type(exc).__name__}" for name, exc in errors
        )
        raise RuntimeError(
            f"All LLM providers failed. Errors: {error_summary}"
        )

    def generate_stream(
        self,
        request: LLMRequest,
        *,
        preferred_provider: Optional[str] = None,
        tier: Optional[str] = None,
        user_message: Optional[str] = None,
        context_token_estimate: int = 0,
        has_tools: bool = False,
    ) -> Generator[str, None, None]:
        """
        Stream a response with tier-aware model selection and provider fallback.

        Yields tokens one at a time. If the primary provider fails before
        yielding any tokens, falls back to the next provider.

        Args: (same as generate())

        Raises:
            RuntimeError: If ALL providers fail.
        """
        effective_user_message = user_message or ""

        if tier is not None or preferred_provider is None:
            yield from self._tier_generate_stream(
                request,
                tier=tier,
                user_message=effective_user_message,
                context_token_estimate=context_token_estimate,
                has_tools=has_tools,
            )
            return

        # Legacy path
        order = self._build_fallback_order(preferred_provider)
        errors: list[tuple[str, Exception]] = []
        for provider_name in order:
            provider = self._providers.get(provider_name)
            if provider is None:
                continue
            try:
                logger.info("Trying stream provider: %s", provider_name)
                for token in provider.generate_stream(request):
                    yield token
                if errors:
                    logger.info(
                        "Stream provider %s succeeded after %d fallback(s)",
                        provider_name,
                        len(errors),
                    )
                return
            except Exception as exc:
                logger.warning(
                    "Stream provider %s failed: %s — %s",
                    provider_name,
                    type(exc).__name__,
                    exc,
                )
                errors.append((provider_name, exc))

        error_summary = "; ".join(
            f"{name}: {type(exc).__name__}" for name, exc in errors
        )
        raise RuntimeError(
            f"All LLM providers failed (stream). Errors: {error_summary}"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_fallback_order(
        self, preferred: Optional[str] = None
    ) -> list[str]:
        """
        Build the provider attempt order (legacy path).
        Preferred provider goes first, then the rest in registration order.
        If no preferred provider, use the default from settings.
        """
        chosen = preferred or self._default_provider
        if chosen and chosen in self._providers:
            rest = [p for p in self._fallback_order if p != chosen]
            return [chosen] + rest
        return list(self._fallback_order)

    @property
    def available_providers(self) -> list[str]:
        return list(self._providers.keys())
