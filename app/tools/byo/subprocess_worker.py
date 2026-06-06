"""Subprocess entry point for BYO webhook dispatch — Arc 12 WU6.

This module is the CHILD-process side of the BYO sandbox. The parent
process (``sandbox.py``) spawns ``python -m app.tools.byo.subprocess_worker``
with the call envelope on stdin; the child performs the HTTP call
and writes the result envelope on stdout. The parent enforces the
30s hard timeout by killing the process if it does not exit in
time.

Input envelope (stdin, JSON one-shot)::

    {
      "endpoint_url": "https://...",
      "payload":       {...},
      "request_timeout_seconds": 8,
      "allowed_domains": ["api.example.com"]
    }

Output envelope (stdout, JSON one-shot)::

    {
      "ok": true,
      "status_code": 200,
      "response_body": {...},   # parsed JSON or empty dict on
                                 # text/non-JSON response
      "error_kind": null,        # one of: transport | egress_denied |
                                 # http_error | malformed_response |
                                 # other
      "error_message": null
    }

The CHILD does NOT validate the response against the output schema
— the parent does that after receiving the envelope so that schema
failure is mapped onto the correct retry policy (NEVER retry
schema failures).

The child re-checks the egress allowlist as defence in depth: the
parent has already checked it, but a compromised payload or a TOCTOU
DNS race should not be able to exfiltrate to an unlisted host.

Why a subprocess at all
-----------------------

§3.3.5 / Decision #5 mandates subprocess isolation: a hung webhook,
a malicious infinite-redirect loop, or a 100MB response body must
not take the worker process down. By running the HTTP call in a
child process, the parent can ``kill()`` after 30s and recover its
own event loop intact.
"""
from __future__ import annotations

import json
import socket
import sys
from typing import Any


# Hard cap on response body size we will read back into the parent —
# matches the §3.3.5 "100MB response" example. Anything larger is
# treated as a transport failure.
_MAX_RESPONSE_BYTES = 1024 * 1024  # 1 MiB; the spec's 100MB example
# is the upper bound of "abuse," not the legitimate target. 1 MiB is
# generous for a structured webhook response. Adjustable via env var
# if a future tier needs more.


def _resolve_host(url: str) -> str:
    """Extract the hostname from a URL (lowercase, no port)."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or ""
    return host.lower()


def _domain_allowed(host: str, allowed: list[str]) -> bool:
    """Case-insensitive exact-match check."""
    if not host:
        return False
    allowed_lower = {d.lower() for d in allowed}
    return host.lower() in allowed_lower


def _do_request(
    endpoint_url: str,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Perform the HTTP call. Returns the output envelope dict.

    Uses ``httpx`` (a pyproject dep) for the call; this avoids the
    extra TLS-config surface of ``urllib`` and is consistent with the
    rest of the codebase's HTTP clients.
    """
    try:
        import httpx
    except Exception as exc:  # pragma: no cover  # noqa: BLE001
        return {
            "ok": False,
            "status_code": 0,
            "response_body": {},
            "error_kind": "other",
            "error_message": f"httpx import failed: {exc}",
        }

    try:
        with httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
        ) as client:
            response = client.post(
                endpoint_url,
                json=payload,
                headers={
                    "User-Agent": "Luciel-BYO-Webhook/1.0",
                    "Content-Type": "application/json",
                },
            )
    except (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        httpx.TimeoutException,
    ) as exc:
        return {
            "ok": False,
            "status_code": 0,
            "response_body": {},
            "error_kind": "transport",
            "error_message": f"{type(exc).__name__}: {exc}",
        }
    except (httpx.RequestError, ssl_error_or_oserror()) as exc:
        return {
            "ok": False,
            "status_code": 0,
            "response_body": {},
            "error_kind": "transport",
            "error_message": f"{type(exc).__name__}: {exc}",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status_code": 0,
            "response_body": {},
            "error_kind": "other",
            "error_message": f"{type(exc).__name__}: {exc}",
        }

    # Size cap.
    body_bytes = response.content[:_MAX_RESPONSE_BYTES + 1]
    if len(body_bytes) > _MAX_RESPONSE_BYTES:
        return {
            "ok": False,
            "status_code": response.status_code,
            "response_body": {},
            "error_kind": "transport",
            "error_message": (
                f"response body exceeds {_MAX_RESPONSE_BYTES} bytes"
            ),
        }

    # HTTP error classes — 4xx/5xx are NOT transport. 5xx ⇒ http_error
    # which the parent treats as a transport-grade failure for the
    # circuit breaker (the endpoint is unhealthy). 4xx ⇒ http_error
    # NOT counted against the breaker (the request is malformed —
    # admin's problem, not endpoint availability).
    if response.status_code >= 400:
        # Try to parse body for the response envelope, but a 4xx/5xx
        # with HTML body is acceptable here — the parent surfaces
        # the status_code in the audit row regardless.
        try:
            parsed = response.json()
            if not isinstance(parsed, dict):
                parsed = {"raw": parsed}
        except Exception:  # noqa: BLE001
            parsed = {}
        return {
            "ok": False,
            "status_code": response.status_code,
            "response_body": parsed,
            "error_kind": "http_error",
            "error_message": (
                f"HTTP {response.status_code}"
            ),
        }

    # 2xx — parse JSON. A non-JSON or non-dict response body is a
    # malformed_response error which is terminal (no retry) but
    # also NOT a transport failure (the endpoint is reachable).
    try:
        parsed = response.json()
    except Exception:  # noqa: BLE001
        return {
            "ok": False,
            "status_code": response.status_code,
            "response_body": {},
            "error_kind": "malformed_response",
            "error_message": "response body is not JSON",
        }
    if not isinstance(parsed, dict):
        return {
            "ok": False,
            "status_code": response.status_code,
            "response_body": {},
            "error_kind": "malformed_response",
            "error_message": (
                f"response JSON is {type(parsed).__name__}, "
                "expected object"
            ),
        }

    return {
        "ok": True,
        "status_code": response.status_code,
        "response_body": parsed,
        "error_kind": None,
        "error_message": None,
    }


