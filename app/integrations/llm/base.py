"""
Base LLM interface.

Every model provider client must implement this contract.
That way the rest of Luciel's runtime never depends on a specific
provider — it only depends on this interface.

To add a new provider later (e.g., Gemini, Mistral, Llama):
  1. Create a new file in this folder.
  2. Implement the LLMBase interface.
  3. Register it in the model router.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Generator


@dataclass
class LLMMessage:
    """
    A single message in a conversation.
    Maps to the standard role/content format used by most LLM APIs.
    """
    role: str       # "system", "user", or "assistant"
    content: str


@dataclass
class LLMRequest:
    """
    Everything needed to make one LLM call.

    messages:    The full conversation context including system prompt.
    model:       Which specific model to use (e.g., gpt-4o, claude-sonnet-4-20250514).
    temperature: Controls randomness. Lower = more focused, higher = more creative.
    max_tokens:  Maximum length of the response.
    """
    messages: list[LLMMessage]
    model: str | None = None
    temperature: float = 0.7
    max_tokens: int = 2048


@dataclass
class LLMResponse:
    """
    Standardized response from any provider.

    content:       The model's text output.
    model:         Which model actually generated the response.
    provider:      Which provider was used (openai, anthropic, etc.).
    usage:         Token usage for cost tracking and observability.
    finish_reason: Why the model stopped (e.g., stop, max_tokens).
    """
    content: str
    model: str
    provider: str
    usage: dict = field(default_factory=dict)
    finish_reason: str = ""


class LLMBase(ABC):
    """
    Abstract base class for LLM provider clients.
    Every provider must implement generate and generate_stream
    following this contract.
    """

    @abstractmethod
    def generate(self, request: LLMRequest) -> LLMResponse:
        """Send a request to the model and return a standardized response."""

    @abstractmethod
    def generate_stream(self, request: LLMRequest) -> Generator[str, None, None]:
        """
        Stream a response from the model, yielding one token/chunk at a time.
        Each yielded string is a small piece of the response text.
        """