"""
Anthropic provider client.

Implements LLMBase for Claude models (claude-sonnet-4-20250514, etc.).
Uses the official anthropic Python SDK.

Note: Anthropic's API separates the system prompt from messages,
so this client extracts the system message before calling the API.
"""

from __future__ import annotations

import anthropic

from app.core.config import settings
from app.integrations.llm.base import LLMBase, LLMRequest, LLMResponse


class AnthropicClient(LLMBase):

    def __init__(self) -> None:
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.default_model = settings.default_anthropic_model

    def generate(self, request: LLMRequest) -> LLMResponse:
        model = request.model or self.default_model

        # Anthropic requires system prompt to be passed separately,
        # not inside the messages array.
        system_prompt = ""
        messages = []

        for msg in request.messages:
            if msg.role == "system":
                system_prompt = msg.content
            else:
                messages.append({"role": msg.role, "content": msg.content})

        response = self.client.messages.create(
            model=model,
            system=system_prompt,
            messages=messages,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
        )

        content = response.content[0].text if response.content else ""

        return LLMResponse(
            content=content,
            model=response.model,
            provider="anthropic",
            usage={
                "input_tokens": response.usage.input_tokens if response.usage else 0,
                "output_tokens": response.usage.output_tokens if response.usage else 0,
            },
            finish_reason=response.stop_reason or "",
        )