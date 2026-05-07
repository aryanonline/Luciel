"""Unit tests for app.verification.http_client:call(params=...).

Step 29 Commit B (closes D-call-helper-missing-params-kwarg-2026-05-05).

These tests run without a backend. They use httpx's MockTransport to capture
the outgoing request and assert the URL was built correctly. We test:

  1. params= dict forms a correctly-encoded querystring on the wire
  2. params= with a value containing reserved chars (`&`, `=`, `?`, space,
     colon) is URL-encoded so the server parses the original value back
  3. Bearer header is still present alongside params=
  4. Backwards-compat: callers that do NOT pass params= behave exactly
     as before (no querystring on the wire)

Why this test exists: the Phase 2 Commit 14 workaround inlined `?k=v` into
the path because call() had no params= kwarg. That worked for a controlled
f-string label, but it was a security-evidence concern (a future caller
passing a label with `&` or `=` would silently corrupt URL parsing on the
FastAPI side -- audit row written, wrong audit_label captured, PIPEDA P5 /
SOC 2 CC7.2 evidence trail corrupted). This test pins the new contract so
no future regression can re-introduce that fragility.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.verification.http_client import call


def _mock_client(captured: list[httpx.Request]) -> httpx.Client:
    """httpx.Client with a MockTransport that records each request and
    returns 200 OK with empty JSON body."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={})

    return httpx.Client(
        base_url="http://test.invalid",
        transport=httpx.MockTransport(handler),
    )


def test_params_dict_builds_querystring_on_the_wire() -> None:
    """call(..., params={'k': 'v'}) produces ?k=v on the outgoing URL."""
    captured: list[httpx.Request] = []
    with _mock_client(captured) as c:
        call(
            "POST",
            "/api/v1/admin/scope-assignments/123/end",
            "luc_sk_test_key",
            json={"reason": "DEPARTED"},
            params={"audit_label": "pillar_14:tenant-abc:departure"},
            client=c,
        )

    assert len(captured) == 1
    req = captured[0]
    parsed = urlparse(str(req.url))
    qs = parse_qs(parsed.query)
    assert parsed.path == "/api/v1/admin/scope-assignments/123/end"
    # parse_qs decodes percent-encoded values back to their original form,
    # so we assert against the original label, not its encoded form.
    assert qs == {"audit_label": ["pillar_14:tenant-abc:departure"]}


def test_params_value_with_reserved_chars_is_url_encoded() -> None:
    """A label containing `&`, `=`, `?`, space MUST be encoded so the
    server parses the original string back -- this is the regression
    guard for the Phase 2 Commit 14 inlined-querystring fragility."""
    captured: list[httpx.Request] = []
    hostile_label = "weird&label=with?reserved chars and:colon"
    with _mock_client(captured) as c:
        call(
            "POST",
            "/api/v1/admin/scope-assignments/123/end",
            "luc_sk_test_key",
            params={"audit_label": hostile_label},
            client=c,
        )

    assert len(captured) == 1
    req = captured[0]
    # The raw URL string must NOT contain unescaped `&`, `=`, `?`, or
    # space inside the audit_label value. parse_qs round-trips it cleanly
    # only if httpx encoded it correctly.
    parsed = urlparse(str(req.url))
    qs = parse_qs(parsed.query)
    assert qs == {"audit_label": [hostile_label]}, (
        f"hostile label was corrupted on the wire. parsed query = {qs!r}, "
        f"raw url = {req.url!r}"
    )


def test_bearer_header_present_with_params() -> None:
    """params= must not displace the Authorization header."""
    captured: list[httpx.Request] = []
    with _mock_client(captured) as c:
        call(
            "GET",
            "/api/v1/admin/anything",
            "luc_sk_some_key",
            params={"x": "y"},
            client=c,
        )

    assert len(captured) == 1
    req = captured[0]
    assert req.headers.get("authorization") == "Bearer luc_sk_some_key"


def test_no_params_omits_querystring_backwards_compat() -> None:
    """Callers that omit params= must behave exactly as before this commit:
    no querystring on the wire."""
    captured: list[httpx.Request] = []
    with _mock_client(captured) as c:
        call(
            "GET",
            "/api/v1/admin/anything",
            "luc_sk_some_key",
            client=c,
        )

    assert len(captured) == 1
    req = captured[0]
    parsed = urlparse(str(req.url))
    assert parsed.query == "", (
        f"expected empty querystring when params= is omitted, got "
        f"{parsed.query!r} from url {req.url!r}"
    )


def test_params_with_non_str_key_rejected_by_h_helper() -> None:
    """The bearer helper h() still rejects non-str keys loudly even when
    params= is present. Pin the existing contract."""
    captured: list[httpx.Request] = []
    with _mock_client(captured) as c, pytest.raises(TypeError):
        call(
            "GET",
            "/api/v1/admin/anything",
            12345,  # type: ignore[arg-type]
            params={"x": "y"},
            client=c,
        )
    assert captured == []
