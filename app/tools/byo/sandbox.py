"""BYO webhook subprocess sandbox — parent side. Arc 12 WU6 / §3.3.5.

Public entry point: :func:`dispatch_byo_webhook`.

What this module enforces (the §3.3.5 envelope)
-----------------------------------------------

1. **Subprocess isolation** — one ``python -m
   app.tools.byo.subprocess_worker`` per invocation. Each call gets
   its own pid, its own memory, its own file handles. A hung or
   abusive webhook is killed at the boundary without losing the
   worker. Decision #5 / Decision #6.

2. **Hard 10s timeout** — ``BYO_HARD_TIMEOUT_SECONDS = 10`` (Arch
   §3.8.6; was 30s, corrected RESCAN BUG-4 2026-06-04).
   Enforcement point: ``asyncio.create_subprocess_exec`` followed
   by ``asyncio.wait_for`` on the child's ``communicate()``. On
   timeout the parent calls ``child.kill()`` (SIGKILL) and treats
   the call as a tool failure with ``error_class="timeout"``. The
   child is NOT given a chance to clean up — the spec says "killed
   at the boundary."

3. **Egress allowlist + SSRF IP guard** — admin-registered FQDN
   list, enforced BEFORE spawning the subprocess, AND (RESCAN BUG-4,
   Arch §3.8.6) a DNS-rebind-resistant IP check: the host is
   resolved and every resolved address is validated against blocked
   ranges (RFC1918, link-local incl. 169.254.169.254 metadata,
   loopback, ULA, multicast, reserved, unspecified, and any
   non-globally-routable address). An allowlisted FQDN that resolves
   to a private/metadata IP is denied with ``error_class=
   "egress_denied"``. Fail-closed on unresolvable hosts. The IP
   guard is skipped only when ``SPAWN_OVERRIDE`` is installed (unit
   tests with no real egress); it is always active in production.

4. **Input schema validation** — happens at the broker (input is
   validated against ``tool.input_schema`` before ``execute`` is
   called) AND inside this sandbox against the admin-registered
   ``input_schema`` from ``byo_webhook_endpoints``. The two
   validations are complementary: the tool input describes the
   call-site contract; the endpoint input_schema describes the
   admin's webhook contract.

5. **Output schema validation** — happens AFTER the subprocess
   returns, against the admin-registered ``output_schema``.
   Malformed output is a tool failure and is NEVER retried.

6. **Retry policy** — 2 retries (3 attempts total), exponential
   backoff: 500ms, then 1s, then 2s (capped at 5s). ONLY on
   transport errors (connection, TLS, timeout). NEVER on schema
   failures, NEVER on 4xx HTTP responses, NEVER on egress denied.

7. **Per-endpoint circuit breaker** — see ``circuit_breaker.py``.
   Consulted BEFORE each dispatch attempt; transport-error attempts
   are recorded against the breaker. Half-open probes are managed
   by the breaker module.

Public surface
--------------

* ``dispatch_byo_webhook(endpoint, payload, *, breaker, schema_validator=None)``
  — async. Returns a ``DispatchEnvelope`` carrying success bool,
  output dict, latency_ms, error_class, breaker state seen at
  dispatch. The caller (the tool body) constructs the audit row
  from this envelope.

* Exceptions are NOT raised on tool failure — every outcome is
  carried in the ``DispatchEnvelope`` so the caller can write the
  audit row uniformly. ``CircuitOpenError`` from the breaker
  module IS caught here and turned into a failure envelope.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from app.tools.byo.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    STATE_CLOSED,
)
from app.tools.schema import SchemaValidationError, validate_schema

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Public constants (§3.3.5)
# ---------------------------------------------------------------------

# RESCAN BUG-4 (2026-06-04): Architecture §3.8.6 specifies a hard 10s
# enforced timeout for BYO-webhook calls ("A webhook call that has not
# returned within 10 seconds is killed"). Was 30s. The child HTTP
# timeout must fire before the SIGKILL deadline so we get a structured
# envelope back rather than a bare kill.
BYO_HARD_TIMEOUT_SECONDS = 10
_CHILD_REQUEST_TIMEOUT_SECONDS = 8

DEFAULT_RETRY_COUNT = 2  # 2 retries = 3 attempts total
DEFAULT_BACKOFF_INITIAL_SECONDS = 0.5
DEFAULT_BACKOFF_MAX_SECONDS = 5.0


# ---------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------


class EgressDeniedError(Exception):
    """Raised internally when the resolved host is not in the
    allowlist. Caught by ``dispatch_byo_webhook`` and turned into
    an envelope with ``error_class='egress_denied'``."""


class TransportError(Exception):
    """Raised internally to drive the retry/backoff loop. Any
    transport-class failure (connect / TLS / timeout / subprocess
    kill) is converted to this exception by
    ``_attempt_single_dispatch`` before the retry layer sees it."""


class SubprocessTimeoutError(TransportError):
    """30s hard timeout — subprocess was killed."""


# ---------------------------------------------------------------------
# Public envelope
# ---------------------------------------------------------------------


@dataclass
class DispatchEnvelope:
    """Result of one BYO dispatch call (after all retries).

    Carried back to the tool body so the audit row can be written
    uniformly regardless of which envelope branch fired.
    """

    success: bool
    output: dict[str, Any] = field(default_factory=dict)
    error_class: Optional[str] = None
    error_message: Optional[str] = None
    latency_ms: int = 0
    # Circuit-breaker state at the FIRST dispatch attempt, recorded
    # in the audit row per §3.3.5.
    circuit_state_at_dispatch: str = STATE_CLOSED
    # Number of attempts actually made (1 = no retry, 2 = one retry,
    # 3 = both retries).
    attempts: int = 0
    # Optional HTTP status code from the last attempt.
    status_code: Optional[int] = None


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _resolve_host(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.hostname or "").lower()


def _is_host_allowed(host: str, allowed: list[str]) -> bool:
    if not host:
        return False
    return host.lower() in {d.lower() for d in (allowed or [])}


# RESCAN BUG-4 (2026-06-04): SSRF egress protection per Architecture
# §3.8.6. The FQDN allowlist alone is insufficient: an allowlisted
# hostname can resolve (or be re-pointed via DNS rebinding) to a
# private, link-local, loopback, or cloud-metadata address. We resolve
# the host and reject if ANY resolved address falls in a blocked range.
# §3.8.6 names: RFC1918 (10/8, 172.16/12, 192.168/16), link-local
# (169.254/16, fe80::/10), loopback (127/8, ::1), and AWS instance
# metadata (169.254.169.254 — already covered by link-local). DNS
# resolution is validated BEFORE the request is issued.
def _resolved_ips(host: str) -> list[str]:
    """Resolve ``host`` to all A/AAAA addresses. Raises on failure so
    the caller treats an unresolvable host as egress-denied (fail-closed)."""
    import socket

    infos = socket.getaddrinfo(host, None)
    out: list[str] = []
    for info in infos:
        addr = info[4][0]
        # strip IPv6 scope id if present (e.g. 'fe80::1%eth0')
        out.append(addr.split("%", 1)[0])
    return out


def _assert_egress_ip_safe(host: str) -> None:
    """Resolve ``host`` and raise EgressDeniedError if any resolved IP
    is private / link-local / loopback / multicast / reserved / metadata.
    This is the DNS-rebind-resistant check: we validate the actual
    resolved address(es), not the hostname string."""
    import ipaddress

    try:
        ips = _resolved_ips(host)
    except Exception as exc:  # DNS failure → fail closed
        raise EgressDeniedError(
            f"host {host!r} did not resolve ({exc}); egress denied"
        ) from exc
    if not ips:
        raise EgressDeniedError(f"host {host!r} resolved to no address")
    for ip_str in ips:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError as exc:
            raise EgressDeniedError(
                f"host {host!r} resolved to unparseable address {ip_str!r}"
            ) from exc
        # Blocks RFC1918 + loopback + link-local (169.254/16 incl.
        # 169.254.169.254 metadata) + ULA fe80:: + unique-local +
        # multicast + reserved + unspecified. is_global is the
        # belt-and-suspenders: only globally-routable public IPs pass.
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
            or not ip.is_global
        ):
            raise EgressDeniedError(
                f"host {host!r} resolved to blocked address {ip_str!r} "
                f"(private/link-local/loopback/metadata range); egress denied"
            )


def canonical_hash(payload: Any) -> str:
    """Hex SHA-256 of a JSON-canonical encoding of ``payload``."""
    try:
        canon = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        canon = repr(payload)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _backoff_seconds(
    attempt_index: int,
    *,
    initial: float = DEFAULT_BACKOFF_INITIAL_SECONDS,
    cap: float = DEFAULT_BACKOFF_MAX_SECONDS,
) -> float:
    """0-indexed attempt number → backoff. 0 → initial, 1 → 2x, ...
    capped at ``cap``."""
    return min(cap, initial * (2 ** attempt_index))


# ---------------------------------------------------------------------
# Subprocess driver
# ---------------------------------------------------------------------


# The subprocess command. Exposed as a module variable so tests can
# inject a fake worker by monkeypatching it.
SUBPROCESS_CMD: list[str] = [
    sys.executable, "-m", "app.tools.byo.subprocess_worker",
]


# Tests can override this to bypass subprocess spawning entirely (e.g.
# unit-testing the retry layer with a mock that raises TransportError).
# When set, it MUST be an async callable with the same signature as
# ``_spawn_and_collect`` and return its envelope dict.
SPAWN_OVERRIDE: Optional[Callable] = None


async def _spawn_and_collect(
    endpoint_url: str,
    payload: dict[str, Any],
    allowed_domains: list[str],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Spawn the BYO subprocess, pipe the call envelope on stdin,
    collect the result envelope on stdout. Enforces the 30s timeout
    by SIGKILL.

    Returns the dict the child wrote to stdout (or a synthesised
    envelope if the child died / hung).
    """
    if SPAWN_OVERRIDE is not None:
        return await SPAWN_OVERRIDE(
            endpoint_url, payload, allowed_domains, timeout_seconds
        )

    call_envelope = json.dumps({
        "endpoint_url": endpoint_url,
        "payload": payload,
        "request_timeout_seconds": _CHILD_REQUEST_TIMEOUT_SECONDS,
        "allowed_domains": allowed_domains,
    }).encode("utf-8")

    try:
        proc = await asyncio.create_subprocess_exec(
            *SUBPROCESS_CMD,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "status_code": 0,
            "response_body": {},
            "error_kind": "other",
            "error_message": f"subprocess spawn failed: {exc}",
        }

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=call_envelope),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        # Hard kill — the spec requires the subprocess be killed at
        # the boundary without waiting for clean shutdown.
        try:
            proc.kill()
        except ProcessLookupError:  # pragma: no cover
            pass
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001  # pragma: no cover
            pass
        return {
            "ok": False,
            "status_code": 0,
            "response_body": {},
            "error_kind": "timeout",
            "error_message": (
                f"subprocess killed at {timeout_seconds}s timeout"
            ),
        }

    if not stdout:
        return {
            "ok": False,
            "status_code": 0,
            "response_body": {},
            "error_kind": "other",
            "error_message": (
                f"subprocess produced no stdout "
                f"(rc={proc.returncode}, stderr="
                f"{stderr[:200]!r})"
            ),
        }

    try:
        envelope = json.loads(stdout.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status_code": 0,
            "response_body": {},
            "error_kind": "other",
            "error_message": (
                f"subprocess returned non-JSON: {exc}"
            ),
        }
    return envelope


