"""
Model router with automatic provider fallback.

When the primary provider fails (overloaded, rate-limited, network error),
the router automatically retries with the next available provider.

Supports both regular generation and streaming.

Fallback order:
  1. Requested provider (e.g. "anthropic")
  2. All other configured providers in order (e.g. "openai")

This ensures chat never fails due to a single provider outage.
"""

from __future__ import annotations

import logging
from typing import Generator, Optional

from app.core.config import settings
from app.integrations.llm.base import LLMBase, LLMRequest, LLMResponse
from app.integrations.llm.openai_client import OpenAIClient
from app.integrations.llm.anthropic_client import AnthropicClient
from app.integrations.llm.stub_client import StubLLMClient

logger = logging.getLogger(__name__)


class ModelRouter:

    def __init__(self) -> None:
        self._providers: dict[str, LLMBase] = {}
        self._fallback_order: list[str] = []

        # Auto-register providers that have valid API keys configured.
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
        """Register a provider. First registered = first in fallback order."""
        self._providers[name] = provider
        if name not in self._fallback_order:
            self._fallback_order.append(name)
        logger.info("Registered LLM provider: %s", name)

    # ------------------------------------------------------------------
    # Generation with fallback
    # ------------------------------------------------------------------

    def generate(
        self,
        request: LLMRequest,
        *,
        preferred_provider: Optional[str] = None,
    ) -> LLMResponse:
        """
        Generate a response, falling back to other providers on failure.

        Args:
            request: The LLM request.
            preferred_provider: Try this provider first. If None, use the
                                default provider from settings.

        Returns:
            LLMResponse from whichever provider succeeded.

        Raises:
            RuntimeError: If ALL providers fail.
        """
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

        # All providers failed
        error_summary = "; ".join(
            f"{name}: {type(exc).__name__}" for name, exc in errors
        )
        raise RuntimeError(
            f"All LLM providers failed. Errors: {error_summary}"
        )

    # ------------------------------------------------------------------
    # Streaming with fallback
    # ------------------------------------------------------------------

    def generate_stream(
        self,
        request: LLMRequest,
        *,
        preferred_provider: Optional[str] = None,
    ) -> Generator[str, None, None]:
        """
        Stream a response, falling back to other providers on failure.

        Yields tokens one at a time. If the preferred provider fails
        before yielding any tokens, falls back to the next provider.

        Raises:
            RuntimeError: If ALL providers fail.
        """
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

        # All providers failed
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
        Build the provider attempt order.
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