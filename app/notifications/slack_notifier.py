"""Slack NotificationAdapter — POSTs to an Incoming Webhook URL (§3.5.1 slack).

Enterprise-only. The webhook URL is stored in Secrets Manager (outbound_webhook
style), resolved via the existing SecretStore integration. When
CHANNELS_LIVE_PROVISIONING_ENABLED is False, records the full routing+attempt
decision in DRY-RUN. Never uses a bot token.
"""
from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error

from app.notifications.base import NotificationAdapter, NotificationResult

logger = logging.getLogger(__name__)


class SlackNotificationAdapter(NotificationAdapter):
    """POST an escalation message to a Slack Incoming Webhook (§3.5.1 slack).

    ``to`` is the webhook URL (resolved from the escalation_config or Secrets
    Manager). Enterprise-only; gated behind
    settings.channels_live_provisioning_enabled.
    """

    channel = "slack"

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
                "[escalation-slack] dry-run: no webhook URL resolved "
                "session=%s signal=%s",
                session_id, signal,
            )
            return NotificationResult(
                channel=self.channel,
                to=to,
                dry_run=True,
                extra={"reason": "no_webhook_url"},
            )

        if not settings.channels_live_provisioning_enabled:
            logger.info(
                "[escalation-slack] (live switch off) dry-run "
                "signal=%s session=%s",
                signal, session_id,
            )
            return NotificationResult(
                channel=self.channel,
                to=to,
                dry_run=True,
            )

        # Live path — POST to the Incoming Webhook URL.
        payload = {
            "text": f"*{subject}*\n{body}",
            "mrkdwn": True,
        }
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                to,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:  # pragma: no cover
                status = resp.status  # pragma: no cover
            logger.info(  # pragma: no cover
                "[escalation-slack] posted to webhook signal=%s session=%s status=%s",
                signal, session_id, status,
            )
            return NotificationResult(  # pragma: no cover
                channel=self.channel,
                to=to,
                sent=True,
                provider_id=f"slack_webhook_http_{status}",
            )
        except urllib.error.HTTPError as exc:  # pragma: no cover
            logger.warning(
                "[escalation-slack] webhook HTTP error signal=%s session=%s "
                "status=%s reason=%s",
                signal, session_id, exc.code, exc.reason,
            )
            return NotificationResult(
                channel=self.channel,
                to=to,
                sent=False,
                error=f"HTTPError:{exc.code}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[escalation-slack] webhook FAILED signal=%s session=%s error=%s",
                signal, session_id, exc,
            )
            return NotificationResult(
                channel=self.channel,
                to=to,
                sent=False,
                error=f"{type(exc).__name__}: {exc}",
            )
