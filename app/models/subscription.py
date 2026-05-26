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
# Tier constants — V2 SHAPE (Arc 5 B8, 2026-05-23).
#
# The v1 4-tier surface (TIER_INDIVIDUAL/TEAM/COMPANY) was DELETED at
# Arc 5 Commit 17 (B8) per docs/DRIFTS.md
# D-arc5-aggressive-cleanup-doctrine-amendment-2026-05-23. The V2
# canonical shape is Free / Pro / Enterprise (lowercase wire strings).
#
# Mirrors CANONICAL_RECAP §14 (V2 monetization tiers) +
# app/policy/entitlements.py (TIER_FREE/PRO/ENTERPRISE + 16-axis
# TierEntitlement matrix at §6.5 founder-locks).
#
# Source-of-truth for the tier rename mapping (Revision B data
# backfill, applied to prod at Arc 5 Commit 22):
#   individual + solo  → pro
#   team + company     → enterprise
#   (free is brand-new — no legacy mapping)
# ---------------------------------------------------------------------

TIER_FREE = "free"
TIER_PRO = "pro"
TIER_ENTERPRISE = "enterprise"

ALLOWED_TIERS = (TIER_FREE, TIER_PRO, TIER_ENTERPRISE)


# ---------------------------------------------------------------------
# Billing cadence constants (Step 30a.1).
#
# Two cadences land at Step 30a.1: monthly (the Step 30a default) and
# annual (a ~17% prepay incentive). The values are stored in the
# ``billing_cadence`` String(16) column with a DB CHECK constraint
# (see migration ``c2a1b9f30e15``) so a malformed payload cannot land
# a third value.
# ---------------------------------------------------------------------

BILLING_CADENCE_MONTHLY = "monthly"
BILLING_CADENCE_ANNUAL = "annual"

ALLOWED_BILLING_CADENCES = (BILLING_CADENCE_MONTHLY, BILLING_CADENCE_ANNUAL)


# ---------------------------------------------------------------------
# Per-tier instance-count caps (Arc 5 B8, V2 values per §6.5 founder-locks).
#
# CANONICAL_RECAP §14 forbids per-seat metering, so these caps are a
# *billing-integrity* guardrail. The V2 values:
#   Free       → 1 instance
#   Pro        → 10 instances
#   Enterprise → unlimited (sentinel 0 historically meant unlimited;
#                in V2 we use None so callsites get a clean type
#                signal via Optional[int]; comparison code at
#                billing_service treats None as "no cap").
#
# NOTE: The Domain layer is dead in V2 (D-arc5-aggressive-cleanup
# amendment). The legacy TIER_PERMITTED_SCOPES + DOMAIN_COUNT_CAP_BY_TIER
# maps were DELETED at this commit — the V2 Admin → Instance → Lead
# hierarchy has no scope-level pluralism to enforce.
#
# Source of truth: app/policy/entitlements.py TIER_ENTITLEMENTS map
# (16-axis full row per tier). This map is a service-layer fast-path
# for the single instance_count_cap axis; the full row is canonical.
# ---------------------------------------------------------------------

TIER_INSTANCE_CAPS: dict[str, int | None] = {
    TIER_FREE:       1,
    TIER_PRO:        10,
    TIER_ENTERPRISE: None,  # unlimited
}


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

    admin_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
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
        comment=(
            "One of ALLOWED_TIERS. Step 30a.1 lifted the v1 Individual-only "
            "carve-out — all three tiers self-serve via /billing/checkout."
        ),
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True,
        comment="Stripe subscription status, mirrored verbatim. See ALLOWED_STATUSES.",
    )

    # -----------------------------------------------------------------
    # Step 30a.1 — cadence + per-tier guardrail.
    # -----------------------------------------------------------------
    billing_cadence: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=BILLING_CADENCE_MONTHLY,
        server_default=BILLING_CADENCE_MONTHLY,
        comment=(
            "One of ALLOWED_BILLING_CADENCES. DB CHECK enforces the literal "
            "set; default 'monthly' preserves Step 30a behaviour for existing rows."
        ),
    )
    instance_count_cap: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        # Default to Pro's cap (10) — historically Subscription rows
        # were only created for paying customers (Individual/Team/Company).
        # In V2 Free admins have no Subscription row at all (lazy-create
        # on upgrade per Gap 1 lock), so any Subscription instantiated
        # without an explicit cap is at least Pro — hence Pro's value
        # is the safest module-import-time default. Service-layer code
        # at billing_service.compute_instance_cap_for_tier(tier) is the
        # canonical accessor and overrides this on actual creation.
        default=TIER_INSTANCE_CAPS[TIER_PRO],
        server_default=str(TIER_INSTANCE_CAPS[TIER_PRO]),
        comment=(
            "Hard ceiling on active Instances under this subscription. "
            "Not a seat count (§14) — a billing-integrity guardrail. "
            "None at the application layer means unlimited (Enterprise)."
        ),
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
    # Arc 6 Commit 8.5b — Deferred-downgrade marker.
    # -----------------------------------------------------------------
    # Set when the buyer schedules a tier downgrade via
    # POST /billing/downgrade. NULL = no downgrade pending (the common
    # case). The webhook ``_on_subscription_deleted`` branches on this
    # column: when set, run the V2 downgrade path (flip admin tier +
    # archive overflow); when NULL, run the V1 hard-cancel deactivate
    # path (preserved for manual Stripe-Dashboard cancels). CHECK
    # constraint at the DB level pins the legal values to {'free','pro'}
    # — Enterprise is the top tier and is never a downgrade target.
    pending_downgrade_target: Mapped[str | None] = mapped_column(
        String(16), nullable=True,
        comment=(
            "Arc 6 Commit 8.5b — destination tier of a scheduled "
            "downgrade. NULL = none pending. CHECK pins to {'free','pro'}."
        ),
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
        Index("ix_subscriptions_tenant_active", "admin_id", "active"),
        # 2. "Which subscription did this Stripe customer last buy?" — for the portal flow.
        Index("ix_subscriptions_stripe_customer", "stripe_customer_id"),
        # 3. Step 30a.1: tier-cohort queries ("how many active Team subs?").
        Index("ix_subscriptions_tier_active", "tier", "active"),
        {"comment": "Step 30a — Stripe subscription <-> Luciel tenant binding."},
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Subscription id={self.id} tenant={self.admin_id} "
            f"tier={self.tier} status={self.status} active={self.active}>"
        )

    @property
    def is_entitled(self) -> bool:
        """True iff the customer should currently have working access."""
        return self.active and self.status in ENTITLED_STATUSES
