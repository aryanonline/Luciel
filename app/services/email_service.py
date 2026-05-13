"""Step 30a: outbound transactional email.

Minimal v1 implementation:

  - Single function, ``send_magic_link_email``.
  - In production with no email provider configured, the function
    LOGS the email body (including the magic-link URL) at WARNING
    so an on-call engineer can manually deliver if needed. This is
    the deliberate v1 fallback while we land the surface; Step 32
    will swap the body of this function for a real SES/Postmark/etc.
    send.
  - The function is synchronous and bounded -- we want the webhook
    handler to return 2xx quickly to Stripe, so the email send must
    not block longer than a few hundred ms. A future provider
    integration should respect this contract.

The Step 32 plan is to introduce an ``EmailProvider`` ABC with
``ses``, ``postmark``, ``null`` (log-only) implementations and a
config flag. Until then this single function is the entire surface,
which keeps Step 30a closeable without coupling to a third email
vendor decision.
"""
from __future__ import annotations

import logging

from app.core.config import settings

logger = logging.getLogger(__name__)


SUBJECT_MAGIC_LINK = "Your Luciel login link"


def send_magic_link_email(*, to_email: str, magic_link_url: str, display_name: str | None = None) -> None:
    """Send (or log) a magic-link email.

    v1 behaviour:
      - Always logs the URL at WARNING with a stable marker so test
        harnesses can grep it back out.
      - No retries -- if a future provider raises, we let it bubble;
        the webhook handler MUST tolerate this by catching the
        exception, recording the audit row, and still returning 200
        to Stripe so the event is not redelivered indefinitely.
        (The next click on the marketing-site Account page can
        re-mint and re-send the email.)
    """
    salutation = display_name or to_email
    body = (
        f"Hi {salutation},\n\n"
        f"Thanks for subscribing to Luciel. Click the link below to "
        f"finish setting up your account:\n\n"
        f"  {magic_link_url}\n\n"
        f"This link expires in {settings.magic_link_ttl_hours} hours. "
        f"If you did not initiate this, you can ignore this email.\n\n"
        f"-- The Luciel team\n"
    )

    # The stable marker prefix `[magic-link-email]` lets the e2e
    # harness (tests/e2e/step_30a_live_e2e.py) assert the URL was
    # produced without needing a real mailbox to read from.
    logger.warning(
        "[magic-link-email] from=%s to=%s subject=%r url=%s\n%s",
        settings.from_email,
        to_email,
        SUBJECT_MAGIC_LINK,
        magic_link_url,
        body,
    )
