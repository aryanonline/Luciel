"""
Provider-agnostic content-safety moderation gate.

Step 30d, Deliverable B.

Purpose
=======

A pluggable runtime gate that runs on the user message of every chat
turn arriving via the widget surface, before any LLM call happens. If
the gate blocks the turn, the route returns a sanitized refusal frame
(see app/api/v1/chat_widget.py); the user message never reaches the
foundation model, and the moderation categories never reach the
client.

Why a separate gate (not prompt-only)
=====================================

Content gating embedded in the system prompt is in-band with the same
model whose output we are trying to govern. It has no independent
audit signal, and it degrades silently when the model is swapped. The
out-of-band moderation gate runs before the LLM call, fails closed,
and emits a structured signal that survives provider changes
(ARCHITECTURE \xa74.9).

Provider model
==============

We define a Protocol (ModerationProvider) and ship two real
implementations:

  * OpenAIModerationProvider -- production default. Hits
    /v1/moderations with a strict timeout. Raises
    ModerationProviderUnavailable on network error / non-2xx /
    timeout.

  * NullModerationProvider -- never blocks. Used in unit tests that
    are not testing the gate itself, and in dev environments that
    have no provider key configured. Logs a WARNING on every call so
    it cannot silently ship to production.

The production wiring is the FailClosedModerationProvider wrapping
the OpenAIModerationProvider. If the OpenAI provider raises
ModerationProviderUnavailable, the wrapper returns
ModerationResult(blocked=True, ...) -- i.e. fail-closed. We do NOT
fall through to the LLM when moderation is unavailable: the entire
point of the gate is that an unmoderated public surface is a
liability, so an unavailable provider is treated the same as a
blocking category.

What this module deliberately does NOT do
=========================================

  * No audit-log row write. The structured audit-row write for chat
    turns is owned by a separate drift (the broader widget-chat audit
    work referenced in ARCHITECTURE \xa73.2.7 with its 📋 mark). The
    Step 30d gate emits a logger.warning at the route layer for the
    interim operator signal.

  * No LLM-output moderation. Step 30d's scope is the user-message
    side. Output-side moderation is a future hardening if we choose
    to open a drift for it.

  * No per-tenant policy overrides. The gate runs the same provider
    on every widget turn at v1. A future Step can layer per-tenant
    category thresholds (e.g., medical-vertical tenants might widen
    'self-harm' detection).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)


# =====================================================================
# Exceptions
# =====================================================================


class ModerationProviderUnavailable(Exception):
    """Raised by a ModerationProvider when it cannot reach its backend
    or got a non-2xx response. Callers (typically the
    FailClosedModerationProvider) translate this into a blocking
    ModerationResult so the chat turn is refused rather than passed
    through to the LLM.
    """


class ConfigurationError(Exception):
    """Raised at boot when the moderation provider is configured to a
    value the system cannot actually run -- e.g. moderation_provider
    is 'openai' but openai_api_key is empty. We fail loud at module
    import time rather than at first request so production deploys
    cannot ship with a silently-broken gate.
    """


# =====================================================================
# Result type
# =====================================================================


@dataclass
class ModerationResult:
    """Outcome of a single moderation call.

    Attributes
    ----------
    blocked : bool
        True iff the turn must NOT be passed to the LLM.
    categories : list[str]
        Provider-specific category labels (e.g. 'hate', 'self-harm',
        'provider_unavailable'). For internal logging only -- never
        returned to the client.
    provider : str
        Which provider produced this result. Composite labels like
        'openai+failclosed' are used by the wrapper.
    provider_request_id : str | None
        Vendor request id when available, for incident triage.
    """

    blocked: bool
    categories: list[str] = field(default_factory=list)
    provider: str = ""
    provider_request_id: str | None = None


# =====================================================================
# Provider protocol
# =====================================================================


class ModerationProvider(Protocol):
    """Minimal interface a moderation backend must satisfy.

    Implementations must raise ModerationProviderUnavailable (not a
    bare Exception or HTTPError) when the backend is unreachable, so
    the FailClosedModerationProvider wrapper can distinguish 'real
    block' from 'provider down'.
    """

    name: str

    def moderate(self, text: str) -> ModerationResult:  # pragma: no cover
        ...


# =====================================================================
# OpenAI provider (production default)
# =====================================================================


class OpenAIModerationProvider:
    """Calls OpenAI's /v1/moderations endpoint.

    Uses httpx with an explicit timeout. Raises
    ModerationProviderUnavailable on network error, timeout, or any
    non-2xx response. Maps the response into ModerationResult.
    """

    name = "openai"

    _ENDPOINT = "https://api.openai.com/v1/moderations"

    def __init__(self, api_key: str, timeout_seconds: float = 3.0) -> None:
        if not api_key:
            # Constructed only by ModerationGate.from_settings, which
            # already enforces this; double-check here so a future
            # caller cannot bypass.
            raise ConfigurationError(
                "OpenAIModerationProvider requires a non-empty api_key. "
                "Set OPENAI_API_KEY or switch moderation_provider to 'null' "
                "(development only)."
            )
        self._api_key = api_key
        self._timeout = timeout_seconds

    def moderate(self, text: str) -> ModerationResult:
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(
                    self._ENDPOINT,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    content=json.dumps({"input": text}),
                )
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            # We deliberately catch the broad httpx.HTTPError so any
            # transport / DNS / connect-time failure is funnelled into
            # the same exception class the fail-closed wrapper handles.
            raise ModerationProviderUnavailable(
                f"OpenAI moderation transport error: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if resp.status_code // 100 != 2:
            raise ModerationProviderUnavailable(
                f"OpenAI moderation returned HTTP {resp.status_code}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise ModerationProviderUnavailable(
                f"OpenAI moderation returned non-JSON body: {exc}"
            ) from exc

        results = data.get("results") or []
        if not results:
            # An empty results list is a contract violation on
            # OpenAI's side; treat it as unavailable rather than
            # silently passing the turn.
            raise ModerationProviderUnavailable(
                "OpenAI moderation returned empty results list"
            )

        first = results[0]
        flagged = bool(first.get("flagged", False))
        categories_map = first.get("categories") or {}
        # Collect every category whose value is truthy. The vendor
        # 'flagged' flag is the source of truth for blocked-vs-not;
        # the categories list is metadata for operator triage.
        triggered = [
            label
            for label, value in categories_map.items()
            if bool(value)
        ]

        return ModerationResult(
            blocked=flagged,
            categories=triggered,
            provider=self.name,
            provider_request_id=resp.headers.get("x-request-id"),
        )


# =====================================================================
# Null provider (dev / non-gate tests only)
# =====================================================================


class NullModerationProvider:
    """Never blocks. Logs a WARNING on every call.

    Exists so dev environments and unit tests that are not exercising
    the gate can run without an OpenAI key. The WARNING line is loud
    by design: any production environment that wires this in by
    accident will surface it in the application log stream
    immediately.
    """

    name = "null"

    def moderate(self, text: str) -> ModerationResult:
        logger.warning(
            "NullModerationProvider in use -- content safety gate is "
            "DISABLED. This must not run in production."
        )
        return ModerationResult(
            blocked=False,
            categories=[],
            provider=self.name,
        )


# =====================================================================
# Fail-closed wrapper (production wiring)
# =====================================================================


class FailClosedModerationProvider:
    """Wraps an inner provider; converts ModerationProviderUnavailable
    into a blocking result.

    This is the production wrapper. Rationale: an unmoderated public
    chat surface is a worse outcome than a few customers seeing a
    refusal when the moderation API is down. The refusal is sanitized
    and the operator sees a structured WARNING in the log stream, so
    the failure is observable and self-healing once the provider
    recovers.
    """

    def __init__(self, inner: ModerationProvider) -> None:
        self._inner = inner
        self.name = f"{inner.name}+failclosed"

    def moderate(self, text: str) -> ModerationResult:
        try:
            return self._inner.moderate(text)
        except ModerationProviderUnavailable as exc:
            logger.warning(
                "Moderation provider unavailable -- failing closed. "
                "inner=%s error=%s",
                self._inner.name,
                exc,
            )
            return ModerationResult(
                blocked=True,
                categories=["provider_unavailable"],
                provider=self.name,
                provider_request_id=None,
            )


# =====================================================================
# Gate factory
# =====================================================================


class ModerationGate:
    """Thin factory that reads settings and returns the right provider.

    The chat route imports a single module-level instance built from
    this factory so it never instantiates providers directly. Wiring
    lives in one place, the factory; the route just calls
    .moderate(text).
    """

    @staticmethod
    def from_settings(settings) -> ModerationProvider:
        """Build the production gate from a Settings instance.

        Recognised values for settings.moderation_provider:
          * 'openai'  -- FailClosedModerationProvider(OpenAIModerationProvider(...))
          * 'null'    -- NullModerationProvider (dev only)

        Raises ConfigurationError at call time for:
          * unknown provider value
          * 'openai' with empty openai_api_key

        We fail loud at module import (which is when from_settings is
        called) rather than at first request, so a misconfigured
        production deploy crash-loops on rollout rather than silently
        running with a disabled gate.
        """

        provider_name = getattr(settings, "moderation_provider", "openai")
        timeout = getattr(settings, "moderation_timeout_seconds", 3.0)
        fail_closed = getattr(settings, "moderation_fail_closed", True)

        if provider_name == "null":
            return NullModerationProvider()

        if provider_name == "openai":
            api_key = getattr(settings, "openai_api_key", "") or ""
            if not api_key:
                raise ConfigurationError(
                    "moderation_provider='openai' but openai_api_key is "
                    "empty. Set OPENAI_API_KEY or switch "
                    "moderation_provider to 'null' (development only)."
                )
            inner = OpenAIModerationProvider(
                api_key=api_key, timeout_seconds=timeout
            )
            if fail_closed:
                return FailClosedModerationProvider(inner)
            # Non-fail-closed mode is a development knob; we log a
            # WARNING so it is visible if anyone ships it.
            logger.warning(
                "moderation_fail_closed=False -- provider unavailability "
                "will pass turns through to the LLM. Do NOT run this "
                "configuration in production."
            )
            return inner

        raise ConfigurationError(
            f"Unknown moderation_provider={provider_name!r}. "
            f"Expected one of: 'openai', 'null'."
        )
