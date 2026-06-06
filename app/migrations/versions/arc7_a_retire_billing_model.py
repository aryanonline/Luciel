"""Arc 7 — Revision A: retire billing_model column.

Drops the ``billing_model`` enum-column scaffolding that Arc 5
Revision A added to back the Enterprise hybrid-billing shape. The
Arc 7 doctrine pivot (CANONICAL_RECAP §17 Arc 7 entry,
2026-05-24) retired the hybrid Enterprise shape in favour of
flat-recurring self-serve symmetric with Pro — under that vision
**every paying tier is flat**, so the column carries zero
information and would become a perpetual liability (every reader
of ``subscriptions`` would need to remember the legacy meaning).

Partner doctrine (Path A, 2026-05-24): "whatever we ship out in
our code and prod and schema must be aligned with this vision."
Keeping a 'hybrid' literal reachable in the CHECK constraint or
in code would violate that. The dataclass field
``TierEntitlement.billing_model`` and the legacy
``BILLING_MODEL_HYBRID`` / ``BILLING_MODEL_CONSUMPTION`` constants
are removed alongside this migration in Arc 7 Commit 2 code.

Design context:

* **Scope:** drop two columns and their CHECK constraints + the
  one index. Do NOT drop ``admin_tier_overrides`` (the table is
  forward-architecture for Enterprise contract overrides per
  ARCHITECTURE §3.2.14 and stays); only its ``billing_model``
  column goes.

* **No data salvage.** Every row currently in ``subscriptions``
  was backfilled to ``'flat'`` by Arc 5 Revision A's UPDATE
  (line 856 of that migration), so dropping the column loses no
  customer-facing information — the canonical buyer-facing
  shape is reconstructable from ``admins.tier`` (every paid
  admin is now flat by definition). ``admin_tier_overrides`` is
  currently empty in prod (no code writes to it yet), so its
  column drop is a no-op on data.

* **Asymmetric reversal.** ``downgrade()`` re-adds the columns
  with the same nullable + CHECK + index shape Arc 5 Revision A
  established, and runs the same ``UPDATE subscriptions SET
  billing_model='flat'`` backfill. ``admin_tier_overrides``
  re-add does NOT backfill (the table has no rows in prod and
  the column was nullable on creation). This is the standard
  Alembic posture: re-add with the column's original shape, do
  not attempt to reconstruct historical 'hybrid' values that
  the Arc 7 doctrine pivot retired.

Drift closure:

* Closes ``D-enterprise-metering-not-implemented-2026-05-22`` (P1)
  by promoting the drift to **retired-not-shipped** at Arc 7
  Commit 11 — the hybrid/metered shape will never ship.
"""

# Revision: arc7_a_retire_billing_model
# Revises: arc6_c_pending_downgrade_columns
# Create Date: 2026-05-24

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "arc7_a_retire_billing_model"
down_revision = "arc6_c_pending_downgrade_columns"
branch_labels = None
depends_on = None


# -----------------------------------------------------------------------------
# Upgrade — drop billing_model from subscriptions and admin_tier_overrides
# -----------------------------------------------------------------------------
def upgrade() -> None:
    # 1. subscriptions.billing_model — drop CHECK, drop index, drop column.
    #    Reverse order from Arc 5 Revision A creation (which created
    #    column → index → CHECK).
    op.drop_constraint(
        "ck_subscriptions_billing_model_valid",
        "subscriptions",
        type_="check",
    )
    op.drop_index(
        "ix_subscriptions_billing_model",
        table_name="subscriptions",
    )
    op.drop_column("subscriptions", "billing_model")

    # 2. admin_tier_overrides.billing_model — drop CHECK, drop column.
    #    Arc 5 Revision A did not create an index on this column (the
    #    table is forward-architecture; the column was a mirror of the
    #    subscriptions value for regulator-readability per
    #    arc5_a_admin_instance_additive.py:604), so there is no index
    #    to drop here. The CHECK shares the same legal-values
    #    expression as the subscriptions CHECK and is dropped the same
    #    way.
    op.drop_constraint(
        "ck_admin_tier_overrides_billing_model_valid",
        "admin_tier_overrides",
        type_="check",
    )
    op.drop_column("admin_tier_overrides", "billing_model")


# -----------------------------------------------------------------------------
# Downgrade — re-add the columns with their Arc 5 Revision A shape
# -----------------------------------------------------------------------------
def downgrade() -> None:
    # 2. admin_tier_overrides.billing_model — re-add nullable column +
    #    CHECK. No backfill: the table is empty in prod (no code path
    #    writes to it) and the column was nullable on original
    #    creation.
    op.add_column(
        "admin_tier_overrides",
        sa.Column(
            "billing_model",
            sa.String(length=16),
            nullable=True,
            comment=(
                "Mirrors subscriptions.billing_model so a regulator "
                "reading admin_tier_overrides sees the buyer-facing "
                "shape without joining subscriptions. NULL = inherit "
                "from subscriptions row."
            ),
        ),
    )
    op.create_check_constraint(
        "ck_admin_tier_overrides_billing_model_valid",
        "admin_tier_overrides",
        "billing_model IS NULL OR billing_model IN ('flat', 'hybrid', 'consumption')",
    )

    # 1. subscriptions.billing_model — re-add nullable column + index +
    #    CHECK, then re-run the Arc 5 Revision A backfill so existing
    #    rows are not left NULL after a downgrade. We do NOT attempt
    #    to reconstruct 'hybrid' on Enterprise rows: the Arc 7
    #    doctrine pivot retired that shape and no on-disk record of
    #    which rows would have been hybrid exists (the column was
    #    backfilled to 'flat' for every row at Arc 5 Revision A
    #    creation time, and no code path has flipped any row since).
    op.add_column(
        "subscriptions",
        sa.Column(
            "billing_model",
            sa.String(length=16),
            nullable=True,
            comment=(
                "Billing shape for this subscription. 'flat' = "
                "fixed-recurring (Pro + Enterprise post-Arc-7). "
                "'hybrid' = floor + metered-overage (RETIRED Arc 7). "
                "'consumption' = pure-metered (reserved; never "
                "shipped). NULL is legal only for soft-deleted rows "
                "predating Arc 5 Revision A."
            ),
        ),
    )
    op.create_index(
        "ix_subscriptions_billing_model",
        "subscriptions",
        ["billing_model"],
        unique=False,
    )
    op.execute(
        "UPDATE subscriptions SET billing_model = 'flat' WHERE billing_model IS NULL"
    )
    op.create_check_constraint(
        "ck_subscriptions_billing_model_valid",
        "subscriptions",
        "billing_model IS NULL OR billing_model IN ('flat', 'hybrid', 'consumption')",
    )
