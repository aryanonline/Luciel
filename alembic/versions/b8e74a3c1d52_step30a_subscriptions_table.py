"""Step 30a: subscriptions table.

Revision ID: b8e74a3c1d52
Revises: 3dbbc70d0105
Create Date: 2026-05-13

Why this migration exists
-------------------------

Step 30a ships self-serve subscription billing for the Individual tier.
The Luciel backend is the source of truth for tenant entitlement; Stripe
is the payment surface. Every paying tenant needs exactly one row in
``subscriptions`` whose ``active`` flag and Stripe-mirrored ``status``
together answer two questions:

  1. "Is tenant X currently entitled to the platform?"  (the chat path)
  2. "What tier did tenant X have on date Y?"           (the audit chain)

The table is intentionally minimal — every field is either an identity
pointer (tenant_id, user_id, customer_email, the three stripe_* ids),
plan/state (tier, status, the four datetime columns + cancel_at_period_end
+ canceled_at), soft-delete control (active), or replay/forensics
(last_event_id, provider_snapshot). Anything richer (invoices, payments,
refunds) lives on Stripe and is fetched on demand — we don't double-book
financial state.

Design decisions worth recording in the migration body
------------------------------------------------------

* ``id BIGSERIAL`` (Integer PK with autoincrement) matches the rest of
  the platform's mutable tables. The Subscription row is mutable — the
  ``status`` column flips as Stripe events arrive — so it is NOT a
  candidate for the UUID-PK + immutable pattern used by ``users``.

* ``tenant_id VARCHAR(100)`` is a string FK to
  ``tenant_configs.tenant_id`` rather than the numeric PK because every
  other tenant-scoped table in the platform addresses tenants by their
  slug (ARCHITECTURE §4.1). No FK constraint at the DB layer because
  ``tenant_configs.tenant_id`` is itself indexed-but-not-PK on that
  table; the app layer enforces the binding inside
  ``OnboardingService.onboard_tenant()`` (which writes the
  ``tenant_configs`` row + the ``subscriptions`` row in the same
  transaction).

* ``user_id UUID`` is a real FK to ``users.id`` with
  ``ON DELETE RESTRICT`` so a misplaced cascade or a future GDPR
  ``RIGHT_TO_BE_FORGOTTEN`` purge cannot silently dismiss the billing
  audit trail. Hard-deletion of a paying user is a manual operation
  by design.

* ``customer_email VARCHAR(320)`` is a denormalized copy of
  ``users.email`` captured at row write time. ARCHITECTURE §4 lists
  email-stable identity as a P0 invariant; keeping a copy here means
  a forensic question of the form "who paid for tenant X on date Y"
  answers from the audit chain even if the ``users`` row is later
  re-parented or anonymized.

* Three Stripe identifier columns are stored as ``VARCHAR(64)``:
  Stripe's published id formats are at most 56 chars (``cus_*`` 19,
  ``sub_*`` 23, ``price_*`` 25, ``evt_*`` 27) plus the platform's
  Test-mode ``_test_`` prefix in the rare case. 64 is the safe upper
  bound and matches the existing ``api_keys.key_hash`` column width.

* ``stripe_subscription_id`` is UNIQUE so a replay of
  ``checkout.session.completed`` cannot mint two rows for the same
  Stripe subscription. The webhook handler also dedupes on
  ``last_event_id``, but the column-level UNIQUE is the
  defense-in-depth guarantee.

* ``provider_snapshot JSONB`` carries the most recent Stripe
  subscription object verbatim. Useful when reconciling a divergence
  between our local state and Stripe's view. Bounded by Stripe's own
  payload size (~10 KB for a single subscription).

* Two composite indexes match the two queries we care about most:
    - ``ix_subscriptions_tenant_active`` ``(tenant_id, active)`` — the
      entitlement lookup on every request.
    - ``ix_subscriptions_stripe_customer`` ``(stripe_customer_id)`` —
      the Customer Portal flow needs "which subscription does this
      Stripe customer have?".

Idempotency
-----------

Every CREATE statement uses ``IF NOT EXISTS`` so this migration is safe
to re-run on a database that already received it (workstation replays
against a hand-patched copy). ``downgrade()`` drops the table; the
``users`` and ``tenant_configs`` references stay because the FK is only
declared one-way.

What this migration does NOT do
-------------------------------

* No price/SKU table. Step 30a ships exactly one SKU (Individual);
  multi-SKU support lands in Step 30a.1 when annual + Team self-serve
  are added.
* No invoice/payment tables. Stripe owns those; we fetch on demand
  from the Customer Portal.
* No grant of permissions to ``luciel_worker``. The retention purge
  worker has no business touching subscription rows — every mutation
  goes through the webhook handler, which runs under the API role.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op


# Revision identifiers, used by Alembic.
revision = "b8e74a3c1d52"
down_revision = "3dbbc70d0105"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id BIGSERIAL PRIMARY KEY,

                -- Scope binding
                tenant_id VARCHAR(100) NOT NULL,

                -- Identity (email-stable per ARCHITECTURE §4)
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                customer_email VARCHAR(320) NOT NULL,

                -- Stripe identifiers
                stripe_customer_id VARCHAR(64) NOT NULL,
                stripe_subscription_id VARCHAR(64) NOT NULL UNIQUE,
                stripe_price_id VARCHAR(64) NOT NULL,

                -- Plan + state
                tier VARCHAR(32) NOT NULL,
                status VARCHAR(32) NOT NULL,

                -- Cycle
                current_period_start TIMESTAMPTZ NULL,
                current_period_end TIMESTAMPTZ NULL,
                trial_end TIMESTAMPTZ NULL,
                cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
                canceled_at TIMESTAMPTZ NULL,

                -- Pattern E soft-delete
                active BOOLEAN NOT NULL DEFAULT TRUE,

                -- Provider replay + forensic snapshot
                last_event_id VARCHAR(64) NULL,
                provider_snapshot JSONB NULL,

                -- TimestampMixin
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )

    # Per-column indexes that the model marks index=True. We declare
    # them explicitly here so Alembic's autogenerate stays consistent
    # against this hand-written migration; the model-side index=True
    # is advisory and harmless against an already-indexed column.
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_subscriptions_tenant_id ON subscriptions(tenant_id)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_subscriptions_user_id ON subscriptions(user_id)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_subscriptions_customer_email ON subscriptions(customer_email)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_subscriptions_stripe_customer_id ON subscriptions(stripe_customer_id)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_subscriptions_tier ON subscriptions(tier)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_subscriptions_status ON subscriptions(status)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_subscriptions_active ON subscriptions(active)"))

    # The two hot composite indexes called out in the model __table_args__.
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_subscriptions_tenant_active "
            "ON subscriptions(tenant_id, active)"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_subscriptions_stripe_customer "
            "ON subscriptions(stripe_customer_id)"
        )
    )

    op.execute(
        sa.text(
            "COMMENT ON TABLE subscriptions IS "
            "'Step 30a — Stripe subscription <-> Luciel tenant binding.'"
        )
    )


def downgrade() -> None:
    # Drop the composite indexes first so the table drop does not
    # silently take them along (Postgres does this automatically, but
    # being explicit makes a manual rollback in a forensic context
    # readable in a single grep).
    op.execute(sa.text("DROP INDEX IF EXISTS ix_subscriptions_stripe_customer"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_subscriptions_tenant_active"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_subscriptions_active"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_subscriptions_status"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_subscriptions_tier"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_subscriptions_stripe_customer_id"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_subscriptions_customer_email"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_subscriptions_user_id"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_subscriptions_tenant_id"))
    op.execute(sa.text("DROP TABLE IF EXISTS subscriptions"))
