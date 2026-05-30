"""Arc 13 D4 — SmsChannelAdapter (store-and-forward over Twilio).

The SMS surface is STORE-AND-FORWARD: an inbound customer text arrives
as a Twilio webhook POST (form-encoded), and the reply is dispatched as
a separate outbound Twilio REST call that returns a provider message
sid. This adapter expresses the ``verify_inbound`` / ``receive`` /
``send`` contract over that wire.

Trust gate (verify_inbound, BEFORE any routing/parsing)
-------------------------------------------------------
Twilio signs every webhook with ``X-Twilio-Signature``: an
HMAC-SHA1 over (the full request URL + every POST param sorted by key,
concatenated as key+value), keyed by the account auth token, then
base64-encoded. We recompute it and ``hmac.compare_digest`` against the
header. Mismatch (or missing token/header) →
:class:`SignatureVerificationError`, raised BEFORE any routing. The
verification is pure-stdlib (``hmac``/``hashlib``) so neither the dev
path nor the test suite needs the ``twilio`` package installed.

Only once authenticity holds do we resolve routing: the destination
number (``To``) must map to a live ``ChannelRoute`` (channel='sms');
otherwise :class:`UnresolvableInboundError`.

Routing model
-------------
A tenant's dedicated number is a ``ChannelRoute`` row with
``channel='sms'`` and ``route_value`` = the E.164 number. The sender
(``From``) is the customer's identity claim (resolved to a session by
the webhook handler via ``SessionService.create_session_with_identity``
with ``issuing_adapter='sms_gateway'``), so the same customer texting
twice continues one conversation.

Shared/brokerage routing is DEDICATED-ONLY in Arc 13 (see
``provisioning.BrokerageRoutingNotImplementedError``); this adapter only
ever resolves a number to the single Instance that owns its dedicated
route.

Outbound
--------
``send`` dispatches via the Twilio REST API and returns a
:class:`DeliveryReceipt` with the provider message sid — but ONLY when
``settings.channels_live_provisioning_enabled`` is True AND the Twilio
credentials are present. With the live switch off (the dev/CI default)
no Twilio call is made and a synthetic sid is returned, mirroring the
provisioning live-switch discipline: the non-live path never bills
Twilio.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.channels.base import (
    DeliveryReceipt,
    InboundMessage,
    InstanceContext,
    OutboundMessage,
    SignatureVerificationError,
    UnresolvableInboundError,
)
from app.core.config import settings
from app.models.channel_route import CHANNEL_SMS, ChannelRoute

logger = logging.getLogger(__name__)

CHANNEL_SMS_ADAPTER = "sms"


def compute_twilio_signature(*, url: str, params: dict[str, str], auth_token: str) -> str:
    """Recompute Twilio's ``X-Twilio-Signature`` for (url, params).

    Algorithm (per Twilio's published spec): take the full request URL,
    append each POST param as ``key + value`` in key-sorted order, HMAC-
    SHA1 with the account auth token, base64-encode. Pure stdlib so no
    twilio package is needed for verification.
    """
    data = url
    for key in sorted(params):
        data += key + params[key]
    digest = hmac.new(
        auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode("ascii")


class SmsChannelAdapter:
    """Store-and-forward channel adapter for inbound/outbound SMS.

    Constructed with a DB :class:`Session` so ``verify_inbound`` can
    resolve the destination number to a live :class:`ChannelRoute`.
    ``raw`` for this adapter is a ``dict`` with three keys the webhook
    route populates: ``{"url": <full request url>, "params": <form
    dict>, "signature": <X-Twilio-Signature header>}``.
    """

    channel: str = CHANNEL_SMS_ADAPTER

    def __init__(self, db: Session) -> None:
        self.db = db

    # -----------------------------------------------------------------
    # verify_inbound — signature FIRST, then routing.
    # -----------------------------------------------------------------

    def verify_inbound(self, raw: Any) -> InstanceContext:
        if not isinstance(raw, dict):
            raise SignatureVerificationError(
                "SMS inbound payload is not a webhook envelope dict."
            )

        url = raw.get("url") or ""
        params = raw.get("params") or {}
        provided_sig = raw.get("signature") or ""

        auth_token = settings.twilio_auth_token
        if not auth_token:
            raise SignatureVerificationError(
                "Twilio auth token is unset; cannot verify X-Twilio-Signature."
            )
        if not provided_sig:
            raise SignatureVerificationError(
                "Inbound SMS carries no X-Twilio-Signature header."
            )

        expected = compute_twilio_signature(
            url=url, params=params, auth_token=auth_token
        )
        if not hmac.compare_digest(expected, provided_sig):
            logger.warning(
                "sms_adapter: SECURITY: X-Twilio-Signature mismatch for url=%s",
                url,
            )
            raise SignatureVerificationError(
                "Inbound SMS X-Twilio-Signature does not verify."
            )

        # --- Authenticity established → resolve routing on ``To``. ---
        to_number = (params.get("To") or "").strip()
        if not to_number:
            raise UnresolvableInboundError(
                "Inbound SMS payload carries no destination ('To') number."
            )

        route = self._live_route(to_number)
        if route is None:
            raise UnresolvableInboundError(
                f"Inbound SMS to {to_number!r} maps to no live sms ChannelRoute."
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
                "SMS inbound payload is not a webhook envelope dict."
            )
        params = raw.get("params") or {}
        to_number = (params.get("To") or "").strip()
        from_number = (params.get("From") or "").strip()
        body = params.get("Body") or ""

        route = self._live_route(to_number)
        if route is None:
            raise UnresolvableInboundError(
                f"Inbound SMS to {to_number!r} maps to no live sms ChannelRoute."
            )

        return InboundMessage(
            admin_id=route.admin_id,
            instance_id=route.luciel_instance_id,
            session_id=None,
            customer_identifier=from_number,
            body=body,
            channel_metadata={
                "to": to_number,
                "message_sid": params.get("MessageSid"),
                "num_segments": params.get("NumSegments"),
            },
            received_at=datetime.now(timezone.utc),
        )

    # -----------------------------------------------------------------
    # send — dispatch the reply via Twilio REST (live-switch gated).
    # -----------------------------------------------------------------

    def send(self, message: OutboundMessage) -> DeliveryReceipt:
        from_number = (message.channel_metadata or {}).get("from")

        if not settings.channels_live_provisioning_enabled:
            synthetic_sid = f"SMfake{uuid.uuid4().hex[:24]}"
            logger.info(
                "sms_adapter: (live switch off) reply to=%s synthetic_sid=%s",
                message.to,
                synthetic_sid,
            )
            return DeliveryReceipt(
                provider_message_id=synthetic_sid,
                status="logged",
                channel=self.channel,
                timestamp=datetime.now(timezone.utc),
            )

        if not (settings.twilio_account_sid and settings.twilio_auth_token):
            raise SignatureVerificationError(
                "Twilio credentials unset; cannot send live SMS."
            )

        from twilio.rest import Client  # pragma: no cover - live only

        client = Client(  # pragma: no cover - live only
            settings.twilio_account_sid, settings.twilio_auth_token
        )
        kwargs: dict[str, Any] = {  # pragma: no cover - live only
            "to": message.to,
            "body": message.body,
        }
        if settings.twilio_messaging_service_sid:  # pragma: no cover - live only
            kwargs["messaging_service_sid"] = settings.twilio_messaging_service_sid
        elif from_number:  # pragma: no cover - live only
            kwargs["from_"] = from_number
        sent = client.messages.create(**kwargs)  # pragma: no cover - live only
        return DeliveryReceipt(  # pragma: no cover - live only
            provider_message_id=sent.sid,
            status="sent",
            channel=self.channel,
            timestamp=datetime.now(timezone.utc),
        )

    # -----------------------------------------------------------------
    # Helpers.
    # -----------------------------------------------------------------

    def _live_route(self, number: str) -> ChannelRoute | None:
        if not number:
            return None
        return (
            self.db.query(ChannelRoute)
            .filter(
                ChannelRoute.channel == CHANNEL_SMS,
                ChannelRoute.route_value == number,
                ChannelRoute.revoked_at.is_(None),
            )
            .first()
        )