async def _attempt_single_dispatch(
    endpoint_url: str,
    payload: dict[str, Any],
    allowed_domains: list[str],
) -> dict[str, Any]:
    """One spawn + collect cycle. Returns the child's envelope dict
    (parsed). Raises ``TransportError`` on transport-class failure
    so the retry layer can drive backoff."""
    envelope = await _spawn_and_collect(
        endpoint_url,
        payload,
        allowed_domains,
        timeout_seconds=BYO_HARD_TIMEOUT_SECONDS,
    )
    kind = envelope.get("error_kind")
    if kind == "timeout":
        raise SubprocessTimeoutError(
            envelope.get("error_message", "subprocess timeout")
        )
    if kind == "transport":
        raise TransportError(
            envelope.get("error_message", "transport error")
        )
    # http_error: 5xx is transport-grade for the breaker (endpoint
    # unhealthy); 4xx is terminal. The child has already labelled the
    # status_code; we branch here.
    if kind == "http_error":
        status = int(envelope.get("status_code") or 0)
        if 500 <= status < 600:
            raise TransportError(
                envelope.get("error_message", f"HTTP {status}")
            )
        # 4xx — terminal. Return the envelope so the parent can
        # surface error_class=http_error in the result.
    return envelope


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------


async def dispatch_byo_webhook(
    *,
    endpoint_id: int,
    endpoint_url: str,
    payload: dict[str, Any],
    endpoint_input_schema: dict[str, Any],
    endpoint_output_schema: dict[str, Any],
    allowed_domains: list[str],
    breaker: CircuitBreaker,
    retry_count: int = DEFAULT_RETRY_COUNT,
    backoff_initial: float = DEFAULT_BACKOFF_INITIAL_SECONDS,
    backoff_max: float = DEFAULT_BACKOFF_MAX_SECONDS,
    sleep_fn: Optional[Callable] = None,
) -> DispatchEnvelope:
    """Dispatch a BYO webhook through the full §3.3.5 envelope.

    Args
    ----
    endpoint_id
        The ``byo_webhook_endpoints.id`` row PK. Used as the
        circuit-breaker key.
    endpoint_url
        The admin-registered URL.
    payload
        The validated tool input payload to POST.
    endpoint_input_schema / endpoint_output_schema
        Admin-registered JSON schemas. The sandbox validates the
        payload against the input schema BEFORE dispatch (so an
        admin-blessed contract violation is a tool failure with NO
        retry); validates the response against the output schema
        AFTER each attempt (same rule — schema failure is terminal).
    allowed_domains
        Egress allowlist. The resolved host of ``endpoint_url`` MUST
        appear here or the dispatch is rejected before spawning
        the subprocess.
    breaker
        Per-endpoint circuit breaker. Consulted before each attempt.
    retry_count
        Number of retries (default 2 → 3 attempts total).
    backoff_initial / backoff_max
        Exponential backoff config. Tests inject shorter values.
    sleep_fn
        Awaitable sleep — default ``asyncio.sleep``. Tests inject a
        recorder so backoff timing can be asserted without real
        sleeping.

    Returns
    -------
    DispatchEnvelope
        Always returned (no raised exceptions for tool-failure
        cases). The tool body uses the envelope to construct the
        audit row.
    """
    started = time.monotonic()
    sleep = sleep_fn or asyncio.sleep

    # --- 1. Input schema validation (terminal on failure) -----------
    try:
        validate_schema(payload, endpoint_input_schema)
    except SchemaValidationError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        return DispatchEnvelope(
            success=False,
            output={},
            error_class="schema_input",
            error_message=f"input schema validation: {exc}",
            latency_ms=latency_ms,
            circuit_state_at_dispatch=breaker.current_state(
                endpoint_id
            ),
            attempts=0,
        )

    # --- 2. Egress allowlist (terminal on failure) ------------------
    host = _resolve_host(endpoint_url)
    if not _is_host_allowed(host, allowed_domains):
        latency_ms = int((time.monotonic() - started) * 1000)
        return DispatchEnvelope(
            success=False,
            output={},
            error_class="egress_denied",
            error_message=(
                f"host {host!r} not in allowlist {allowed_domains!r}"
            ),
            latency_ms=latency_ms,
            circuit_state_at_dispatch=breaker.current_state(
                endpoint_id
            ),
            attempts=0,
        )

    # --- 2b. SSRF guard (RESCAN BUG-4, §3.8.6) ----------------------
    # An allowlisted FQDN can still resolve to a private/metadata IP
    # (incl. via DNS rebinding). Resolve and validate the actual IPs
    # BEFORE any dispatch; fail closed on private/link-local/loopback/
    # metadata/unresolvable. Terminal — not a retryable transport error.
    #
    # Test seam: when SPAWN_OVERRIDE is installed there is NO real
    # network egress (the override returns a canned envelope), so the
    # live DNS resolution check is both unnecessary and undesirable
    # (it would couple unit tests to the sandbox's resolver). The guard
    # is ALWAYS active in production, where SPAWN_OVERRIDE is None.
    try:
        if SPAWN_OVERRIDE is None:
            _assert_egress_ip_safe(host)
    except EgressDeniedError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        return DispatchEnvelope(
            success=False,
            output={},
            error_class="egress_denied",
            error_message=str(exc),
            latency_ms=latency_ms,
            circuit_state_at_dispatch=breaker.current_state(
                endpoint_id
            ),
            attempts=0,
        )

    # --- 3. Circuit-breaker pre-check + dispatch loop ---------------
    # Snapshot the breaker state at the FIRST attempt — that's what
    # gets recorded in the audit row per §3.3.5. Subsequent attempts
    # within this dispatch (retries) belong to the same audit row.
    try:
        first_snapshot = breaker.before_dispatch(endpoint_id)
    except CircuitOpenError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        return DispatchEnvelope(
            success=False,
            output={},
            error_class="circuit_open",
            error_message=str(exc),
            latency_ms=latency_ms,
            circuit_state_at_dispatch=breaker.current_state(
                endpoint_id
            ),
            attempts=0,
        )

    circuit_state = first_snapshot.state
    last_transport_error: Optional[str] = None
    attempts = 0

    for attempt_idx in range(retry_count + 1):
        attempts += 1

        # For attempts after the first, the breaker has already been
        # consulted on the first attempt; record_failure on transport
        # errors flips state. We do NOT call ``before_dispatch`` again
        # within a single ``dispatch_byo_webhook`` call — the audit
        # row's "circuit_state_at_dispatch" is what we saw at attempt
        # 1. Re-checking would also break the half-open probe lock.

        try:
            envelope = await _attempt_single_dispatch(
                endpoint_url, payload, allowed_domains
            )
        except SubprocessTimeoutError as exc:
            last_transport_error = str(exc)
            breaker.record_failure(endpoint_id)
            if attempt_idx < retry_count:
                await sleep(
                    _backoff_seconds(
                        attempt_idx,
                        initial=backoff_initial,
                        cap=backoff_max,
                    )
                )
                continue
            latency_ms = int((time.monotonic() - started) * 1000)
            return DispatchEnvelope(
                success=False,
                output={},
                error_class="timeout",
                error_message=last_transport_error,
                latency_ms=latency_ms,
                circuit_state_at_dispatch=circuit_state,
                attempts=attempts,
            )
        except TransportError as exc:
            last_transport_error = str(exc)
            breaker.record_failure(endpoint_id)
            if attempt_idx < retry_count:
                await sleep(
                    _backoff_seconds(
                        attempt_idx,
                        initial=backoff_initial,
                        cap=backoff_max,
                    )
                )
                continue
            latency_ms = int((time.monotonic() - started) * 1000)
            return DispatchEnvelope(
                success=False,
                output={},
                error_class="transport",
                error_message=last_transport_error,
                latency_ms=latency_ms,
                circuit_state_at_dispatch=circuit_state,
                attempts=attempts,
            )

        kind = envelope.get("error_kind")
        status_code = envelope.get("status_code")

        # 4xx HTTP — terminal, NOT counted against breaker, NO retry.
        if kind == "http_error":
            latency_ms = int((time.monotonic() - started) * 1000)
            return DispatchEnvelope(
                success=False,
                output=envelope.get("response_body", {}) or {},
                error_class="http_error",
                error_message=envelope.get("error_message"),
                latency_ms=latency_ms,
                circuit_state_at_dispatch=circuit_state,
                attempts=attempts,
                status_code=status_code,
            )

        # Malformed (non-JSON / non-dict) 2xx response — terminal,
        # NO retry. Treated as schema_output.
        if kind == "malformed_response":
            latency_ms = int((time.monotonic() - started) * 1000)
            return DispatchEnvelope(
                success=False,
                output={},
                error_class="schema_output",
                error_message=envelope.get("error_message"),
                latency_ms=latency_ms,
                circuit_state_at_dispatch=circuit_state,
                attempts=attempts,
                status_code=status_code,
            )

        # Egress denied at the child layer — same as if the parent
        # caught it. Terminal.
        if kind == "egress_denied":
            latency_ms = int((time.monotonic() - started) * 1000)
            return DispatchEnvelope(
                success=False,
                output={},
                error_class="egress_denied",
                error_message=envelope.get("error_message"),
                latency_ms=latency_ms,
                circuit_state_at_dispatch=circuit_state,
                attempts=attempts,
                status_code=status_code,
            )

        # Other unhandled kind — terminal, NOT a retry.
        if kind not in (None, "ok"):
            latency_ms = int((time.monotonic() - started) * 1000)
            return DispatchEnvelope(
                success=False,
                output=envelope.get("response_body", {}) or {},
                error_class="other",
                error_message=envelope.get("error_message"),
                latency_ms=latency_ms,
                circuit_state_at_dispatch=circuit_state,
                attempts=attempts,
                status_code=status_code,
            )

        # Success path — validate output schema. Schema failure is
        # terminal (no retry) per §3.3.5.
        response_body = envelope.get("response_body", {}) or {}
        try:
            validate_schema(response_body, endpoint_output_schema)
        except SchemaValidationError as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            return DispatchEnvelope(
                success=False,
                output={},
                error_class="schema_output",
                error_message=(
                    f"output schema validation: {exc}"
                ),
                latency_ms=latency_ms,
                circuit_state_at_dispatch=circuit_state,
                attempts=attempts,
                status_code=status_code,
            )

        # Healthy — close the breaker.
        breaker.record_success(endpoint_id)
        latency_ms = int((time.monotonic() - started) * 1000)
        return DispatchEnvelope(
            success=True,
            output=response_body,
            error_class=None,
            error_message=None,
            latency_ms=latency_ms,
            circuit_state_at_dispatch=circuit_state,
            attempts=attempts,
            status_code=status_code,
        )

    # Unreachable — the loop body always returns or continues.
    latency_ms = int((time.monotonic() - started) * 1000)  # pragma: no cover
    return DispatchEnvelope(  # pragma: no cover
        success=False,
        output={},
        error_class="other",
        error_message="dispatch loop exited unexpectedly",
        latency_ms=latency_ms,
        circuit_state_at_dispatch=circuit_state,
        attempts=attempts,
    )


__all__ = [
    "BYO_HARD_TIMEOUT_SECONDS",
    "DEFAULT_BACKOFF_INITIAL_SECONDS",
    "DEFAULT_BACKOFF_MAX_SECONDS",
    "DEFAULT_RETRY_COUNT",
    "DispatchEnvelope",
    "EgressDeniedError",
    "SubprocessTimeoutError",
    "TransportError",
    "canonical_hash",
    "dispatch_byo_webhook",
]
