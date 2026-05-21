"""Arc 2 — Backend Code Hygiene (2026-05-20): token-URL redaction contract.

Drift: ``D-set-password-token-logged-plaintext-2026-05-17``.

Before Arc 2, ``app/services/email_service.py`` emitted the full URL
(including the single-use JWT in the ``?token=`` query) on six log lines
across ``send_magic_link_email`` and ``send_welcome_set_password_email``.
That meant any operator with ``logs:GetLogEvents`` on
``/aws/ecs/luciel-backend`` could harvest unredeemed tokens straight out
of CloudWatch and impersonate the recipient until the token TTL elapsed
or it was redeemed.

Arc 2 P1 fix introduces two helpers at module scope --
``_redact_token_url`` (strip the ``token`` query parameter) and
``_redact_body`` (rewrite token-bearing URLs inside the email body) --
and routes every log emitter through them before
``logger.info``/``logger.warning`` sees the URL or body.

This file is the **invariant pin**: if a future edit re-introduces a
raw token URL on a log line, this test fails and the fix is caught in
CI long before the regression reaches production. Two complementary
assertion families:

  1. **Source-grep** (always runs, no runtime deps) -- proves the six
     emitter sites still call the redaction helpers, and proves the
     helpers themselves are still defined at module scope.
  2. **Functional** (skipped gracefully if ``email_service`` cannot
     import in the harness) -- imports the helpers and exercises them
     against representative URL/body shapes to prove the runtime
     behaviour matches the contract.

Pairs with the CloudWatch backlog audit deferred to Arc 3 (paired
prod-touch arc) -- once the historical log buffer is scrubbed and the
ingest-side fix is pinned by this test, the drift is fully closed.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
EMAIL_SERVICE_PATH = REPO_ROOT / "app" / "services" / "email_service.py"


# ---------------------------------------------------------------------
# 1. Source-grep contract -- helpers exist + every emitter uses them
# ---------------------------------------------------------------------

class TestEmailServiceRedactionSource:
    """The source file must define the redaction helpers and route every
    token-bearing log emitter through them.

    These assertions are pure ``read_text`` greps -- they do not require
    boto3, pydantic_settings, or any other runtime dependency, so they
    run in every CI configuration including the bare ``pytest`` smoke
    on the sandbox.
    """

    def setup_method(self) -> None:
        self.src = EMAIL_SERVICE_PATH.read_text()

    def test_redact_token_url_helper_defined(self) -> None:
        assert "def _redact_token_url(" in self.src, (
            "Arc 2 P1 helper `_redact_token_url` is missing -- "
            "drift D-set-password-token-logged-plaintext would regress."
        )

    def test_redact_body_helper_defined(self) -> None:
        assert "def _redact_body(" in self.src, (
            "Arc 2 P1 helper `_redact_body` is missing -- "
            "the SES-failure log path would re-expose tokens in body."
        )

    def test_no_bare_magic_link_url_log_emitter(self) -> None:
        """No log call inside ``send_magic_link_email`` may pass the
        raw ``magic_link_url`` -- it must always be wrapped in
        ``_redact_token_url(...)``.

        We scan every logger.{info,warning,exception,error} statement
        in the magic-link function and assert that any occurrence of
        ``magic_link_url`` is wrapped.
        """
        # Pull just the body of send_magic_link_email.
        m = re.search(
            r"def send_magic_link_email\(.*?\n(?=def |\Z)",
            self.src,
            flags=re.DOTALL,
        )
        assert m, "send_magic_link_email definition not found"
        fn_body = m.group(0)
        # Find every logger.* call. A logger call may span multiple lines.
        for log_call in re.finditer(
            r"logger\.(?:info|warning|exception|error)\((.*?)\)\s*(?:\n|$)",
            fn_body,
            flags=re.DOTALL,
        ):
            payload = log_call.group(1)
            if "magic_link_url" not in payload:
                continue
            assert "_redact_token_url(magic_link_url)" in payload, (
                "send_magic_link_email logger call still passes raw "
                "magic_link_url -- wrap with _redact_token_url():\n"
                f"  {payload[:200]}..."
            )

    def test_no_bare_set_password_url_log_emitter(self) -> None:
        """Same invariant for ``send_welcome_set_password_email`` --
        every log emitter must wrap ``set_password_url`` in
        ``_redact_token_url``.
        """
        m = re.search(
            r"def send_welcome_set_password_email\(.*?\n(?=def |\Z)",
            self.src,
            flags=re.DOTALL,
        )
        assert m, "send_welcome_set_password_email definition not found"
        fn_body = m.group(0)
        for log_call in re.finditer(
            r"logger\.(?:info|warning|exception|error)\((.*?)\)\s*(?:\n|$)",
            fn_body,
            flags=re.DOTALL,
        ):
            payload = log_call.group(1)
            if "set_password_url" not in payload:
                continue
            assert "_redact_token_url(set_password_url)" in payload, (
                "send_welcome_set_password_email logger call still passes "
                "raw set_password_url -- wrap with _redact_token_url():\n"
                f"  {payload[:200]}..."
            )

    def test_ses_failure_path_redacts_body(self) -> None:
        """The SES-failure log paths (both functions) log the full
        email body so on-call can manually relay -- the body must
        flow through ``_redact_body`` so the in-body URL is redacted
        too.
        """
        # Two SES-failure log warnings exist -- one per function.
        # Each must reference _redact_body.
        ses_failure_blocks = re.findall(
            r"logger\.warning\(\s*\"\[(?:magic-link-email|welcome-set-password-email)\] "
            r"SES send FAILED.*?\)\s*(?:\n|$)",
            self.src,
            flags=re.DOTALL,
        )
        assert len(ses_failure_blocks) >= 2, (
            "Expected at least 2 SES-failure log blocks (one per email "
            f"function); found {len(ses_failure_blocks)}"
        )
        for block in ses_failure_blocks:
            assert "_redact_body(body)" in block, (
                "SES-failure log block does not pass body through "
                f"_redact_body -- tokens may leak via body:\n  {block[:300]}"
            )


# ---------------------------------------------------------------------
# 2. Functional contract -- helpers actually redact at runtime
# ---------------------------------------------------------------------

# Try to import the helpers. If the harness can't satisfy
# email_service's transitive imports (boto3 isn't required because of
# the lazy import, but pydantic_settings is), skip cleanly rather than
# regressing the source-grep coverage above.
try:
    from app.services.email_service import _redact_body, _redact_token_url

    _IMPORT_OK = True
except Exception as exc:  # pragma: no cover - depends on harness env
    _IMPORT_OK = False
    _IMPORT_ERR = exc


@pytest.mark.skipif(
    not _IMPORT_OK,
    reason="email_service not importable in this harness "
    "(source-grep coverage in TestEmailServiceRedactionSource still applies)",
)
class TestEmailServiceRedactionFunctional:
    """Exercise the helpers end-to-end against representative shapes."""

    def test_redact_strips_token_param(self) -> None:
        url = "https://vantagemind.ai/auth/set-password?token=eyJ.abc.xyz"
        out = _redact_token_url(url)
        assert "eyJ.abc.xyz" not in out, (
            "JWT payload must not survive _redact_token_url"
        )
        # The exact placeholder is urlencode'd (<redacted> -> %3Credacted%3E)
        # which is cosmetic -- the security contract is "no JWT".
        assert "token=" in out  # the key remains, only the value is wiped

    def test_redact_preserves_non_token_params(self) -> None:
        url = (
            "https://vantagemind.ai/auth/set-password"
            "?token=eyJ.abc.xyz&purpose=invite&utm_source=email"
        )
        out = _redact_token_url(url)
        assert "eyJ.abc.xyz" not in out
        assert "purpose=invite" in out
        assert "utm_source=email" in out

    def test_redact_noop_when_no_token(self) -> None:
        url = "https://vantagemind.ai/dashboard?purpose=invite"
        assert _redact_token_url(url) == url

    def test_redact_noop_when_no_query(self) -> None:
        url = "https://vantagemind.ai/dashboard"
        assert _redact_token_url(url) == url

    def test_redact_malformed_input_safe(self) -> None:
        # Helper must never raise -- a logger call that explodes would
        # take down the whole magic-link send path.
        out = _redact_token_url("not a url at all")
        assert isinstance(out, str)

    def test_redact_body_rewrites_in_place(self) -> None:
        body = (
            "Hi there,\n\nClick "
            "https://vantagemind.ai/auth/set-password?token=eyJ.SECRET.xyz"
            " to finish setup.\n\nThanks,\nVantageMind"
        )
        out = _redact_body(body)
        assert "eyJ.SECRET.xyz" not in out, (
            "Body redaction must strip the JWT payload"
        )
        assert "Hi there" in out  # surrounding copy preserved
        assert "Thanks,\nVantageMind" in out

    def test_redact_body_handles_multiple_urls(self) -> None:
        body = (
            "Link A https://x.ai/a?token=AAA and link B "
            "https://y.ai/b?token=BBB end."
        )
        out = _redact_body(body)
        assert "AAA" not in out
        assert "BBB" not in out

    def test_redact_body_noop_without_token(self) -> None:
        body = "Plain text with no URLs at all."
        assert _redact_body(body) == body
