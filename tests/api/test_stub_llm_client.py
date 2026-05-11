"""
Unit tests for StubLLMClient (Step 30d Deliverable C harness follow-up).

These tests pin the behaviour the widget-e2e harness depends on:

  * StubLLMClient.generate(...) returns a deterministic, non-empty
    LLMResponse with provider='stub'.
  * StubLLMClient.generate_stream(...) yields at least one token and
    the joined token stream equals the .content of generate().
  * Construction emits a logger.warning at WARNING level so a future
    production deploy that flips settings.enable_stub_llm_provider=True
    is observable in the log stream the first time the module is
    imported.

The widget-e2e AST suite separately pins the *existence* of these
properties (class name, __init__ logger.warning call) at the cheap
backend-free job. This file pins *runtime* behaviour at the same
cheap job, since the stub has no transport and needs no fixtures.
"""

from __future__ import annotations

import logging

from app.integrations.llm.base import LLMMessage, LLMRequest
from app.integrations.llm.stub_client import StubLLMClient


def _request() -> LLMRequest:
    return LLMRequest(messages=[LLMMessage(role="user", content="ping")])


def test_stub_generate_returns_deterministic_response() -> None:
    client = StubLLMClient()
    r1 = client.generate(_request())
    r2 = client.generate(_request())
    assert r1.content == r2.content, (
        "StubLLMClient.generate must be deterministic across calls; "
        "the widget-e2e refusal/happy assertions rely on a fixed "
        "output for byte-stable SSE frames."
    )
    assert r1.provider == "stub", (
        "StubLLMClient.generate must set provider='stub' so router "
        "fallback logs identify which seat produced the response."
    )
    assert r1.content, "Stub response must be non-empty."


def test_stub_generate_stream_yields_tokens_matching_generate() -> None:
    client = StubLLMClient()
    tokens = list(client.generate_stream(_request()))
    assert tokens, (
        "StubLLMClient.generate_stream must yield at least one token. "
        "The widget-e2e harness asserts the SSE stream produces a "
        "'token' frame before the 'done' frame."
    )
    streamed = "".join(tokens)
    blocking = client.generate(_request()).content
    assert streamed == blocking, (
        "The stream and blocking paths must produce byte-identical "
        "output so a future swap between the two in chat_service does "
        "not silently change the harness's observed bytes."
    )


def test_stub_construction_emits_warning(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="app.integrations.llm.stub_client"):
        StubLLMClient()
    warning_records = [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING
        and rec.name == "app.integrations.llm.stub_client"
    ]
    assert warning_records, (
        "StubLLMClient.__init__ must emit a logger.warning so a "
        "production deploy with enable_stub_llm_provider=True is "
        "visible in the application log stream at the first import."
    )
    # Loosely pin the message intent: 'production' must appear so a
    # future cosmetic reword that drops the production-warning intent
    # is caught.
    assert any(
        "production" in rec.getMessage().lower()
        for rec in warning_records
    ), (
        "The construction WARNING must mention 'production' so an "
        "operator scanning the log stream understands why this "
        "warning matters. (Same intent-pin as the discipline in "
        "app/policy/moderation.py for NullModerationProvider.)"
    )
