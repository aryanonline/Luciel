"""SMS NotificationAdapter — wraps the Twilio path used by sms_adapter.

Reuses the existing Twilio REST client pattern from
app.channels.sms_adapter. When CHANNELS_LIVE_PROVISIONING_ENABLED is False,
returns a dry-run result with a synthetic sid (matching sms_adapter.send).
Never builds a second SMS stack.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from app.notifications.base import NotificationAdapter, NotificationResult

logger = logging.getLogger(__name__)


class SmsNotificationAdapter(NotificationAdapter):
    """Send an escalation SMS via Twilio REST (§3.5.1 sms, Pro+).

    Gated behind settings.channels_live_provisioning_enabled. When the switch
    is off, a synthetic sid is logged and dry_run=True is returned (same
    posture as sms_adapter.send). When the switch is on and Twilio credentials
    are present, a real message is dispatched.
    """

    channel = "sms"

    def send(
        self,
        *,
        to: str | None,
        subject: str,
        body: str,
        signal: str,
        session_id: str,
    ) -> NotificationResult:
        from app.core.config import settings

        if not to:
            logger.info(
                "[escalation-sms] dry-run: no recipient resolved "
                "session=%s signal=%s",
                session_id, signal,
            )
            return NotificationResult(
                channel=self.channel,
                to=to,
                dry_run=True,
                extra={"reason": "no_recipient"},
            )

        if not settings.channels_live_provisioning_enabled:
            synthetic_sid = f"SMfake{uuid.uuid4().hex[:24]}"
            logger.info(
                "[escalation-sms] (live switch off) dry-run to=%s "
                "signal=%s session=%s synthetic_sid=%s",
                to, signal, session_id, synthetic_sid,
            )
            return NotificationResult(
                channel=self.channel,
                to=to,
                dry_run=True,
                provider_id=synthetic_sid,
            )

        # Live path — Twilio REST.
        if not (settings.twilio_account_sid and settings.twilio_auth_token):
            logger.warning(
                "[escalation-sms] Twilio credentials unset; cannot send live SMS "
                "to=%s signal=%s session=%s",
                to, signal, session_id,
            )
            return NotificationResult(
                channel=self.channel,
                to=to,
                sent=False,
                error="twilio_credentials_unset",
            )

        try:
            from twilio.rest import Client  # pragma: no cover - live only

            client = Client(  # pragma: no cover - live only
                settings.twilio_account_sid, settings.twilio_auth_token
            )
            kwargs = {  # pragma: no cover - live only
                "to": to,
                "body": body,
            }
            if settings.twilio_messaging_service_sid:  # pragma: no cover
                kwargs["messaging_service_sid"] = settings.twilio_messaging_service_sid
            else:  # pragma: no cover
                from_num = getattr(settings, "twilio_from_number", None)
                if from_num:  # pragma: no cover
                    kwargs["from_"] = from_num

            sent = client.messages.create(**kwargs)  # pragma: no cover
            logger.info(  # pragma: no cover
                "[escalation-sms] sent via Twilio to=%s signal=%s session=%s sid=%s",
                to, signal, session_id, sent.sid,
            )
            return NotificationResult(  # pragma: no cover
                channel=self.channel,
                to=to,
                sent=True,
                provider_id=sent.sid,
            )
        except Exception as exc:  # noqa: BLE001  # pragma: no cover
            logger.warning(
                "[escalation-sms] Twilio send FAILED to=%s signal=%s "
                "session=%s error=%s",
                to, signal, session_id, exc,
            )
            return NotificationResult(
                channel=self.channel,
                to=to,
                sent=False,
                error=f"{type(exc).__name__}: {exc}",
            )
