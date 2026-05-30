"""ChannelRoute ORM model — Arc 13 inbound addressing → Instance map.

A *channel route* answers the question every store-and-forward inbound
turn asks: "this email landed on address X / this SMS came in on number
Y — which (admin_id, instance_id) owns it?" The widget never needs a
route row (its embed key already carries the binding); email and SMS do,
because the provider only hands us the destination address/number.

Addressing shapes (Architecture §3.1 / Vision §7):

  * email — two sub-shapes share one row type:
        - platform subdomain: ``<instance-slug>@<admin-slug>.luciel-mail.com``
        - custom domain:       a customer-verified address on their domain
    Both are stored as the fully-qualified lowercase address in
    ``route_value`` with ``channel='email'``.

  * sms — the provisioned per-instance E.164 number, stored in
    ``route_value`` with ``channel='sms'``.

Uniqueness (one address/number → exactly one Instance):
  * A given ``(channel, route_value)`` maps to AT MOST one live route.
    Enforced by a partial unique index over live (non-revoked) rows so
    a number can be re-provisioned to another instance after release
    without colliding with the historical row.

Tenant scoping + RLS:
  * ``admin_id NOT NULL`` strict-tenant shape (Arc 9 C11), fenced by a
    RESTRICTIVE + FORCE RLS policy keyed on ``app.admin_id`` — identical
    to ``knowledge_sources``. Instance scoping stays at the service
    layer (Ownership Model C: an admin reads across their own
    instances), matching the knowledge_sources doctrine.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Channel id literals shared with app.channels and entitlements.
CHANNEL_EMAIL = "email"
CHANNEL_SMS = "sms"


class ChannelRoute(Base):
    __tablename__ = "channel_routes"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    admin_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    luciel_instance_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("instances.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # 'email' | 'sms' — the channel this route addresses.
    channel: Mapped[str] = mapped_column(String(50), nullable=False)
    # The fully-qualified inbound address: lowercase email or E.164 SMS.
    route_value: Mapped[str] = mapped_column(String(320), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # Soft-release stamp. NULL = live route. A released number/address
    # keeps its historical row (for audit) but stops being unique so it
    # can be re-provisioned to a different instance.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index(
            "ix_channel_routes_admin_instance",
            "admin_id",
            "luciel_instance_id",
        ),
        Index("ix_channel_routes_channel_value", "channel", "route_value"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ChannelRoute id={self.id} admin_id={self.admin_id} "
            f"instance={self.luciel_instance_id} channel={self.channel} "
            f"value={self.route_value!r} live={self.revoked_at is None}>"
        )
