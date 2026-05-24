"""Arc 6 — Revision C: pending_downgrade columns.

Adds the schema scaffolding for Commit 8.5b's downgrade paths
(Pro→Free, Ent→Free, Ent→Pro-via-cancel-and-email-rebuy).

Design context (CANONICAL §17 Commit 8.5b lock):

* **Timing model = deferred.** All paid-tier downgrades fire
  ``stripe.Subscription.modify(cancel_at_period_end=True)`` and the
  actual tier flip + overflow archive runs at ``subscription.deleted``
  webhook time (i.e. at ``current_period_end``). The buyer keeps the
  entitlements they paid for until the boundary; they can re-upgrade
  before the boundary to undo the schedule.

* **Pending-target marker** lives on ``subscriptions.pending_downgrade_target``.
  This is the column the webhook ``_on_subscription_deleted`` branches
  on: when set, run the V2 downgrade path (flip admin tier + archive
  overflow); when unset, run the V1 hard-cancel deactivate path
  (preserved for manual cancels from the Stripe Dashboard).

* **Overflow policy = LRU soft-archive with rehydrate window.**
  When a downgrade boundary lands and the admin holds more of any
  cap'd resource than the destination tier allows (instances, embed
  keys, custom-domain CNAMEs, seats), the *least-recently-updated*
  overflow rows are soft-archived (``active=false``) and stamped with
  ``pending_downgrade_archived_at``. The stamp is what lets a
  re-upgrade within ``audit_retention`` (Free=30d) rehydrate the same
  rows rather than forcing the admin to recreate them. After the
  retention window the rows can be hard-collected by a future janitor.

* **Seats use the existing soft-delete column.** ``scope_assignments``
  already carries ``ended_at`` + ``end_reason`` (Pattern E from Arc 5),
  so seat archives reuse those columns with a new
  ``end_reason='downgrade_overflow_archive'`` literal — no new column
  on that table.

Columns added by this revision:

1. ``subscriptions.pending_downgrade_target VARCHAR(16) NULL``
   * CHECK constraint: ``pending_downgrade_target IN ('free','pro')``.
     Enterprise is the top tier so it is never a downgrade target.
     NULL = no downgrade scheduled (the common case).

2. ``instances.pending_downgrade_archived_at TIMESTAMPTZ NULL``
   * Stamp set when this Instance is one of the LRU losers at a
     downgrade boundary. NULL = not archived for downgrade reasons.

3. ``api_keys.pending_downgrade_archived_at TIMESTAMPTZ NULL``
   * Same shape; applies only to rows with ``key_kind='embed'``
     (admin keys are not capped — they are platform-internal).

4. ``admin_widget_domains.pending_downgrade_archived_at TIMESTAMPTZ NULL``
   * Same shape; CNAMEs are Pro-or-better entitlement.

Enum extension:

5. ``scope_assignment_end_reason`` PG ENUM gains a new value
   ``DOWNGRADE_OVERFLOW_ARCHIVE``. This is the literal stamped into
   ``scope_assignments.ended_reason`` when a seat is archived at a
   downgrade boundary (existing literals: PROMOTED, DEMOTED,
   REASSIGNED, DEPARTED, DEACTIVATED). The seat archive reuses the
   existing soft-delete columns (``ended_at`` + ``ended_reason`` +
   ``active=false``) per Pattern E — no new column on this table.

Decision D-arc6-c8.5b-downgrade-schema-2026-05-23 (recorded in
CANONICAL_RECAP §17 Commit 8.5b entry):

* **Four columns, not one polymorphic table.** A single
  ``pending_downgrade_archives`` table keyed by (entity_type,
  entity_id) was considered and rejected: the entity-type column
  would need a CHECK + a switch in every reader, and the JOIN cost
  on every cap-check query (\"how many active instances does this
  admin have?\") would dominate the savings. A per-table column is
  a stamp, not a row — readers that already filter by ``active``
  pay zero extra cost.

* **NULL default, no backfill.** This revision lands BEFORE Commit
  8.5b's service code, so no rows are in a \"pending downgrade\"
  state at migration time. NULL is the schema-honest default.

* **No index.** The stamp is read on a per-admin basis only when
  a re-upgrade rehydrates archives — never as a hot \"find all
  pending archives across the platform\" query.

Revision: arc6_c_pending_downgrade_columns
Revises: arc6_b_users_email_verified
Create Date: 2026-05-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "arc6_c_pending_downgrade_columns"
down_revision = "arc6_b_users_email_verified"
branch_labels = None
depends_on = None


# -----------------------------------------------------------------------------
# Upgrade
# -----------------------------------------------------------------------------
def upgrade() -> None:
    # 0. Extend the scope_assignment_end_reason PG ENUM. The literal
    #    DOWNGRADE_OVERFLOW_ARCHIVE is stamped into
    #    scope_assignments.ended_reason when a seat is archived at a
    #    downgrade boundary.
    #
    #    Implementation note: on PostgreSQL >= 12, ALTER TYPE ... ADD
    #    VALUE IF NOT EXISTS can run inside a transaction block as long
    #    as the new value is not USED in the same transaction. We add
    #    the value here but do not insert any rows that reference it
    #    in this same migration, so this is safe. Production target is
    #    PostgreSQL 16 (RDS engine pin), so the precondition holds.
    #    IF NOT EXISTS makes the statement idempotent under Alembic
    #    replay.
    op.execute(
        "ALTER TYPE scope_assignment_end_reason "
        "ADD VALUE IF NOT EXISTS 'DOWNGRADE_OVERFLOW_ARCHIVE'"
    )

    # 1. subscriptions.pending_downgrade_target — the marker the webhook
    #    branches on. NULL = no downgrade scheduled. Constrained to the
    #    two legal targets ('free', 'pro') by a CHECK; Enterprise is the
    #    top tier and can never be a downgrade target.
    op.add_column(
        "subscriptions",
        sa.Column(
            "pending_downgrade_target",
            sa.String(length=16),
            nullable=True,
            comment=(
                "Arc 6 Commit 8.5b — set when buyer schedules a tier "
                "downgrade via POST /billing/downgrade. NULL = no "
                "downgrade pending. Webhook _on_subscription_deleted "
                "branches on this column: set => V2 downgrade path "
                "(flip admin tier + archive overflow); NULL => V1 "
                "hard-cancel deactivate path (preserved for manual "
                "Stripe Dashboard cancels)."
            ),
        ),
    )
    op.create_check_constraint(
        "ck_subscriptions_pending_downgrade_target_legal",
        "subscriptions",
        "pending_downgrade_target IS NULL "
        "OR pending_downgrade_target IN ('free', 'pro')",
    )

    # 2. instances.pending_downgrade_archived_at — stamp set by the
    #    overflow archive logic when this Instance is one of the LRU
    #    losers at a downgrade boundary. Pairs with the existing
    #    instances.active soft-delete column (set to false at the same
    #    time). NULL = not archived for downgrade reasons.
    op.add_column(
        "instances",
        sa.Column(
            "pending_downgrade_archived_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Arc 6 Commit 8.5b — timestamp when this Instance was "
                "soft-archived as overflow at a downgrade boundary. "
                "Pairs with active=false. Re-upgrade within the "
                "audit_retention window (Free=30d) rehydrates rows "
                "that still carry this stamp."
            ),
        ),
    )

    # 3. api_keys.pending_downgrade_archived_at — applies to embed keys
    #    (key_kind='embed') which are capped per tier. Admin keys are
    #    platform-internal and uncapped, so the column is harmless on
    #    those rows (stays NULL).
    op.add_column(
        "api_keys",
        sa.Column(
            "pending_downgrade_archived_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Arc 6 Commit 8.5b — timestamp when this embed-key row "
                "(key_kind='embed') was soft-archived as overflow at a "
                "downgrade boundary. Pairs with active=false. Admin "
                "keys are uncapped and never carry this stamp."
            ),
        ),
    )

    # 4. admin_widget_domains.pending_downgrade_archived_at — CNAMEs are
    #    a Pro-and-Enterprise entitlement, so a Pro→Free downgrade
    #    archives ALL of an admin's CNAMEs (Free cap = 0). Ent→Pro
    #    downgrade archives down to Pro's cap (1).
    op.add_column(
        "admin_widget_domains",
        sa.Column(
            "pending_downgrade_archived_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Arc 6 Commit 8.5b — timestamp when this widget-domain "
                "CNAME was soft-archived as overflow at a downgrade "
                "boundary. Free cap = 0 (all archived on Pro→Free); "
                "Pro cap = 1; Enterprise cap = unlimited."
            ),
        ),
    )


# -----------------------------------------------------------------------------
# Downgrade
# -----------------------------------------------------------------------------
def downgrade() -> None:
    # NOTE: PostgreSQL does not support dropping a single ENUM value.
    # The 'DOWNGRADE_OVERFLOW_ARCHIVE' literal added to
    # scope_assignment_end_reason in upgrade() is left in place on
    # downgrade. This is the standard Alembic posture for PG enums;
    # a follow-up revision could rebuild the ENUM via CREATE / ALTER
    # TABLE / DROP / RENAME if the value ever needs hard removal.
    op.drop_column("admin_widget_domains", "pending_downgrade_archived_at")
    op.drop_column("api_keys", "pending_downgrade_archived_at")
    op.drop_column("instances", "pending_downgrade_archived_at")
    op.drop_constraint(
        "ck_subscriptions_pending_downgrade_target_legal",
        "subscriptions",
        type_="check",
    )
    op.drop_column("subscriptions", "pending_downgrade_target")
