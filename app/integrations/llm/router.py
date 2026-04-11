"""
Model router.

This is the layer that decides which LLM provider handles a given request.
The rest of Luciel's runtime calls the router, never a provider directly.

Right now it supports explicit provider selection and a configurable default.
Later this can become smarter:
- cost-aware routing (use cheaper models for simple tasks)
- latency-aware routing (use fastest available provider)
- domain-aware routing (use specific models for specific domains)
- fallback routing (if primary provider fails, try secondary)
"""

from __future__ import annotations

from app.core.config import settings
from app.integrations.llm.base import LLMBase, LLMRequest, LLMResponse
from app.integrations.llm.openai_client import OpenAIClient
from app.integrations.llm.anthropic_client import AnthropicClient


class ModelRouter:
    """
    Routes LLM requests to the appropriate provider.

    Usage:
        router = ModelRouter()
        response = router.generate(request)                    # uses default
        response = router.generate(request, provider="openai") # explicit choice
    """

    def __init__(self) -> None:
        # Register all available providers.
        # To add a new provider, instantiate it here and add to the dict.
        self._providers: dict[str, LLMBase] = {}

        if settings.openai_api_key:
            self._providers["openai"] = OpenAIClient()

        if settings.anthropic_api_key:
            self._providers["anthropic"] = AnthropicClient()

        self._default_provider = settings.default_llm_provider

    @property
    def available_providers(self) -> list[str]:
        """List of providers that have valid API keys configured."""
        return list(self._providers.keys())

    def generate(
        self,
        request: LLMRequest,
        *,
        provider: str | None = None,
    ) -> LLMResponse:
        """
        Route a request to the specified or default provider.

        Args:
            request:  The LLM request with messages, model, and parameters.
            provider: Explicit provider name. Falls back to default if not given.

        Returns:
            Standardized LLMResponse regardless of which provider handled it.

        Raises:
            ValueError: If the requested provider is not available.
        """
        provider_name = provider or self._default_provider

        if provider_name not in self._providers:
            available = ", ".join(self.available_providers) or "none"
            raise ValueError(
                f"Provider '{provider_name}' is not available. "
                f"Available: {available}"
            )

        client = self._providers[provider_name]
        return client.generate(request)