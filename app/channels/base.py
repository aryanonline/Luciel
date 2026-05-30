"""Arc 13 — the ChannelAdapter contract.

One protocol, three surfaces. A *channel* is any external messaging
surface a customer's end-user can reach Luciel through:

  * ``widget``  — the embeddable Preact chat panel. STREAMING: the
                  inbound turn and the outbound reply share one open
                  HTTP/SSE connection. ``send`` resolves synchronously
                  over that socket; there is no provider message id.
  * ``email``   — inbound email via an SES (or equivalent) webhook.
                  STORE-AND-FORWARD: the inbound turn arrives as a
                  provider POST, the reply is dispatched as a separate
                  outbound API call that returns a provider message id.
  * ``sms``     — inbound SMS via a Twilio (or equivalent) webhook.
                  STORE-AND-FORWARD, same shape as email.

The abstraction has to fit BOTH the streaming widget and the
store-and-forward gateways, so it is deliberately verb-shaped rather
than transport-shaped:

  verify_inbound(raw) -> InstanceContext
      Validate the provider's authenticity signal (HMAC signature,
      Twilio ``X-Twilio-Signature``, SES SNS signature, or — for the
      widget — the already-authenticated embed-key request.state)
      FIRST, and only then resolve the addressing of the raw payload
      to a concrete (admin_id, instance_id, session_id). Two failure
      modes, two typed exceptions, in a fixed order:
        1. signature/authenticity fails  -> SignatureVerificationError
           raised BEFORE any parsing or routing work happens.
        2. routing cannot be resolved     -> UnresolvableInboundError
           (address/number maps to no live instance, etc.).

  receive(raw) -> InboundMessage
      Parse the (already-verified) raw payload into the canonical
      InboundMessage the runtime consumes. Callers MUST call
      verify_inbound first; receive does not re-verify the signature.

  send(OutboundMessage) -> DeliveryReceipt
      Dispatch a reply. For streaming surfaces (widget) the receipt is
      synchronous and carries ``provider_message_id=None``. For
      store-and-forward surfaces the receipt carries the provider's
      message id and accepted/queued status.

``raw`` is intentionally typed ``Any`` on the protocol: each adapter
receives a transport-native object (a Starlette ``Request`` for the
widget, a parsed webhook ``dict`` for email/SMS) and is responsible
for narrowing it. Keeping ``raw`` opaque at the protocol boundary is
what lets one protocol cover three wire formats.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


# =====================================================================
# Typed errors.
# =====================================================================
#
# Two distinct failure modes on the inbound path, surfaced as two
# exception types so the transport layer (a FastAPI route, a webhook
# handler) can map each to the correct HTTP status without string
# matching:
#
#   SignatureVerificationError -> 401/403 (authenticity failure; the
#       caller is not who they claim to be). MUST be raised before any
#       routing or body parsing so a forged payload never reaches the
#       resolution logic.
#
#   UnresolvableInboundError   -> 404/422 (the payload is authentic but
#       its addressing does not map to a live instance — unknown
#       recipient address, deprovisioned number, instance soft-deleted).


class ChannelError(Exception):
    """Base class for all channel-adapter ingress/egress errors."""


class SignatureVerificationError(ChannelError):
    """Raised when a provider's authenticity signal fails validation.

    MUST be raised by ``verify_inbound`` BEFORE any routing resolution
    or body parsing happens — a payload whose signature does not check
    out is treated as hostile and never processed further.
    """


class UnresolvableInboundError(ChannelError):
    """Raised when an authentic inbound payload cannot be routed.

    The signature verified, but the addressing (recipient email
    address, destination phone number, embed-key instance binding)
    does not resolve to a live (admin_id, instance_id). Distinct from
    :class:`SignatureVerificationError` so the transport layer can
    return 404/422 (not-found) rather than 401/403 (forbidden).
    """


# =====================================================================
# Canonical dataclasses.
# =====================================================================


@dataclass(frozen=True)
class InstanceContext:
    """The resolved tenant scope of an inbound turn.

    Output of :meth:`ChannelAdapter.verify_inbound`. Carries exactly
    the V2 Admin→Instance→Session triple (Arc 12 excised the v1
    domain_id / agent_id layers — do NOT reintroduce them here).

    ``session_id`` may be ``None`` on the very first turn of a
    store-and-forward conversation, where the adapter resolves it
    lazily via SessionService.create_session_with_identity after
    verification. The widget, which creates its session inside the
    route, populates it once known.
    """

    admin_id: str
    instance_id: int
    session_id: str | None = None


@dataclass(frozen=True)
class InboundMessage:
    """A canonical inbound turn, transport-agnostic.

    Matches Architecture §3.1.2's seven-field inbound shape. Produced
    by :meth:`ChannelAdapter.receive` after verification. The runtime
    (ChatService.respond / respond_stream) consumes ``body`` +
    ``session_id`` + the tenant scope; ``customer_identifier`` and
    ``channel_metadata`` carry the channel-specific provenance the
    identity resolver and audit log need.

    Fields (the §3.1.2 seven):
        admin_id            — resolved tenant.
        instance_id         — resolved instance (V2 single boundary).
        session_id          — the session this turn belongs to; None
                              until lazily created on a first
                              store-and-forward turn.
        customer_identifier — the channel-native identity of the
                              end-user: the email address, the E.164
                              phone number, or the widget visitor's
                              asserted claim value. Fed to the identity
                              resolver as the claim value.
        body                — the message text the runtime answers.
        channel_metadata    — channel-specific provenance (provider
                              message ids, subject lines, MIME parts,
                              SMS segment counts, embed-key prefix).
                              Opaque to the runtime; surfaced to audit.
        received_at         — ingress timestamp (tz-aware UTC).
    """

    admin_id: str
    instance_id: int
    session_id: str | None
    customer_identifier: str
    body: str
    channel_metadata: dict[str, Any] = field(default_factory=dict)
    received_at: datetime | None = None


@dataclass(frozen=True)
class OutboundMessage:
    """A canonical outbound reply, transport-agnostic.

    Input to :meth:`ChannelAdapter.send`. Carries the destination
    address, the body, the tenant scope, and the session linkage so a
    store-and-forward adapter can thread the reply onto the right
    provider conversation and an audit row can be written.

    ``to`` is the channel-native destination: the customer's email
    address, their E.164 number, or — for the widget — ignored, since
    the reply streams back over the open socket the request arrived on.
    """

    to: str
    body: str
    admin_id: str
    instance_id: int
    session_id: str | None = None
    channel_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryReceipt:
    """The result of dispatching an :class:`OutboundMessage`.

    ``provider_message_id`` is nullable: streaming surfaces (the
    widget) deliver synchronously over the open connection and have no
    provider-assigned id, so they return ``None``. Store-and-forward
    surfaces (email / SMS) return the provider's accepted-message id so
    delivery can be correlated with later provider status callbacks.

    ``status`` is a short channel-agnostic token — e.g. ``"streamed"``
    for the widget's synchronous path, ``"queued"`` / ``"sent"`` /
    ``"accepted"`` for a provider that acknowledged the dispatch.
    """

    provider_message_id: str | None
    status: str
    channel: str
    timestamp: datetime


# =====================================================================
# The protocol.
# =====================================================================


@runtime_checkable
class ChannelAdapter(Protocol):
    """The single ingress/egress contract every channel implements.

    Streaming (widget) and store-and-forward (email / SMS) adapters
    both satisfy this protocol; the difference between them is captured
    in the *values* they return (a ``DeliveryReceipt`` with a None
    provider id and ``status="streamed"`` vs. one carrying a real
    provider message id), not in the *shape* of the methods.

    ``channel`` is the channel id ("widget" / "email" / "sms"), used to
    stamp DeliveryReceipt.channel and to key per-channel entitlement
    and routing lookups.
    """

    channel: str

    def verify_inbound(self, raw: Any) -> InstanceContext:
        """Validate authenticity, THEN resolve tenant scope.

        Order is contractual: validate the provider signature FIRST and
        raise :class:`SignatureVerificationError` on failure BEFORE any
        parsing or routing. Only once authenticity is established,
        resolve the payload's addressing to a concrete
        (admin_id, instance_id, session_id); raise
        :class:`UnresolvableInboundError` when it maps to no live
        instance.
        """
        ...

    def receive(self, raw: Any) -> InboundMessage:
        """Parse an already-verified raw payload into an InboundMessage.

        Callers MUST call :meth:`verify_inbound` first. ``receive``
        does not re-validate the signature; it only canonicalises the
        payload into the runtime's seven-field inbound shape.
        """
        ...

    def send(self, message: OutboundMessage) -> DeliveryReceipt:
        """Dispatch a reply and return a DeliveryReceipt.

        Streaming surfaces resolve this synchronously over the open
        connection (``provider_message_id=None``); store-and-forward
        surfaces dispatch to a provider and return its message id.
        """
        ...
