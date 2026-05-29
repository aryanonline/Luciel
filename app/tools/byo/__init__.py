"""BYO webhook subprocess sandbox — Arc 12 WU6.

Sub-package for the §3.3.5 security envelope around the
``bring_your_own_webhook`` tool. The public surface is intentionally
minimal:

* ``dispatch_byo_webhook`` — entry point. Validates the input
  against the admin-registered input schema, enforces the egress
  allowlist, consults the per-endpoint circuit breaker, spawns the
  subprocess with a hard 30s timeout, retries transport-only errors
  with exponential backoff, validates the response against the
  output schema, and returns a structured result. The caller (the
  tool body) writes the audit row using the returned envelope.

* ``CircuitBreaker`` — Redis-backed per-endpoint breaker.

* ``EgressDeniedError`` — raised when the endpoint host is not in
  the admin-registered allowlist.

The sandbox is async-native because the broker calls
``tool.execute`` as a coroutine.
"""
from __future__ import annotations

from app.tools.byo.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
)
from app.tools.byo.sandbox import (
    BYO_HARD_TIMEOUT_SECONDS,
    DEFAULT_BACKOFF_INITIAL_SECONDS,
    DEFAULT_BACKOFF_MAX_SECONDS,
    DEFAULT_RETRY_COUNT,
    DispatchEnvelope,
    EgressDeniedError,
    SubprocessTimeoutError,
    TransportError,
    dispatch_byo_webhook,
)

__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "DispatchEnvelope",
    "EgressDeniedError",
    "SubprocessTimeoutError",
    "TransportError",
    "dispatch_byo_webhook",
    "BYO_HARD_TIMEOUT_SECONDS",
    "DEFAULT_BACKOFF_INITIAL_SECONDS",
    "DEFAULT_BACKOFF_MAX_SECONDS",
    "DEFAULT_RETRY_COUNT",
]
