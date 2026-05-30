"""Arc 13 — channel-adapter subsystem.

The channel layer is the ingress/egress boundary between an external
messaging surface (the embeddable widget, inbound email, inbound SMS)
and the Luciel runtime. Every surface implements the one
:class:`~app.channels.base.ChannelAdapter` protocol so the runtime is
agnostic to whether a turn arrived over a live SSE socket (widget,
streaming) or a store-and-forward provider webhook (email / SMS).

Slice 1 (this commit) ships the contract + the widget retrofit. Slices
2/3 add the email + SMS adapters that import from
:mod:`app.channels.base`.
"""
from __future__ import annotations

from app.channels.base import (
    ChannelAdapter,
    DeliveryReceipt,
    InboundMessage,
    InstanceContext,
    OutboundMessage,
    SignatureVerificationError,
    UnresolvableInboundError,
)

__all__ = [
    "ChannelAdapter",
    "DeliveryReceipt",
    "InboundMessage",
    "InstanceContext",
    "OutboundMessage",
    "SignatureVerificationError",
    "UnresolvableInboundError",
]
