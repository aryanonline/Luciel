"""HTTP client helper for Step 26 verification.

Extracted from the landed suite's `call()` function (commit 85a29f3,
scripts/step26_verify.py). Two changes vs. landed:

  1. `pooled_client()` context manager -- a single httpx.Client reused
     across all calls in a pillar (or the full run, at the caller's
     discretion). Landed suite opened a new client per call, which cost
     a TCP handshake on every assertion.
  2. `call()` accepts an optional `client=` arg so pillars can share a
     pooled client. Falls back to an ephemeral client if none passed --
     preserves the landed call signature for trivial one-shots.

Contract preserved from landed:
  - `expect=` is a single int or tuple of ints, status-code allowlist
  - Raises AssertionError on status mismatch, with response body
    truncated to 400 chars for log readability
  - Bearer header built via `h(key)` helper -- rejects non-str keys
    loudly (TypeError), matching landed behavior

Step 29 Commit B (closes D-call-helper-missing-params-kwarg-2026-05-05):
  - `params=` kwarg added. Forwarded to `httpx.Client.request(...)` which
    URL-encodes safely. Prior callers that needed query parameters were
    forced to inline `?key=value` into the path, which silently corrupts
    URL parse on the server side if any value contains `&`, `=`, `?`, or
    whitespace. P14 line ~349 (Phase 2 Commit 14, `42e95d1`) was the only
    such inlined caller; this commit also migrates that callsite back to
    `params=` form.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

import httpx

BASE_URL: str = os.environ.get("LUCIEL_BASE_URL", "http://127.0.0.1:8000")
REQUEST_TIMEOUT: float = float(os.environ.get("LUCIEL_REQUEST_TIMEOUT", "60.0"))


def h(key: str) -> dict[str, str]:
    """Bearer header. Rejects non-str keys loudly."""
    if not isinstance(key, str):
        raise TypeError(f"API key must be str, got {type(key).__name__}: {key!r}")
    return {"Authorization": f"Bearer {key}"}


@contextmanager
def pooled_client(*, base_url: str = BASE_URL, timeout: float = REQUEST_TIMEOUT) -> Iterator[httpx.Client]:
    """Single reusable httpx.Client for a run. Closes cleanly on exit."""
    with httpx.Client(base_url=base_url, timeout=timeout) as client:
        yield client


def call(
    method: str,
    path: str,
    key: str,
    *,
    json: Any = None,
    files: Any = None,
    data: Any = None,
    params: dict[str, Any] | list[tuple[str, Any]] | None = None,
    expect: int | tuple[int, ...] = 200,
    client: httpx.Client | None = None,
) -> httpx.Response:
    """Issue an HTTP request with bearer auth; assert status-code allowlist.

    If `client` is provided, it is reused (caller owns lifecycle). Otherwise
    an ephemeral client is opened for this single call.

    `params=` is forwarded to httpx, which URL-encodes safely. Use this for
    any query parameter; never inline `?k=v` into `path` because httpx will
    NOT re-encode an already-formed querystring, so values containing `&`,
    `=`, `?`, or whitespace silently corrupt URL parsing on the server.

    Returns the httpx.Response on success. Raises AssertionError on status
    mismatch, including method+path+expected+got and a truncated body.
    """
    def _do(c: httpx.Client) -> httpx.Response:
        r = c.request(
            method, path, headers=h(key), json=json, files=files, data=data, params=params
        )
        allowed = (expect,) if isinstance(expect, int) else tuple(expect)
        if r.status_code not in allowed:
            raise AssertionError(
                f"{method} {path} expected {allowed} got {r.status_code} {r.text[:400]}"
            )
        return r

    if client is not None:
        return _do(client)
    with httpx.Client(base_url=BASE_URL, timeout=REQUEST_TIMEOUT) as ephemeral:
        return _do(ephemeral)


def forensics_get(
    path: str,
    key: str,
    *,
    params: dict[str, Any] | list[tuple[str, Any]] | None = None,
    client: httpx.Client | None = None,
) -> httpx.Response:
    """GET a Step 29 forensic endpoint, expecting (200, 404).

    Step 29 Commit C.6 introduced this wrapper to name the recurring
    "GET-and-expect-(200,404)" pattern that pillars 11, 12, 13, and 14
    reproduce inline at every cross-pillar forensic lookup. The (200, 404)
    allowlist is correct for forensic GETs because:

      - 200 means the row exists; the caller inspects `r.json()`.
      - 404 means the row was teardown-raced or never created; the caller
        decides whether that is an assertion failure (e.g. P11 F10's
        instance_agent disappearing) or expected behavior (e.g. P14's A1
        K1 key existing post-departure).
      - Any other status (401/403/500/etc.) is a HARD failure of the
        forensic plane itself -- platform_admin gate failure, missing
        endpoint, server crash -- and `call()` raises AssertionError on it.

    The wrapper does NOT swallow 404; it simply admits 404 to the allowlist
    and returns the response so the caller can branch on `r.status_code`.
    This preserves every existing 404-handling block at the callsite (no
    assertion behavior changes); only the `"GET" + expect=(200, 404)` ritual
    is hoisted into a named function.

    Why not retrofit the strict `expect=200` forensic GETs too? Because
    those callsites are the much more common case where 404 IS itself a
    failure (e.g. P11 F1's `api_keys_step29c?id=` lookup of a key the
    pillar JUST created -- a 404 there means the create silently failed
    and the pillar should fail loudly). Wrapping those in a helper that
    admits 404 would mask exactly the failure mode they need to surface.
    Keep the strict-200 callers as `call("GET", ..., expect=200)`.
    """
    return call(
        "GET",
        path,
        key,
        params=params,
        expect=(200, 404),
        client=client,
    )