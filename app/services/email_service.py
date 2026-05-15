"""Step 30a / 30a.2: outbound transactional email.

v1 behaviour preserved:
  - Single function, ``send_magic_link_email``.
  - The function is synchronous and bounded -- the webhook handler must
    return 2xx quickly to Stripe, so the email send must not block longer
    than a few hundred ms. Boto3 SES ``send_email`` is RTT-bounded by the
    region's SES endpoint (~50-200ms from a co-located task), well inside
    that contract. SES failure modes (throttling, throttle-then-recover,
    region failure) are surfaced as :class:`MagicLinkError` so the caller
    can record an audit row and either fail-loud at the API surface or
    swallow-and-200 at the webhook surface (see
    ``app/services/billing_webhook_service.py`` for the swallow contract).

Step 30a.2 wiring:
  - Real SES delivery via ``boto3.client('sesv2')``. Region is read from
    the ``SES_REGION`` environment variable, defaulting to the AWS region
    the task is running in (``AWS_REGION``), which is ``ca-central-1`` in
    production. The task's IAM role (``luciel-ecs-web-role``) carries the
    ``LucielSESSendEmail`` inline policy scoped to the verified
    ``vantagemind.ai`` identity ARN; no credentials are read here.
  - Backwards-compatible fallback: when ``LUCIEL_EMAIL_TRANSPORT=log``
    is set (the local-dev convention), the function logs the body
    instead of sending. The stable ``[magic-link-email]`` log marker is
    emitted on **both** paths so the e2e harness (and the contract test
    in ``tests/api/test_step30a_billing_shape.py``) keeps passing.
  - Future ``EmailProvider`` ABC remains the right direction for adding a
    second provider (Postmark/Resend/etc.); this commit lands the SES
    integration directly to close ``D-email-service-log-only-no-real-
    delivery-2026-05-14`` without taking on that abstraction in the same
    revision. The single read site for transport selection is the
    ``_transport()`` helper below, which is the seam for that future
    refactor.
"""
from __future__ import annotations

import logging
import os
from typing import Final

from app.core.config import settings

logger = logging.getLogger(__name__)


SUBJECT_MAGIC_LINK: Final[str] = "Your VantageMind login link"
_LOG_TRANSPORT: Final[str] = "log"
_SES_TRANSPORT: Final[str] = "ses"


class MagicLinkError(RuntimeError):
    """Raised when the magic-link email cannot be delivered.

    Callers in the webhook path catch this and record an audit row while
    still returning 200 to Stripe; callers in the synchronous API path
    surface it as a 5xx so the caller's UI can show a retry affordance.
    """


def _transport() -> str:
    """Return the configured email transport: ``ses`` or ``log``."""
    raw = (os.getenv("LUCIEL_EMAIL_TRANSPORT") or _SES_TRANSPORT).strip().lower()
    if raw not in {_SES_TRANSPORT, _LOG_TRANSPORT}:
        # Unknown value -> fail closed to log so we never silently mis-route.
        logger.warning(
            "[magic-link-email] unknown LUCIEL_EMAIL_TRANSPORT=%r; falling back to %r",
            raw,
            _LOG_TRANSPORT,
        )
        return _LOG_TRANSPORT
    return raw


def _build_body(to_email: str, magic_link_url: str, display_name: str | None) -> str:
    salutation = display_name or to_email
    return (
        f"Hi {salutation},\n\n"
        f"Thanks for signing up for VantageMind. Click the link below to "
        f"finish setting up your account:\n\n"
        f"  {magic_link_url}\n\n"
        f"This link expires in {settings.magic_link_ttl_hours} hours. "
        f"If you did not initiate this, you can ignore this email.\n\n"
        f"-- The VantageMind team\n"
    )


def send_magic_link_email(
    *,
    to_email: str,
    magic_link_url: str,
    display_name: str | None = None,
) -> None:
    """Send (or log) a magic-link email.

    Behaviour:
      - With ``LUCIEL_EMAIL_TRANSPORT=ses`` (production default): sends
        through Amazon SES v2 ``send_email``. On any SES failure, logs the
        full body at WARNING (so the on-call engineer can manually relay)
        AND raises :class:`MagicLinkError` so the caller's audit row is
        accurate. The ``[magic-link-email]`` marker is emitted on both
        the success log line and the failure log line so the e2e harness
        keeps working regardless of the delivery outcome.
      - With ``LUCIEL_EMAIL_TRANSPORT=log`` (local dev): logs the body
        only. Useful for offline development and for the existing
        contract test, which greps the source for the marker string.

    The stable marker prefix `[magic-link-email]` lets the e2e
    harness (tests/e2e/step_30a_live_e2e.py) assert the URL was
    produced without needing a real mailbox to read from.
    """
    body = _build_body(to_email, magic_link_url, display_name)
    transport = _transport()

    if transport == _LOG_TRANSPORT:
        logger.warning(
            "[magic-link-email] (log-only transport) from=%s to=%s subject=%r url=%s\n%s",
            settings.from_email,
            to_email,
            SUBJECT_MAGIC_LINK,
            magic_link_url,
            body,
        )
        return

    # Real SES delivery. Boto3 is imported lazily so the log-only path
    # (used in unit tests and local dev) does not require boto3 to be
    # importable; the production task always has it installed.
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:  # pragma: no cover - prod always has boto3
        logger.exception("[magic-link-email] boto3 unavailable; cannot send")
        raise MagicLinkError("boto3 is not installed") from exc

    region = (
        os.getenv("SES_REGION")
        or os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or "ca-central-1"
    )

    try:
        client = boto3.client("sesv2", region_name=region)
        response = client.send_email(
            FromEmailAddress=settings.from_email,
            Destination={"ToAddresses": [to_email]},
            Content={
                "Simple": {
                    "Subject": {"Data": SUBJECT_MAGIC_LINK, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                },
            },
        )
        message_id = response.get("MessageId", "<unknown>")
        logger.info(
            "[magic-link-email] sent via SES from=%s to=%s subject=%r url=%s message_id=%s",
            settings.from_email,
            to_email,
            SUBJECT_MAGIC_LINK,
            magic_link_url,
            message_id,
        )
    except (ClientError, BotoCoreError) as exc:
        # Log the full body so on-call can manually relay if SES is down,
        # then raise so the caller's audit row records the failure.
        logger.warning(
            "[magic-link-email] SES send FAILED from=%s to=%s subject=%r url=%s error=%s\n%s",
            settings.from_email,
            to_email,
            SUBJECT_MAGIC_LINK,
            magic_link_url,
            exc,
            body,
        )
        raise MagicLinkError(f"SES send_email failed: {exc}") from exc
