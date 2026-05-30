"""Arc 13 D2 — the widget channel adapter.

``WidgetChannelAdapter`` conforms to
:class:`app.channels.base.ChannelAdapter` for the embeddable Preact
widget surface. The widget is the STREAMING member of the channel
family: the inbound turn and the outbound reply share one open
HTTP/SSE connection opened by the customer's browser, so:

  * ``verify_inbound`` does NOT re-implement signature checking — the
    widget's authenticity envelope is the embed-key dependency
    (``require_embed_key``) that already ran in the FastAPI dependency
    chain by the time the route body executes. By the time this adapter
    sees the request, ``request.state`` carries the verified
    ``admin_id`` / ``luciel_instance_id`` / ``key_prefix``. The adapter
    re-asserts those invariants and raises the channel-typed errors so
    the route's behaviour is expressed through the same contract email
    and SMS will use. A request whose embed key did not authenticate
    never reaches here (the dependency raised first) — that ordering IS
    the "validate signature FIRST" guarantee for the streaming surface.

  * ``send`` is synchronous: the reply streams back over the open
    socket the request arrived on, so the :class:`DeliveryReceipt`
    carries ``provider_message_id=None`` and ``status="streamed"``.

The full widget security envelope (embed-key kind, permissions==
['chat'], Origin allowlist, per-key rate limit, moderation gate, lazy
session creation, ``WIDGET_ISSUING_ADAPTER='widget'``) is unchanged —
it lives in the route + dependency layer; this adapter expresses the
verify/receive/send *shape* over that envelope without weakening it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.channels.base import (
    DeliveryReceipt,
    InboundMessage,
    InstanceContext,
    OutboundMessage,
    SignatureVerificationError,
    UnresolvableInboundError,
)

CHANNEL_WIDGET = "widget"


class WidgetChannelAdapter:
    """Streaming channel adapter for the embeddable widget.

    Constructed per-request in the route. ``raw`` for this adapter is
    the Starlette ``Request`` whose ``state`` the embed-key dependency
    has already populated.
    """

    channel: str = CHANNEL_WIDGET

    def verify_inbound(self, raw: Any) -> InstanceContext:
        """Re-assert the embed-key envelope, then resolve tenant scope.

        The embed-key dependency (``require_embed_key``) is the widget's
        authenticity signal and has already run before this method —
        a request that failed it never reaches the route body. We treat
        the absence of a verified ``admin_id`` on ``request.state`` as a
        signature failure (defence in depth: it should be impossible to
        get here without one) and raise BEFORE resolving routing, then
        raise :class:`UnresolvableInboundError` when the key is not
        bound to a live Instance.
        """
        state = getattr(raw, "state", None)
        admin_id = getattr(state, "admin_id", None)
        if not admin_id:
            # No verified tenant on the request — the embed-key envelope
            # did not establish authenticity. Mirror the route's 403
            # 'embed_key_not_tenant_scoped' as a signature failure.
            raise SignatureVerificationError(
                "Widget request carries no verified admin_id; the embed-key "
                "authenticity envelope did not establish a tenant."
            )

        instance_id = getattr(state, "luciel_instance_id", None)
        if instance_id is None:
            raise UnresolvableInboundError(
                "Embed key is not bound to a luciel_instance_id; the inbound "
                "widget turn cannot be routed to an Instance."
            )

        # session_id is created lazily inside the route (anonymous,
        # identity-bound, or follow-up). It is None at verify time.
        return InstanceContext(
            admin_id=admin_id,
            instance_id=int(instance_id),
            session_id=None,
        )

    def receive(self, raw: Any) -> InboundMessage:
        """Canonicalise the widget request into an InboundMessage.

        ``raw`` is a tuple ``(request, message, session_id)`` carrying
        the verified request, the turn body, and the (lazily created)
        session id. Callers MUST have called :meth:`verify_inbound`
        first; this method does not re-verify.
        """
        request, message, session_id = raw
        state = getattr(request, "state", None)
        admin_id = getattr(state, "admin_id", None)
        instance_id = getattr(state, "luciel_instance_id", None)
        key_prefix = getattr(state, "key_prefix", None)
        return InboundMessage(
            admin_id=admin_id,
            instance_id=int(instance_id) if instance_id is not None else None,
            session_id=session_id,
            customer_identifier=session_id or "anonymous",
            body=message,
            channel_metadata={"embed_key_prefix": key_prefix},
            received_at=datetime.now(timezone.utc),
        )

    def send(self, message: OutboundMessage) -> DeliveryReceipt:
        """Synchronous send over the open SSE connection.

        The widget reply is streamed token-by-token by the route's
        ``event_stream`` generator; there is no provider hand-off and
        no provider-assigned message id. The receipt records that the
        reply was streamed back synchronously.
        """
        return DeliveryReceipt(
            provider_message_id=None,
            status="streamed",
            channel=self.channel,
            timestamp=datetime.now(timezone.utc),
        )