def ssl_error_or_oserror():
    """Tuple of exception classes treated as transport errors but
    not part of httpx's RequestError hierarchy (TLS handshake,
    DNS, broken pipe)."""
    import ssl as _ssl

    return (_ssl.SSLError, socket.gaierror, OSError)


def main() -> int:
    """Read the call envelope from stdin, do the call, write the
    result envelope to stdout. Returns 0 on success, 1 on any
    structured failure (still wrote a result envelope), 2 if the
    input could not be parsed at all (no envelope to write)."""
    try:
        raw = sys.stdin.read()
        call = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"BYO worker: bad input: {exc}\n")
        return 2

    endpoint_url = str(call.get("endpoint_url", ""))
    payload = call.get("payload", {}) or {}
    # Fallback only; the parent always passes the child budget
    # (``_CHILD_REQUEST_TIMEOUT_SECONDS`` = 8s, < the 10s hard
    # timeout, §3.8.6). The default mirrors that ceiling so a missing
    # field can never exceed the doctrine timeout.
    timeout_seconds = float(
        call.get("request_timeout_seconds", 8.0)
    )
    allowed_domains = list(call.get("allowed_domains", []) or [])

    if not endpoint_url:
        envelope = {
            "ok": False,
            "status_code": 0,
            "response_body": {},
            "error_kind": "other",
            "error_message": "missing endpoint_url",
        }
        sys.stdout.write(json.dumps(envelope))
        return 1

    # Defence-in-depth egress check — the parent already verified
    # this; the child re-checks so a compromised envelope cannot
    # exfiltrate.
    host = _resolve_host(endpoint_url)
    if not _domain_allowed(host, allowed_domains):
        envelope = {
            "ok": False,
            "status_code": 0,
            "response_body": {},
            "error_kind": "egress_denied",
            "error_message": (
                f"host {host!r} not in allowlist {allowed_domains!r}"
            ),
        }
        sys.stdout.write(json.dumps(envelope))
        return 1

    envelope = _do_request(endpoint_url, payload, timeout_seconds)
    try:
        sys.stdout.write(json.dumps(envelope))
    except Exception:  # noqa: BLE001  # pragma: no cover
        sys.stdout.write(
            '{"ok":false,"status_code":0,"response_body":{},'
            '"error_kind":"other","error_message":"non-serialisable envelope"}'
        )
    return 0 if envelope.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
