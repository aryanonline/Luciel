"""Arc 8 Work-Unit 5 -- hCaptcha verification service.

D-free-tier-captcha-missing-2026-05-22 resolution. The Free-tier
self-serve signup endpoint at ``POST /api/v1/billing/signup-free`` is
public (unauthenticated by design -- nobody holds a credential yet at
the moment of signup), which means it is a free SES-quota drain and a
free database-row drain if we don't gate bot traffic. hCaptcha is the
gate.

This module is the single canonical surface for hCaptcha verification
across the backend. Today only ``/billing/signup-free`` consumes it; a
future Free-tier login throttle or password-reset throttle would
import the same ``verify_captcha`` callable.

Design contract:

  * Empty ``settings.hcaptcha_secret_key`` -> raise
    ``CaptchaNotConfiguredError``. The route handler catches that and
    returns HTTP 501 (boot-safe / dev-safe pattern, never 500).
  * Empty / missing ``token`` argument -> raise
    ``CaptchaInvalidError``. The route catches that and returns
    HTTP 422 (validation failure).
  * Network failure / non-2xx from hCaptcha -> raise
    ``CaptchaInvalidError`` with the upstream error code captured for
    log forensics. Fails closed (no token == no signup) per
    six-pillar security.
  * Upstream JSON with ``success=false`` -> raise
    ``CaptchaInvalidError`` carrying the ``error-codes`` list.
  * Upstream JSON with ``success=true`` -> return a small dict carrying
    the verified payload metadata (hostname, challenge_ts, optional
    credit boolean) for audit logging at the route level.

No PII is logged from this module. The token itself is treated as a
short-lived bearer credential and never echoed into logs (the upstream
``success=false`` path logs only the hCaptcha error-codes list).

Reference: https://docs.hcaptcha.com/#verify-the-user-response-server-side
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class CaptchaError(Exception):
    """Base class for hCaptcha verification failures."""


class CaptchaNotConfiguredError(CaptchaError):
    """Raised when ``settings.hcaptcha_secret_key`` is empty.

    Mapped to HTTP 501 at the route layer (boot-safe pattern -- a
    backend without hCaptcha configured boots fine; only the
    ``/signup-free`` route is unavailable until the SSM slot lands).
    """


class CaptchaInvalidError(CaptchaError):
    """Raised when the upstream returns ``success=false`` or the
    request itself never reached hCaptcha (network / shape error).

    Mapped to HTTP 422 at the route layer. The ``error_codes`` and
    ``message`` attributes are safe to surface in the response body
    (hCaptcha's error codes are public per their docs).
    """

    def __init__(
        self,
        message: str,
        error_codes: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_codes = list(error_codes or [])


async def verify_captcha(
    token: str,
    *,
    remote_ip: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Verify an hCaptcha response token against the upstream API.

    Args:
      token: the ``h-captcha-response`` value that the front-end
             widget submitted along with the signup form. The
             ``CaptchaInvalidError`` path covers empty / whitespace-
             only tokens before any network call is made.
      remote_ip: the buyer's IP address. Passed to hCaptcha as the
             optional ``remoteip`` parameter; hCaptcha uses it as one
             input to its risk score. Not required, but improves
             score quality. Caller is responsible for extracting the
             trustworthy client IP from ``X-Forwarded-For`` /
             ``request.client.host`` per its perimeter trust model.
      http_client: optional injected httpx client. Tests pass a
             mock-transport client here; production passes ``None``
             and we construct a short-lived client with a 5s timeout.

    Returns:
      A dict carrying the verified payload metadata::

        {
          "success": True,
          "challenge_ts": "2026-05-22T15:07:00.000Z",  # iso8601
          "hostname": "www.vantagemind.ai",
          "credit": False,  # optional; hCaptcha Enterprise field
        }

      Callers should NOT make business decisions on the returned
      dict's contents beyond "verification succeeded"; the dict is
      preserved verbatim from hCaptcha for audit logging.

    Raises:
      CaptchaNotConfiguredError: ``settings.hcaptcha_secret_key``
        is empty. Route handler returns 501.
      CaptchaInvalidError: token is empty, network call failed,
        upstream returned non-2xx, or upstream returned
        ``success=false``. Route handler returns 422.
    """
    if not settings.hcaptcha_secret_key:
        raise CaptchaNotConfiguredError(
            "hCaptcha is not configured on this backend "
            "(settings.hcaptcha_secret_key is empty). Free-tier "
            "signup is unavailable until the SSM slot is populated."
        )

    if not token or not token.strip():
        raise CaptchaInvalidError(
            "Captcha token is missing or empty.",
            error_codes=["missing-input-response"],
        )

    payload: dict[str, str] = {
        "secret": settings.hcaptcha_secret_key,
        "response": token,
    }
    if remote_ip:
        payload["remoteip"] = remote_ip

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=5.0)
    try:
        try:
            response = await client.post(
                settings.hcaptcha_verify_url,
                data=payload,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "hcaptcha.verify.network_error type=%s",
                type(exc).__name__,
            )
            raise CaptchaInvalidError(
                "Captcha verification request failed.",
                error_codes=["network-error"],
            ) from exc

        if response.status_code != 200:
            logger.warning(
                "hcaptcha.verify.non_200 status=%s",
                response.status_code,
            )
            raise CaptchaInvalidError(
                f"Captcha verification returned HTTP {response.status_code}.",
                error_codes=[f"upstream-{response.status_code}"],
            )

        try:
            body = response.json()
        except ValueError as exc:
            logger.warning("hcaptcha.verify.bad_json")
            raise CaptchaInvalidError(
                "Captcha verification returned non-JSON body.",
                error_codes=["bad-json"],
            ) from exc
    finally:
        if owns_client:
            await client.aclose()

    if not isinstance(body, dict):
        raise CaptchaInvalidError(
            "Captcha verification returned non-object body.",
            error_codes=["bad-shape"],
        )

    if not body.get("success"):
        error_codes = body.get("error-codes") or []
        if not isinstance(error_codes, list):
            error_codes = ["bad-error-codes-shape"]
        logger.info("hcaptcha.verify.failed error_codes=%s", error_codes)
        raise CaptchaInvalidError(
            "Captcha verification failed.",
            error_codes=[str(c) for c in error_codes],
        )

    # Successful verification. Strip the secret-bearing keys (none
    # should be present, but be paranoid) and return a clean copy.
    return {
        "success": True,
        "challenge_ts": body.get("challenge_ts"),
        "hostname": body.get("hostname"),
        "credit": body.get("credit"),
    }
