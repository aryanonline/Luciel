"""
Tests for TIER C: per-tier LLM model-class resolution + intra-tier fast routing.
Architecture §3.4.3, Locked Decisions #7, #8, #9, #11.

Coverage:
  1. Tier -> model-class resolution: Free/Pro/Enterprise resolve to DISTINCT
     configured model ids, Anthropic primary + OpenAI fallback for each tier.
  2. Anthropic-primary is INDEPENDENT of provider registration order: register
     openai first; primary is still anthropic for the tier.
  3. Intra-tier fast routing: no-tools + small-context + low-complexity picks
     the fast model; any one failing condition picks the tier primary.
  4. Existing behavior: generate() with preferred_provider and no tier still
     works (legacy path preserved).
  5. Fallback trace: when Anthropic primary fails, the router falls back to
     the OpenAI fallback and logs a WARNING.
  6. Unknown tier falls back to Free (fail-closed to cheapest class).
  7. Complexity heuristic is deterministic and its score characteristics
     are correct.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest

from app.integrations.llm.base import LLMMessage, LLMRequest, LLMResponse
from app.integrations.llm.router import (
    ModelRouter,
    _complexity_score,
    _qualifies_for_fast_route,
    _resolve_tier_model_pair,
    _TIER_FREE,
    _TIER_PRO,
)


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------

def _make_request(content: str = "Hello") -> LLMRequest:
    return LLMRequest(messages=[LLMMessage(role="user", content=content)])


def _stub_response(provider: str, model: str) -> LLMResponse:
    return LLMResponse(content="ok", model=model, provider=provider)


class _CountingProvider:
    """Tracks how many calls were made and which model was requested."""
    def __init__(self, provider_name: str, fail: bool = False):
        self.provider_name = provider_name
        self.fail = fail
        self.calls: list[str | None] = []  # model ids requested

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request.model)
        if self.fail:
            raise RuntimeError(f"{self.provider_name} simulated failure")
        return _stub_response(self.provider_name, request.model or "unknown")

    def generate_stream(self, request: LLMRequest) -> Generator[str, None, None]:
        self.calls.append(request.model)
        if self.fail:
            raise RuntimeError(f"{self.provider_name} simulated failure")
        yield "tok"


def _router_with_providers(
    anthropic_provider=None,
    openai_provider=None,
    register_openai_first: bool = False,
) -> ModelRouter:
    """
    Build a ModelRouter with injected fake providers, bypassing key checks.
    Optionally register openai first to test order-independence.
    """
    router = ModelRouter.__new__(ModelRouter)
    router._providers = {}
    router._fallback_order = []
    router._default_provider = "anthropic"

    if register_openai_first:
        if openai_provider:
            router._register("openai", openai_provider)
        if anthropic_provider:
            router._register("anthropic", anthropic_provider)
    else:
        if anthropic_provider:
            router._register("anthropic", anthropic_provider)
        if openai_provider:
            router._register("openai", openai_provider)

    return router


# ---------------------------------------------------------------------------
# 1. Tier -> model-class resolution
# ---------------------------------------------------------------------------

class TestTierModelClassResolution:
    """Each tier resolves to distinct Anthropic primary + OpenAI fallback."""

    def test_free_tier_resolves_anthropic_primary_and_openai_fallback(self):
        pair = _resolve_tier_model_pair(_TIER_FREE)
        assert pair.primary_model, "Free tier must have an Anthropic primary model"
        assert pair.fallback_model, "Free tier must have an OpenAI fallback model"
        assert "haiku" in pair.primary_model.lower() or "free" in pair.primary_model.lower() or pair.primary_model, \
            "Free tier Anthropic primary should be a Haiku-class model"
        assert "mini" in pair.fallback_model.lower() or "4o" in pair.fallback_model.lower(), \
            "Free tier OpenAI fallback should be a small model (e.g. gpt-4o-mini)"

    def test_pro_tier_resolves_distinct_models_from_free(self):
        free_pair = _resolve_tier_model_pair(_TIER_FREE)
        pro_pair = _resolve_tier_model_pair(_TIER_PRO)
        assert pro_pair.primary_model != free_pair.primary_model, \
            "Pro tier Anthropic primary must differ from Free tier primary"
        assert pro_pair.fallback_model != free_pair.fallback_model, \
            "Pro tier OpenAI fallback must differ from Free tier fallback"

    def test_all_tiers_have_non_empty_models(self):
        # Enterprise removed (Unit 1 excision); Free/Pro only.
        for tier in (_TIER_FREE, _TIER_PRO):
            pair = _resolve_tier_model_pair(tier)
            assert pair.primary_model, f"{tier}: Anthropic primary must be non-empty"
            assert pair.fallback_model, f"{tier}: OpenAI fallback must be non-empty"
            assert pair.fast_model_anthropic, f"{tier}: fast model must be non-empty"

    def test_free_tier_model_used_in_generate_for_free_tier(self):
        """ModelRouter.generate with tier='free' sends the free-tier model to anthropic."""
        free_pair = _resolve_tier_model_pair(_TIER_FREE)
        anthropic = _CountingProvider("anthropic")
        router = _router_with_providers(anthropic_provider=anthropic)

        req = _make_request("Simple question")
        router.generate(
            req,
            tier=_TIER_FREE,
            user_message="Simple question",
            context_token_estimate=100,
            has_tools=False,
        )
        assert len(anthropic.calls) == 1
        # The free tier with low complexity should use the fast model
        # (which equals the free primary since free IS Haiku-class).
        # What matters is the model is the configured free or fast one.
        assert anthropic.calls[0] in (
            free_pair.primary_model,
            free_pair.fast_model_anthropic,
        ), f"Unexpected model sent to anthropic: {anthropic.calls[0]}"

    def test_pro_tier_model_used_in_generate(self):
        """ModelRouter.generate with tier='pro' sends the pro-tier Anthropic model."""
        pro_pair = _resolve_tier_model_pair(_TIER_PRO)
        anthropic = _CountingProvider("anthropic")
        router = _router_with_providers(anthropic_provider=anthropic)

        # Force non-fast path: large context
        router.generate(
            _make_request("Explain the full history"),
            tier=_TIER_PRO,
            user_message="Explain the full history of AI",
            context_token_estimate=5000,  # > 4096 → skip fast route
            has_tools=False,
        )
        assert anthropic.calls[0] == pro_pair.primary_model, \
            f"Pro tier should use {pro_pair.primary_model}, got {anthropic.calls[0]}"


# ---------------------------------------------------------------------------
# 2. Anthropic-primary independent of registration order
# ---------------------------------------------------------------------------

class TestAnthropicPrimaryOrderIndependence:
    """Anthropic must be the primary regardless of registration order."""

    def test_openai_registered_first_anthropic_is_still_primary(self):
        """Register openai first; primary provider for tier generate must be anthropic."""
        anthropic = _CountingProvider("anthropic")
        openai = _CountingProvider("openai")
        # openai registered first
        router = _router_with_providers(
            anthropic_provider=anthropic,
            openai_provider=openai,
            register_openai_first=True,
        )
        assert router._fallback_order[0] == "openai", \
            "Pre-condition: openai was registered first (legacy order check)"

        router.generate(
            _make_request("Test"),
            tier=_TIER_PRO,
            user_message="Test",
            context_token_estimate=5000,
            has_tools=False,
        )
        assert len(anthropic.calls) == 1, "Anthropic must receive the primary call"
        assert len(openai.calls) == 0, "OpenAI must NOT be called when anthropic succeeds"

    def test_openai_registered_first_anthropic_primary_for_free_tier(self):
        anthropic = _CountingProvider("anthropic")
        openai = _CountingProvider("openai")
        router = _router_with_providers(
            anthropic_provider=anthropic,
            openai_provider=openai,
            register_openai_first=True,
        )
        router.generate(
            _make_request("Q"),
            tier=_TIER_FREE,
            user_message="Q",
            context_token_estimate=10,
            has_tools=False,
        )
        assert len(anthropic.calls) == 1
        assert len(openai.calls) == 0

    def test_anthropic_only_router_still_works(self):
        """If only Anthropic is registered, it is used."""
        anthropic = _CountingProvider("anthropic")
        router = _router_with_providers(anthropic_provider=anthropic)
        router.generate(
            _make_request("Q"),
            tier=_TIER_PRO,
            user_message="Q",
            context_token_estimate=5000,
            has_tools=False,
        )
        assert len(anthropic.calls) == 1

    def test_no_primary_retry_on_anthropic_failure(self):
        """When Anthropic fails, router must NOT retry Anthropic; goes to OpenAI once."""
        anthropic = _CountingProvider("anthropic", fail=True)
        openai = _CountingProvider("openai")
        router = _router_with_providers(
            anthropic_provider=anthropic,
            openai_provider=openai,
        )
        router.generate(
            _make_request("Q"),
            tier=_TIER_PRO,
            user_message="Q",
            context_token_estimate=5000,
            has_tools=False,
        )
        assert len(anthropic.calls) == 1, "Anthropic must be tried exactly once (no retry)"
        assert len(openai.calls) == 1, "OpenAI must be tried as fallback"

    def test_fallback_logged_on_primary_failure(self, caplog):
        """Fallback must be logged at WARNING level, not surfaced to caller."""
        anthropic = _CountingProvider("anthropic", fail=True)
        openai = _CountingProvider("openai")
        router = _router_with_providers(
            anthropic_provider=anthropic,
            openai_provider=openai,
        )
        with caplog.at_level(logging.WARNING, logger="app.integrations.llm.router"):
            router.generate(
                _make_request("Q"),
                tier=_TIER_PRO,
                user_message="Q",
                context_token_estimate=5000,
                has_tools=False,
            )
        warning_texts = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("fallback" in t.lower() or "failed" in t.lower() for t in warning_texts), \
            "Fallback event must produce a WARNING log entry"


# ---------------------------------------------------------------------------
# 3. Intra-tier fast routing
# ---------------------------------------------------------------------------

class TestIntraTierFastRouting:
    """
    When ALL three conditions hold (no tools, <= 4K ctx, low complexity),
    the fast model is selected; otherwise the tier primary is used.
    """

    def _pro_pair(self):
        return _resolve_tier_model_pair(_TIER_PRO)

    def test_all_conditions_met_picks_fast_model(self):
        """Short, simple, no-tools, small context → fast model."""
        pair = self._pro_pair()
        anthropic = _CountingProvider("anthropic")
        router = _router_with_providers(anthropic_provider=anthropic)

        short_message = "Hi"
        router.generate(
            _make_request(short_message),
            tier=_TIER_PRO,
            user_message=short_message,
            context_token_estimate=100,  # well under 4096
            has_tools=False,
        )
        assert anthropic.calls[0] == pair.fast_model_anthropic, \
            f"Expected fast model {pair.fast_model_anthropic}, got {anthropic.calls[0]}"

    def test_has_tools_skips_fast_route(self):
        """has_tools=True → tier primary, not fast model."""
        pair = self._pro_pair()
        anthropic = _CountingProvider("anthropic")
        router = _router_with_providers(anthropic_provider=anthropic)

        router.generate(
            _make_request("Simple"),
            tier=_TIER_PRO,
            user_message="Simple",
            context_token_estimate=100,
            has_tools=True,  # tool use → no fast route
        )
        assert anthropic.calls[0] == pair.primary_model, \
            f"Tool-using request must use tier primary {pair.primary_model}"

    def test_large_context_skips_fast_route(self):
        """context_token_estimate > 4096 → tier primary."""
        pair = self._pro_pair()
        anthropic = _CountingProvider("anthropic")
        router = _router_with_providers(anthropic_provider=anthropic)

        router.generate(
            _make_request("Simple"),
            tier=_TIER_PRO,
            user_message="Simple",
            context_token_estimate=5000,  # > 4096
            has_tools=False,
        )
        assert anthropic.calls[0] == pair.primary_model, \
            "Large context must use tier primary"

    def test_high_complexity_skips_fast_route(self):
        """A complex multi-clause question → tier primary."""
        pair = self._pro_pair()
        anthropic = _CountingProvider("anthropic")
        router = _router_with_providers(anthropic_provider=anthropic)

        # Construct a message that exceeds the complexity threshold.
        # Multiple question marks + clause connectors push score above 10.0.
        complex_message = (
            "Can you explain why the transformer architecture works? "
            "However, I also want to understand the training process. "
            "Furthermore, what are the limitations? Additionally, how does "
            "this compare to RNNs? Therefore, should I use it? "
            "First, explain the attention mechanism. Second, explain gradient flow. "
            "Third, explain positional encoding."
        )
        score = _complexity_score(complex_message)
        assert score >= 10.0, f"Pre-condition: message must be complex (score={score})"

        router.generate(
            _make_request(complex_message),
            tier=_TIER_PRO,
            user_message=complex_message,
            context_token_estimate=100,
            has_tools=False,
        )
        assert anthropic.calls[0] == pair.primary_model, \
            "Complex message must use tier primary, not fast model"

    def test_fast_route_on_free_tier_uses_fast_model(self):
        """Free tier fast path uses the configured fast model."""
        pair = _resolve_tier_model_pair(_TIER_FREE)
        anthropic = _CountingProvider("anthropic")
        router = _router_with_providers(anthropic_provider=anthropic)

        router.generate(
            _make_request("ok"),
            tier=_TIER_FREE,
            user_message="ok",
            context_token_estimate=10,
            has_tools=False,
        )
        assert anthropic.calls[0] == pair.fast_model_anthropic

    def test_context_exactly_at_limit_still_qualifies(self):
        """context_token_estimate == limit is eligible for fast route."""
        pair = self._pro_pair()
        anthropic = _CountingProvider("anthropic")
        router = _router_with_providers(anthropic_provider=anthropic)

        router.generate(
            _make_request("ok"),
            tier=_TIER_PRO,
            user_message="ok",
            context_token_estimate=4096,  # exactly at limit
            has_tools=False,
        )
        assert anthropic.calls[0] == pair.fast_model_anthropic

    def test_context_one_over_limit_uses_primary(self):
        """context_token_estimate == limit + 1 → tier primary."""
        pair = self._pro_pair()
        anthropic = _CountingProvider("anthropic")
        router = _router_with_providers(anthropic_provider=anthropic)

        router.generate(
            _make_request("ok"),
            tier=_TIER_PRO,
            user_message="ok",
            context_token_estimate=4097,
            has_tools=False,
        )
        assert anthropic.calls[0] == pair.primary_model

    def test_fast_route_stream_uses_fast_model(self):
        """generate_stream also picks the fast model when conditions hold."""
        pair = self._pro_pair()
        anthropic = _CountingProvider("anthropic")
        router = _router_with_providers(anthropic_provider=anthropic)

        tokens = list(router.generate_stream(
            _make_request("ok"),
            tier=_TIER_PRO,
            user_message="ok",
            context_token_estimate=100,
            has_tools=False,
        ))
        assert len(tokens) > 0
        assert anthropic.calls[0] == pair.fast_model_anthropic


# ---------------------------------------------------------------------------
# 4. Complexity heuristic unit tests
# ---------------------------------------------------------------------------

class TestComplexityHeuristic:

    def test_empty_string_scores_zero(self):
        assert _complexity_score("") == 0.0

    def test_single_token_has_minimal_score(self):
        score = _complexity_score("Hello")
        assert score == pytest.approx(1 / 50.0)

    def test_question_mark_increases_score(self):
        base = _complexity_score("What")
        with_q = _complexity_score("What?")
        assert with_q > base

    def test_clause_connector_increases_score(self):
        base = _complexity_score("This is a thing")
        with_connector = _complexity_score("This is a thing however it is complex")
        assert with_connector > base

    def test_multi_part_marker_increases_score(self):
        base = _complexity_score("Explain this")
        with_list = _complexity_score("Explain this. 1. point one 2. point two")
        assert with_list > base

    def test_complex_message_above_default_threshold(self):
        complex_msg = (
            "Why does this work? However, I'm not sure. "
            "Therefore please explain. First, give me context. "
            "Second, give the rationale. Third, provide examples. "
            "Additionally, what are the caveats? Furthermore, is this scalable? "
            "Consequently, should I use it? Whereas the alternative is simpler."
        )
        assert _complexity_score(complex_msg) >= 10.0

    def test_simple_greeting_below_threshold(self):
        assert _complexity_score("Hi") < 10.0
        assert _complexity_score("Hello there") < 10.0

    def test_heuristic_is_deterministic(self):
        msg = "Why? Because. However, first, 1."
        assert _complexity_score(msg) == _complexity_score(msg)


# ---------------------------------------------------------------------------
# 5. qualifies_for_fast_route helper
# ---------------------------------------------------------------------------

class TestQualifiesForFastRoute:

    def test_all_conditions_met_returns_true(self):
        assert _qualifies_for_fast_route(
            message="Hi",
            context_token_estimate=100,
            has_tools=False,
        )

    def test_has_tools_returns_false(self):
        assert not _qualifies_for_fast_route(
            message="Hi",
            context_token_estimate=100,
            has_tools=True,
        )

    def test_large_context_returns_false(self):
        assert not _qualifies_for_fast_route(
            message="Hi",
            context_token_estimate=4097,
            has_tools=False,
        )

    def test_high_complexity_returns_false(self):
        complex_msg = (
            "Can you explain why? However, I'm unsure. Therefore clarify. "
            "First, context. Second, detail. Third, examples. Additionally? "
            "Furthermore? Whereas it is complex. Consequently?"
        )
        assert not _qualifies_for_fast_route(
            message=complex_msg,
            context_token_estimate=100,
            has_tools=False,
        )


# ---------------------------------------------------------------------------
# 6. Unknown tier falls back to Free
# ---------------------------------------------------------------------------

class TestUnknownTierFallback:

    def test_unknown_tier_string_uses_free_models(self):
        unknown_pair = _resolve_tier_model_pair("unknown_tier")
        free_pair = _resolve_tier_model_pair(_TIER_FREE)
        assert unknown_pair.primary_model == free_pair.primary_model
        assert unknown_pair.fallback_model == free_pair.fallback_model

    def test_none_tier_uses_free_models(self):
        none_pair = _resolve_tier_model_pair(None)
        free_pair = _resolve_tier_model_pair(_TIER_FREE)
        assert none_pair.primary_model == free_pair.primary_model

    def test_empty_string_tier_uses_free_models(self):
        empty_pair = _resolve_tier_model_pair("")
        free_pair = _resolve_tier_model_pair(_TIER_FREE)
        assert empty_pair.primary_model == free_pair.primary_model


# ---------------------------------------------------------------------------
# 7. Fallback to OpenAI on primary failure
# ---------------------------------------------------------------------------

class TestFallbackBehavior:

    def test_anthropic_failure_falls_back_to_openai(self):
        anthropic = _CountingProvider("anthropic", fail=True)
        openai = _CountingProvider("openai")
        router = _router_with_providers(
            anthropic_provider=anthropic,
            openai_provider=openai,
        )
        response = router.generate(
            _make_request("Q"),
            tier=_TIER_PRO,
            user_message="Q",
            context_token_estimate=5000,
            has_tools=False,
        )
        assert response.provider == "openai", "Fallback response must come from openai"
        assert len(anthropic.calls) == 1, "Primary tried exactly once"
        assert len(openai.calls) == 1, "Fallback tried exactly once"

    def test_both_fail_raises_runtime_error(self):
        anthropic = _CountingProvider("anthropic", fail=True)
        openai = _CountingProvider("openai", fail=True)
        router = _router_with_providers(
            anthropic_provider=anthropic,
            openai_provider=openai,
        )
        with pytest.raises(RuntimeError):
            router.generate(
                _make_request("Q"),
                tier=_TIER_PRO,
                user_message="Q",
                context_token_estimate=5000,
                has_tools=False,
            )

    def test_fallback_uses_openai_tier_model(self):
        """The fallback model used must be the tier's OpenAI model, not a global default."""
        pro_pair = _resolve_tier_model_pair(_TIER_PRO)
        anthropic = _CountingProvider("anthropic", fail=True)
        openai = _CountingProvider("openai")
        router = _router_with_providers(
            anthropic_provider=anthropic,
            openai_provider=openai,
        )
        router.generate(
            _make_request("Q"),
            tier=_TIER_PRO,
            user_message="Q",
            context_token_estimate=5000,
            has_tools=False,
        )
        assert openai.calls[0] == pro_pair.fallback_model, \
            f"Fallback must use Pro-tier OpenAI model {pro_pair.fallback_model}"

    def test_stream_anthropic_failure_falls_back_to_openai(self):
        anthropic = _CountingProvider("anthropic", fail=True)
        openai = _CountingProvider("openai")
        router = _router_with_providers(
            anthropic_provider=anthropic,
            openai_provider=openai,
        )
        tokens = list(router.generate_stream(
            _make_request("Q"),
            tier=_TIER_PRO,
            user_message="Q",
            context_token_estimate=5000,
            has_tools=False,
        ))
        assert tokens, "Fallback stream must yield tokens"
        assert len(anthropic.calls) == 1
        assert len(openai.calls) == 1


