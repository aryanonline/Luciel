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