"""Arc 13 D3 — EmailChannelAdapter (store-and-forward over SES/SNS).

The email surface is STORE-AND-FORWARD: an inbound customer email
arrives as an SNS Notification (SES inbound → SNS topic → our webhook),
and the reply is dispatched as a separate outbound SES ``send_email``
call that returns a provider message id. This adapter expresses the
``verify_inbound`` / ``receive`` / ``send`` contract over that wire.

Trust gate (verify_inbound, BEFORE any routing/parsing)
-------------------------------------------------------
Mirrors the SES-feedback SNS trust gate in ``app/api/v1/ses_events.py``
— the SAME two-check defence, reused so the inbound-mail path is no
weaker than the feedback path:

  1. ``TopicArn`` must equal ``settings.ses_inbound_topic_arn`` (the
     inbound topic we explicitly subscribe). Mismatch → forged →
     :class:`SignatureVerificationError`.
  2. ``SigningCertURL`` must be HTTPS under ``*.amazonaws.com`` (reusing
     :func:`app.api.v1.ses_events._is_amazonaws_url`). Bad host →
     :class:`SignatureVerificationError`.

Only once authenticity holds do we resolve routing: the destination
address (the inbound recipient) must map to a live ``ChannelRoute``
(channel='email'); otherwise :class:`UnresolvableInboundError`.

Routing model
-------------
A tenant's inbound mailbox is a ``ChannelRoute`` row with
``channel='email'`` and ``route_value`` = the recipient address Luciel
listens on for that Instance. The sender address is the customer's
identity claim (resolved to a session by the webhook handler via
``SessionService.create_session_with_identity`` with
``issuing_adapter='email_gateway'``), so the same customer emailing
twice continues one conversation.

Outbound
--------
``send`` dispatches the reply via SES (sesv2 ``send_email``) and returns
a :class:`DeliveryReceipt` carrying the provider message id. When the
email transport is the dev/CI log transport (``LUCIEL_EMAIL_TRANSPORT``
!= 'ses') no network call is made and a synthetic id is returned — so
the dev/test path never touches SES, mirroring the live-switch
discipline on the SMS side.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any

from sqlalchemy.orm import Session

from app.api.v1.ses_events import _is_amazonaws_url
from app.channels.base import (
    DeliveryReceipt,
    InboundMessage,
    InstanceContext,
    OutboundMessage,
    SignatureVerificationError,
    UnresolvableInboundError,
)
from app.core.config import settings
from app.models.channel_route import CHANNEL_EMAIL, ChannelRoute
from app.services.email_service import _SES_TRANSPORT, _transport

logger = logging.getLogger(__name__)

CHANNEL_EMAIL_ADAPTER = "email"

# SNS message-type constants (subset we accept on the inbound mail path).
_SNS_TYPE_NOTIFICATION = "Notification"


class EmailChannelAdapter:
    """Store-and-forward channel adapter for inbound/outbound email.

    Constructed with a DB :class:`Session` so ``verify_inbound`` can
    resolve the recipient address to a live :class:`ChannelRoute`.
    ``raw`` for this adapter is the parsed SNS message ``dict`` (the
    webhook route has already ``json.loads``-ed the request body).
    """

    channel: str = CHANNEL_EMAIL_ADAPTER

    def __init__(self, db: Session) -> None:
        self.db = db

    # -----------------------------------------------------------------
    # verify_inbound — trust gate FIRST, then routing.
    # -----------------------------------------------------------------

    def verify_inbound(self, raw: Any) -> InstanceContext:
        if not isinstance(raw, dict):
            raise SignatureVerificationError(
                "Email inbound payload is not an SNS message dict."
            )

        # --- Check 1: TopicArn allowlist. ---
        expected_topic = settings.ses_inbound_topic_arn
        actual_topic = raw.get("TopicArn")
        if expected_topic and actual_topic != expected_topic:
            logger.warning(
                "email_adapter: SECURITY: inbound TopicArn mismatch "
                "(expected=%s actual=%s MessageId=%s)",
                expected_topic,
                actual_topic,
                raw.get("MessageId"),
            )
            raise SignatureVerificationError(
                "Inbound email TopicArn is not the configured inbound topic."
            )

        # --- Check 2: SigningCertURL host. ---
        signing_cert_url = raw.get("SigningCertURL") or raw.get("SigningCertUrl")
        if signing_cert_url and not _is_amazonaws_url(signing_cert_url):
            logger.warning(
                "email_adapter: SECURITY: inbound SigningCertURL host not "
                "amazonaws.com: %s (MessageId=%s)",
                signing_cert_url,
                raw.get("MessageId"),
            )
            raise SignatureVerificationError(
                "Inbound email SigningCertURL host is not amazonaws.com."
            )

        # --- Authenticity established → resolve routing. ---
        recipient = self._extract_recipient(raw)
        if not recipient:
            raise UnresolvableInboundError(
                "Inbound email payload carries no resolvable recipient address."
            )

        route = (
            self.db.query(ChannelRoute)
            .filter(
                ChannelRoute.channel == CHANNEL_EMAIL,
                ChannelRoute.route_value == recipient,
                ChannelRoute.revoked_at.is_(None),
            )
            .first()
        )
        if route is None:
            raise UnresolvableInboundError(
                f"Inbound email recipient {recipient!r} maps to no live "
                "email ChannelRoute."
            )

        return InstanceContext(
            admin_id=route.admin_id,
            instance_id=route.luciel_instance_id,
            session_id=None,
        )

    # -----------------------------------------------------------------
    # receive — canonicalise an already-verified payload.
    # -----------------------------------------------------------------

    def receive(self, raw: Any) -> InboundMessage:
        if not isinstance(raw, dict):
            raise UnresolvableInboundError(
                "Email inbound payload is not an SNS message dict."
            )

        recipient = self._extract_recipient(raw)
        route = (
            self.db.query(ChannelRoute)
            .filter(
                ChannelRoute.channel == CHANNEL_EMAIL,
                ChannelRoute.route_value == recipient,
                ChannelRoute.revoked_at.is_(None),
            )
            .first()
        )
        if route is None:
            raise UnresolvableInboundError(
                f"Inbound email recipient {recipient!r} maps to no live "
                "email ChannelRoute."
            )

        inner = self._inner_message(raw)
        sender = self._extract_sender(inner)
        subject = self._extract_subject(inner)
        body = self._extract_body(inner)

        return InboundMessage(
            admin_id=route.admin_id,
            instance_id=route.luciel_instance_id,
            session_id=None,
            customer_identifier=sender,
            body=body,
            channel_metadata={
                "recipient": recipient,
                "subject": subject,
                "sns_message_id": raw.get("MessageId"),
            },
            received_at=datetime.now(timezone.utc),
        )

    # -----------------------------------------------------------------
    # send — dispatch the reply via SES.
    # -----------------------------------------------------------------

    def send(self, message: OutboundMessage) -> DeliveryReceipt:
        subject = (message.channel_metadata or {}).get("subject") or "Re: your message"
        reply_to = (message.channel_metadata or {}).get("reply_to") or settings.from_email

        if _transport() != _SES_TRANSPORT:
            # Dev/CI log transport — no SES call, synthetic id. Mirrors
            # the live-switch discipline: the non-live path never hits a
            # real provider.
            synthetic_id = f"log-email-{uuid.uuid4().hex}"
            logger.info(
                "email_adapter: (log transport) reply to=%s subject=%s "
                "synthetic_id=%s",
                message.to,
                subject,
                synthetic_id,
            )
            return DeliveryReceipt(
                provider_message_id=synthetic_id,
                status="logged",
                channel=self.channel,
                timestamp=datetime.now(timezone.utc),
            )

        import boto3  # pragma: no cover - live only

        region = (  # pragma: no cover - live only
            os.getenv("SES_REGION")
            or os.getenv("AWS_REGION")
            or os.getenv("AWS_DEFAULT_REGION")
            or "ca-central-1"
        )
        client = boto3.client("sesv2", region_name=region)  # pragma: no cover
        resp = client.send_email(  # pragma: no cover - live only
            FromEmailAddress=reply_to,
            Destination={"ToAddresses": [message.to]},
            Content={
                "Simple": {
                    "Subject": {"Data": subject},
                    "Body": {"Text": {"Data": message.body}},
                }
            },
        )
        provider_id = resp.get("MessageId") if isinstance(resp, dict) else None
        return DeliveryReceipt(
            provider_message_id=provider_id,
            status="sent",
            channel=self.channel,
            timestamp=datetime.now(timezone.utc),
        )

    # -----------------------------------------------------------------
    # Payload parsing helpers.
    # -----------------------------------------------------------------

    def _inner_message(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Decode the SNS ``Message`` field (JSON-encoded SES payload).

        SES inbound notifications wrap the mail object inside the SNS
        ``Message`` string. A flat (already-decoded) dict is accepted too
        so tests can pass the mail object directly.
        """
        inner = raw.get("Message")
        if isinstance(inner, str):
            try:
                decoded = json.loads(inner)
            except json.JSONDecodeError:
                return {}
            return decoded if isinstance(decoded, dict) else {}
        if isinstance(inner, dict):
            return inner
        # No SNS wrapper — treat the raw payload itself as the mail object.
        return raw

    def _extract_recipient(self, raw: dict[str, Any]) -> str | None:
        """Resolve the inbound recipient (the address Luciel listens on).

        SES inbound puts recipients on ``mail.destination`` (a list) and
        also on ``receipt.recipients``. We take the first non-empty.
        """
        inner = self._inner_message(raw)
        mail = inner.get("mail") or {}
        for key, parent in (("destination", mail), ("recipients", inner.get("receipt") or {})):
            recipients = parent.get(key)
            if isinstance(recipients, list):
                for r in recipients:
                    addr = parseaddr(r)[1] if isinstance(r, str) else None
                    if addr:
                        return addr.lower()
        return None

    def _extract_sender(self, inner: dict[str, Any]) -> str:
        mail = inner.get("mail") or {}
        source = mail.get("source")
        if isinstance(source, str) and source.strip():
            return parseaddr(source)[1].lower() or source.strip().lower()
        common = mail.get("commonHeaders") or {}
        from_list = common.get("from")
        if isinstance(from_list, list) and from_list:
            return parseaddr(from_list[0])[1].lower()
        return ""

    def _extract_subject(self, inner: dict[str, Any]) -> str:
        mail = inner.get("mail") or {}
        common = mail.get("commonHeaders") or {}
        subject = common.get("subject")
        return subject if isinstance(subject, str) else ""

    def _extract_body(self, inner: dict[str, Any]) -> str:
        """Pull the plain-text body.

        SES inbound can carry the content in ``content`` (when SNS action
        includes the message) or our test payloads put it in ``body``.
        """
        for key in ("body", "content", "text"):
            val = inner.get(key)
            if isinstance(val, str) and val.strip():
                return val
        return ""