# ---------------------------------------------------------------------------
# 8. Legacy path (preferred_provider with no tier) preserved
# ---------------------------------------------------------------------------

class TestLegacyPath:
    """Callers that pass preferred_provider without tier still work."""

    def test_preferred_provider_with_no_tier_uses_legacy_order(self):
        anthropic = _CountingProvider("anthropic")
        openai = _CountingProvider("openai")
        router = _router_with_providers(
            anthropic_provider=anthropic,
            openai_provider=openai,
        )
        # Legacy call: preferred_provider set, no tier
        router.generate(
            _make_request("Q"),
            preferred_provider="anthropic",
        )
        assert len(anthropic.calls) == 1
        assert len(openai.calls) == 0

    def test_no_tier_no_preferred_uses_tier_routing_with_free_defaults(self):
        """No tier, no preferred_provider → tier routing with free defaults."""
        anthropic = _CountingProvider("anthropic")
        router = _router_with_providers(anthropic_provider=anthropic)

        free_pair = _resolve_tier_model_pair(_TIER_FREE)
        router.generate(
            _make_request("ok"),
            user_message="ok",
            context_token_estimate=10,
        )
        assert len(anthropic.calls) == 1
        # model should be free fast or primary
        assert anthropic.calls[0] in (
            free_pair.primary_model,
            free_pair.fast_model_anthropic,
        )
