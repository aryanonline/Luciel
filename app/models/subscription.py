"""Subscription model — Step 30a.

A `Subscription` row binds a paying customer's Stripe customer + subscription
to exactly one Luciel `tenant_id`. The scope hierarchy in ARCHITECTURE §4.1
makes a tenant the billing boundary, so a subscription is per-tenant.

Two design decisions worth recording:

1. **Email-stable identity is captured here** (alongside ``user_id``), because
   the future Sarah-to-department re-parenting flow (CANONICAL_RECAP Q5,
   Step 38) needs to look up an Individual subscription by the buyer's
   email when a department upgrades them. ``user_id`` is the FK; ``email``
   is a denormalized copy that survives audit-only reads.

2. **Soft-delete via ``active`` + Stripe-mirrored ``status``.** Cancellation
   flips ``active=false`` and records the Stripe status so the audit chain
   can answer "what tier did this customer have on date X" — same
   discipline as ARCHITECTURE §4.3 (immutable audit chain) and §4.4
   (soft-delete by default). Hard-delete only via the retention worker.

Pattern E discipline: we never DELETE a subscription row. A cancelled
subscription stays in the table with ``active=false`` and ``status``
mirroring Stripe; a customer who reactivates gets a fresh row (Stripe
treats reactivation as a new subscription anyway).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


# ---------------------------------------------------------------------
# Tier constants — kept module-level (not a PG enum) so a new tier
# (e.g. 'individual_pro') can land without a schema migration.
# Mirrors CANONICAL_RECAP §14 monetization tiers.
# ---------------------------------------------------------------------

TIER_INDIVIDUAL = "individual"
TIER_TEAM = "team"
TIER_COMPANY = "company"

ALLOWED_TIERS = (TIER_INDIVIDUAL, TIER_TEAM, TIER_COMPANY)


# ---------------------------------------------------------------------
# Status constants — mirror Stripe's subscription status values exactly.
# https://stripe.com/docs/api/subscriptions/object#subscription_object-status
# Stored as a String so a future Stripe-side new status doesn't require
# a migration; ALLOWED_STATUSES is advisory only.
# ---------------------------------------------------------------------

STATUS_INCOMPLETE = "incomplete"
STATUS_INCOMPLETE_EXPIRED = "incomplete_expired"
STATUS_TRIALING = "trialing"
STATUS_ACTIVE = "active"
STATUS_PAST_DUE = "past_due"
STATUS_CANCELED = "canceled"
STATUS_UNPAID = "unpaid"
STATUS_PAUSED = "paused"

ALLOWED_STATUSES = (
    STATUS_INCOMPLETE,
    STATUS_INCOMPLETE_EXPIRED,
    STATUS_TRIALING,
    STATUS_ACTIVE,
    STATUS_PAST_DUE,
    STATUS_CANCELED,
    STATUS_UNPAID,
    STATUS_PAUSED,
)

# Statuses where the subscription should result in an ACTIVE tenant.
# Anything else (canceled, unpaid past dunning, incomplete_expired) flips
# the tenant inactive via the cascade in ARCHITECTURE §4.5.
ENTITLED_STATUSES = frozenset({STATUS_TRIALING, STATUS_ACTIVE, STATUS_PAST_DUE})


class Subscription(Base, TimestampMixin):
    """One Stripe subscription bound to one Luciel tenant.

    A tenant has at most one active subscription at a time; older
    cancelled subscriptions stay in the table for the audit chain
    (Pattern E — deactivate, never delete).
    """

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # -----------------------------------------------------------------
    # Scope binding — the tenant this subscription pays for.
    # -----------------------------------------------------------------
    # Stored as a string FK to tenant_configs.tenant_id rather than the
    # numeric PK because the rest of the platform addresses tenants
    # by their slug (ARCHITECTURE §4.1).
    tenant_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        comment="FK to tenant_configs.tenant_id. Scope-as-billing-boundary.",
    )

    # -----------------------------------------------------------------
    # Identity — email-stable, survives role changes (CANONICAL_RECAP Q5).
    # -----------------------------------------------------------------
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Denormalized copy of users.email at the moment of purchase. The
    # users row is the source of truth, but we keep a copy here so
    # the audit chain answer to "who paid for tenant X on date Y"
    # doesn't require the users row to still exist.
    customer_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)

    # -----------------------------------------------------------------
    # Stripe identifiers — the bridge to the payment system.
    # -----------------------------------------------------------------
    stripe_customer_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True,
        comment="Stripe customer id (cus_…). One customer per User per tenant.",
    )
    stripe_subscription_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True,
        comment="Stripe subscription id (sub_…). Globally unique per Stripe.",
    )
    stripe_price_id: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="Stripe price id (price_…). The SKU the customer subscribed to.",
    )

    # -----------------------------------------------------------------
    # Plan + status — what we sell and what state Stripe says we are in.
    # -----------------------------------------------------------------
    tier: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True,
        comment="One of ALLOWED_TIERS. Self-serve v1 only mints 'individual'.",
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True,
        comment="Stripe subscription status, mirrored verbatim. See ALLOWED_STATUSES.",
    )

    # -----------------------------------------------------------------
    # Billing cycle — used by the Account/billing UI and dunning logic.
    # -----------------------------------------------------------------
    current_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    trial_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="If True, status stays 'active' until current_period_end then flips to 'canceled'.",
    )
    canceled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # -----------------------------------------------------------------
    # Soft-delete — Pattern E. Mirrors active=false on the tenant cascade.
    # -----------------------------------------------------------------
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True,
    )

    # -----------------------------------------------------------------
    # Provider payload + last event marker — debugging + replay.
    # -----------------------------------------------------------------
    # Last raw Stripe object snapshot (subscription.* event). Bounded
    # by Stripe's payload size; useful when reconciling a divergence
    # between our local state and Stripe's view.
    last_event_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="Stripe event id (evt_…) of the most recent webhook applied.",
    )
    provider_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # -----------------------------------------------------------------
    # Relationships
    # -----------------------------------------------------------------
    user: Mapped["User"] = relationship("User", lazy="joined")

    __table_args__ = (
        # The two queries we care about most:
        # 1. "Is this tenant currently entitled?" — look up active+entitled by tenant_id.
        Index("ix_subscriptions_tenant_active", "tenant_id", "active"),
        # 2. "Which subscription did this Stripe customer last buy?" — for the portal flow.
        Index("ix_subscriptions_stripe_customer", "stripe_customer_id"),
        {"comment": "Step 30a — Stripe subscription <-> Luciel tenant binding."},
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Subscription id={self.id} tenant={self.tenant_id} "
            f"tier={self.tier} status={self.status} active={self.active}>"
        )

    @property
    def is_entitled(self) -> bool:
        """True iff the customer should currently have working access."""
        return self.active and self.status in ENTITLED_STATUSES
