"""
OpenAI provider client.

Implements LLMBase for GPT models (gpt-4o, gpt-4.1, etc.).
Uses the official openai Python SDK.
"""

from __future__ import annotations

from typing import Generator

from openai import OpenAI

from app.core.config import settings
from app.integrations.llm.base import LLMBase, LLMRequest, LLMResponse


class OpenAIClient(LLMBase):

    def __init__(self) -> None:
        self.client = OpenAI(api_key=settings.openai_api_key)
        self.default_model = settings.default_openai_model

    def generate(self, request: LLMRequest) -> LLMResponse:
        model = request.model or self.default_model
        messages = [
            {"role": msg.role, "content": msg.content}
            for msg in request.messages
        ]

        response = self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
        )

        choice = response.choices[0]

        return LLMResponse(
            content=choice.message.content or "",
            model=response.model,
            provider="openai",
            usage={
                "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                "total_tokens": response.usage.total_tokens if response.usage else 0,
            },
            finish_reason=choice.finish_reason or "",
        )

    def generate_stream(self, request: LLMRequest) -> Generator[str, None, None]:
        """Stream tokens one at a time from OpenAI."""
        model = request.model or self.default_model
        messages = [
            {"role": msg.role, "content": msg.content}
            for msg in request.messages
        ]

        stream = self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            stream=True,
        )

        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content