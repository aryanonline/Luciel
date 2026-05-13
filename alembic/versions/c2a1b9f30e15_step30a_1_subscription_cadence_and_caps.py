"""Step 30a.1: subscription billing_cadence + instance_count_cap.

Revision ID: c2a1b9f30e15
Revises: b8e74a3c1d52
Create Date: 2026-05-13

Why this migration exists
-------------------------

Step 30a shipped the ``subscriptions`` table with a single SKU (Individual
monthly). Step 30a.1 lifts the v1 carve-out: all three tiers (Individual,
Team, Company) and both cadences (monthly, annual) become self-serve.

Two columns make the row honest about the new product surface:

* ``billing_cadence VARCHAR(16) NOT NULL DEFAULT 'monthly'`` — answers the
  cohort question "of our Team subscribers, how many pay monthly vs annual?"
  with a single GROUP BY. Defaults to ``'monthly'`` because every existing
  row was minted under the Step 30a Individual-monthly path, so the default
  is factually correct for the backfill — no historical lie introduced.

* ``instance_count_cap INTEGER NOT NULL DEFAULT 3`` — the hard ceiling on
  active LucielInstances under this subscription. Per the CANONICAL_RECAP
  §14 commitment that *"the tiers exist as separate products and not as
  seat counts"*, this column does NOT meter usage; it caps pathological
  over-provisioning under one subscription (a runaway script that mints
  200 Luciels under a single $300/mo subscription is a billing-integrity
  problem the cap catches). Default ``3`` matches the existing
  Individual-tier expectation; the webhook overwrites the value for Team
  (10) and Company (50) subscribers from the per-tier table defined in
  ``app/models/subscription.py::TIER_INSTANCE_CAPS``.

Design decisions worth recording
--------------------------------

* **No `ALTER TYPE` work.** ``subscriptions.tier`` is a ``String(32)``,
  not a PostgreSQL ENUM (the model comment at lines 51–54 of
  ``app/models/subscription.py`` records that this is deliberate so a
  new tier can land without a schema migration). The ``ALLOWED_TIERS``
  tuple already includes ``'team'`` and ``'company'``; this step turns
  the string into a *minted* string for those values via the webhook
  path and the BillingService.resolve_price_id lookup.

* **No `seat_count`, `conversation_cap`, or per-seat-metered column.**
  The CANONICAL_RECAP §14 commitment forbids those shapes. Tier-as-scope
  is the single axis of differentiation; instance_count_cap is a
  per-subscription guardrail, not a per-seat meter.

* **CHECK constraint on `billing_cadence`** — the column accepts only
  ``'monthly'`` or ``'annual'``. PostgreSQL enforces it at the DB layer
  for the same reason ``status`` is constrained at the model layer:
  a malformed Stripe payload should not be able to land a third value.

* **New composite index `ix_subscriptions_tier_active`** — answers the
  query "how many active Team subscriptions do we have?" in O(log n)
  rather than a full table scan as the table grows. The two existing
  indexes (``ix_subscriptions_tenant_active``, ``ix_subscriptions_stripe_customer``)
  remain unchanged.

* **Pattern E discipline.** Additive only; no data loss; the DEFAULT on
  both columns means existing rows are valid from the moment the ALTER
  TABLE finishes. No backfill UPDATE statement is needed.

Rollback (downgrade) drops the constraint + index + columns in the
reverse order. The drop is destructive (the values themselves are lost)
but the data we lose is small (the cadence flag and the cap value, both
re-derivable from Stripe + the per-tier defaults table).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c2a1b9f30e15"
down_revision = "b8e74a3c1d52"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add billing_cadence + instance_count_cap + CHECK + index.

    The two columns are NOT NULL with safe defaults so the migration is
    online-safe even on a live ``subscriptions`` table.
    """
    op.add_column(
        "subscriptions",
        sa.Column(
            "billing_cadence",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'monthly'"),
            comment=(
                "Stripe cadence the buyer selected at checkout. "
                "One of 'monthly' | 'annual'. Default 'monthly' matches "
                "every Step 30a row (Individual-monthly only)."
            ),
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "instance_count_cap",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("3"),
            comment=(
                "Hard ceiling on active LucielInstances under this "
                "subscription. Not a seat count (§14) — a billing-"
                "integrity guardrail. 3 / 10 / 50 for Individual / "
                "Team / Company per TIER_INSTANCE_CAPS."
            ),
        ),
    )

    # CHECK constraint on billing_cadence — fail-closed at the DB layer.
    op.create_check_constraint(
        "ck_subscriptions_billing_cadence",
        "subscriptions",
        "billing_cadence IN ('monthly', 'annual')",
    )

    # Composite index for tier-cohort queries.
    op.create_index(
        "ix_subscriptions_tier_active",
        "subscriptions",
        ["tier", "active"],
    )


def downgrade() -> None:
    """Drop the Step 30a.1 columns + index + constraint.

    Reverse order of upgrade. Destructive — the cadence + cap values are
    lost. Re-deriving them requires re-reading from Stripe (cadence) and
    re-applying TIER_INSTANCE_CAPS (cap).
    """
    op.drop_index("ix_subscriptions_tier_active", table_name="subscriptions")
    op.drop_constraint(
        "ck_subscriptions_billing_cadence",
        "subscriptions",
        type_="check",
    )
    op.drop_column("subscriptions", "instance_count_cap")
    op.drop_column("subscriptions", "billing_cadence")
