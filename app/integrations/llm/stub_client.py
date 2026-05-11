"""
Deterministic stub LLM provider.

Step 30d, Deliverable C (post-merge harness follow-up).

Purpose
=======

A hermetic LLMBase implementation that yields fixed tokens and never
makes a network call. Exists so the widget-surface E2E CI gate
(.github/workflows/widget-e2e.yml) can assert the happy-path SSE
contract -- session frame, one or more token frames, terminal
{"done": true} frame -- WITHOUT either of:

  * a billable OpenAI / Anthropic call on every workflow_dispatch
    (and, once the pull_request trigger lands per the Pattern E
    follow-up, on every contributor's push), or
  * a coupling to a live third-party API whose availability and
    quota status would inject flakiness into a contract test.

Parallel to KeywordModerationProvider in app/policy/moderation.py:
both exist to give the E2E harness a deterministic, hermetic seat
for a class of dependency (LLM here, content-safety there) without
disabling the gate-shape we are testing. Same construction-time
WARNING discipline so a misconfigured production deploy is
observable in the application log stream the first time the module
is imported.

Wiring
======

Registered by ModelRouter (app/integrations/llm/router.py) only when
settings.enable_stub_llm_provider is True. The setting defaults to
False so production is unaffected. The widget-e2e workflow flips it
to True via the ENABLE_STUB_LLM_PROVIDER env var.

What this module deliberately does NOT do
=========================================

  * No retry / fallback semantics. The router's own fallback loop is
    what we are exercising; the stub itself is a pure function.
  * No request-content inspection. The harness asserts the SSE
    contract shape, not the response body content. A deterministic
    fixed-token output is exactly the right signal.
  * No production seat. Registration is gated behind a settings flag
    that defaults to False, AND construction emits a WARNING so any
    deploy that flips the flag in production is loud.
"""

from __future__ import annotations

import logging
from typing import Generator

from app.integrations.llm.base import LLMBase, LLMRequest, LLMResponse

logger = logging.getLogger(__name__)


# Fixed output. Three tokens so the SSE harness sees more than one
# 'token' frame -- a single-token response would still satisfy the
# contract but a multi-token response also exercises the per-frame
# yield loop in chat_widget.py event_stream().
_STUB_TOKENS: tuple[str, ...] = ("E2E ", "stub ", "response.")
_STUB_TEXT: str = "".join(_STUB_TOKENS)
_STUB_MODEL: str = "stub-e2e"


class StubLLMClient(LLMBase):
    """Deterministic LLMBase impl. Never blocks, never errors, never
    calls out. See module docstring for the design rationale."""

    name = "stub"

    def __init__(self) -> None:
        # Loud at construction so a production deploy that flips
        # enable_stub_llm_provider=True is visible in the log stream
        # the first time the module is imported. Same discipline as
        # NullModerationProvider / empty-list KeywordModerationProvider.
        logger.warning(
            "StubLLMClient constructed -- this provider returns a fixed "
            "deterministic response and MUST NOT run in production. "
            "Gate this on settings.enable_stub_llm_provider=False for "
            "any non-CI environment."
        )

    def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content=_STUB_TEXT,
            model=_STUB_MODEL,
            provider=self.name,
            usage={},
            finish_reason="stop",
        )

    def generate_stream(self, request: LLMRequest) -> Generator[str, None, None]:
        for token in _STUB_TOKENS:
            yield token
