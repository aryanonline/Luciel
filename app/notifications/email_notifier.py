"""Email NotificationAdapter — wraps app.services.email_service (SES path).

Reuses the existing SES stack (send_budget_alert_email pattern). Does NOT
build a second email transport. Gated behind LUCIEL_EMAIL_TRANSPORT and
CHANNELS_LIVE_PROVISIONING_ENABLED — when either path says no real send, the
adapter records the routing+attempt decision in DRY-RUN (consistent with
the existing escalation_routing convention).
"""
from __future__ import annotations

import logging
import os

from app.notifications.base import NotificationAdapter, NotificationResult

logger = logging.getLogger(__name__)

_LOG_TRANSPORT = "log"
_SES_TRANSPORT = "ses"


def _email_transport() -> str:
    raw = (os.getenv("LUCIEL_EMAIL_TRANSPORT") or _SES_TRANSPORT).strip().lower()
    if raw not in {_SES_TRANSPORT, _LOG_TRANSPORT}:
        return _LOG_TRANSPORT
    return raw


class EmailNotificationAdapter(NotificationAdapter):
    """Send an escalation email via the existing SES stack (§3.5.1 email).

    Wraps the raw SES sesv2 send_email call mirroring
    send_budget_alert_email. The log-only transport (dev / CI) logs the
    body and returns dry_run=True without calling boto3.
    """

    channel = "email"

    def send(
        self,
        *,
        to: str | None,
        subject: str,
        body: str,
        signal: str,
        session_id: str,
    ) -> NotificationResult:
        if not to:
            logger.info(
                "[escalation-email] dry-run: no recipient resolved "
                "session=%s signal=%s",
                session_id, signal,
            )
            return NotificationResult(
                channel=self.channel,
                to=to,
                dry_run=True,
                extra={"reason": "no_recipient"},
            )

        transport = _email_transport()
        if transport == _LOG_TRANSPORT:
            logger.warning(
                "[escalation-email] (log-only transport) to=%s subject=%r "
                "signal=%s session=%s",
                to, subject, signal, session_id,
            )
            return NotificationResult(
                channel=self.channel,
                to=to,
                dry_run=True,
            )

        # Real SES delivery — same as send_budget_alert_email.
        try:
            import boto3
            from botocore.exceptions import BotoCoreError, ClientError
        except ImportError as exc:
            logger.exception("[escalation-email] boto3 unavailable; cannot send")
            return NotificationResult(
                channel=self.channel,
                to=to,
                sent=False,
                error=f"boto3_unavailable: {exc}",
            )

        import os as _os
        from app.core.config import settings

        region = (
            _os.getenv("SES_REGION")
            or _os.getenv("AWS_REGION")
            or _os.getenv("AWS_DEFAULT_REGION")
            or "ca-central-1"
        )
        try:
            client = boto3.client("sesv2", region_name=region)
            response = client.send_email(
                FromEmailAddress=settings.from_email,
                Destination={"ToAddresses": [to]},
                ReplyToAddresses=[settings.ses_reply_to_address],
                ConfigurationSetName=settings.ses_configuration_set_name,
                Content={
                    "Simple": {
                        "Subject": {"Data": subject, "Charset": "UTF-8"},
                        "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                    },
                },
            )
            message_id = response.get("MessageId", "<unknown>")
            logger.info(
                "[escalation-email] sent via SES to=%s signal=%s session=%s "
                "message_id=%s",
                to, signal, session_id, message_id,
            )
            return NotificationResult(
                channel=self.channel,
                to=to,
                sent=True,
                provider_id=message_id,
            )
        except (ClientError, BotoCoreError) as exc:
            logger.warning(
                "[escalation-email] SES send FAILED to=%s signal=%s "
                "session=%s error=%s",
                to, signal, session_id, exc,
            )
            return NotificationResult(
                channel=self.channel,
                to=to,
                sent=False,
                error=f"{type(exc).__name__}: {exc}",
            )
