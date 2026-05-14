"""Step 30a.2: deactivated_at columns + retention-scan index.

Revision ID: dfea1a04e037
Revises: c2a1b9f30e15
Create Date: 2026-05-14

Why this migration exists
-------------------------

Step 30a.2 closes three coupled gaps that surfaced together when we
audited the cancellation-to-purge lifecycle on 2026-05-14:

  D-cancellation-cascade-incomplete-conversations-claims-2026-05-14
      ``deactivate_tenant_with_cascade`` walked 7 layers leaf-first
      (memory_items -> api_keys -> luciel_instances -> agents ->
      agent_configs -> domain_configs -> tenant_config) but never
      touched ``conversations`` or ``identity_claims`` even though
      both carry ``tenant_id`` + ``active`` columns. A cancelled
      tenant left conversation rows + identity_claims rows
      ``active=true`` in the database -- a soft-delete invariant
      violation (Invariant 3) and a PIPEDA Principle 5 exposure.

  D-no-retention-worker-pipeda-principle-5-2026-05-14
      PIPEDA Principle 5 ("Limiting Use, Disclosure, and Retention")
      requires that personal data be destroyed when no longer needed
      for the purpose it was collected for. We had a soft-delete
      cascade but no SCHEDULED hard-delete -- soft-deactivated tenant
      data lived forever. Step 30a.2 ships a Celery-beat retention
      worker that hard-deletes tenants whose ``active=false`` flip
      happened more than 90 days ago. To scan efficiently the worker
      needs an indexed ``(active, deactivated_at)`` predicate on the
      tenant-row table, which today is ``tenant_configs``.

  D-trial-policy-mixed-per-tier-2026-05-14
      Step 30a.1 shipped a tier-varying trial (Individual monthly
      14d, Team/Company monthly 7d, all annual 0d). Step 30a.2 lifts
      that to a uniform $100/3-month paid intro across all six
      (tier, cadence) primitives. The schema change for this lives
      entirely in code (TRIAL_DAYS table -> single constant; intro_fee
      Price; first-time gate) -- this migration intentionally adds
      NO column for it. Recording the drift here so the
      cancellation/retention/trial cluster ships as one observable
      atomic unit in §12.

What this migration adds
------------------------

* ``tenant_configs.deactivated_at TIMESTAMP WITH TIME ZONE NULL`` --
  set by ``deactivate_tenant_with_cascade`` at the moment the
  ``tenant_config.active`` flag flips from True to False. Read by
  the retention worker to compute "how long ago was this tenant
  deactivated?". TIMESTAMP WITH TIME ZONE matches the discipline
  established by ``conversations.last_activity_at`` and
  ``conversations.created_at`` -- never store naive timestamps for
  retention math, since the worker runs in UTC and the cascade may
  be triggered from a request in Markham time.

* ``conversations.deactivated_at TIMESTAMP WITH TIME ZONE NULL`` --
  stamped alongside ``conversations.active=false`` so that any future
  per-row retention question (e.g. "which conversations in this
  tenant were deactivated more than N days ago?") has the same shape
  as the tenant-level scan. Today the retention worker only consults
  ``tenant_configs.deactivated_at`` -- this column is forward-looking
  symmetry, not load-bearing yet.

* ``identity_claims.deactivated_at TIMESTAMP WITH TIME ZONE NULL`` --
  same rationale as conversations. Identity claims are PII (claim_value
  may be an email address or phone number) so honoring their
  deactivation time individually is the right shape for PIPEDA-grade
  future audits even though today's worker scans at tenant granularity.

* ``ix_tenant_configs_active_deactivated_at`` partial-friendly composite
  index on ``tenant_configs(active, deactivated_at)`` -- this is the
  single hot-path query the retention worker runs nightly:
  ``WHERE active = false AND deactivated_at < now() - INTERVAL '90 days'``.
  Without the index every nightly run is a sequential scan over the
  full ``tenant_configs`` table. With the index the planner can use
  the leading ``active`` column to narrow to inactive tenants and the
  trailing ``deactivated_at`` column to range-scan the cutoff.

Design decisions worth recording
--------------------------------

* **No new column on `sessions`.** Sessions don't carry a soft-delete
  shape today -- ``SessionModel`` (app/models/session.py) has no
  ``active`` column, only a ``status`` string for runtime state.
  Adding ``sessions.deactivated_at`` would invent a new lifecycle layer
  that no read path honors. Sessions die at retention-time hard-purge
  via ``DELETE FROM sessions WHERE tenant_id=:tid``, with messages
  following via the existing ``ON DELETE CASCADE`` FK
  (``messages.session_id``). See §2 of the design plan for the full
  trace.

* **No new column on `messages`.** ``MessageModel`` is the only data-
  bearing table in the platform with no ``active`` flag and no
  ``deactivated_at`` -- messages live purely as children of sessions
  via SQL FK CASCADE. We honor that. The retention worker never
  touches messages directly; deleting their parent ``sessions`` row
  is sufficient.

* **`tenant_configs` is the retention root, not `tenants`.** There is
  no separate ``tenants`` table in this codebase. ``tenant_configs``
  serves both as configuration storage and as the unique identity
  row keyed by ``tenant_id String(100)``. The cascade already flips
  ``tenant_configs.active`` at the end; this migration adds the
  timestamp it should stamp alongside.

* **Pattern E discipline.** Additive only; all three columns are
  NULLable with no default. Existing rows remain valid the moment
  the ALTER TABLE finishes. No backfill is required because pre-
  existing inactive rows (if any) lack a deactivation timestamp by
  definition -- the retention worker skips rows where
  ``deactivated_at IS NULL`` (also indexed via the composite, since
  the planner treats NULL as comparable).

* **TIMESTAMP WITH TIME ZONE (timestamptz), not naive timestamp.** PG
  stores all timestamptz internally as UTC microseconds since epoch;
  the timezone metadata is rendered at query time. This is the right
  shape for cross-timezone retention math (cascade fires in Markham
  local time via FastAPI request thread; worker scans in UTC). Naive
  timestamps would silently mix the two and create an undetectable
  off-by-an-offset drift at every DST transition.

Rollback (downgrade) drops the index first, then the three columns in
reverse order. Destructive -- the timestamp values themselves are
lost, but they are reconstructible from AdminAuditLog rows since the
cascade emits an audit row at every deactivation.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "dfea1a04e037"
down_revision = "c2a1b9f30e15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add deactivated_at on (tenant_configs, conversations, identity_claims).

    Plus the retention-worker composite index on tenant_configs.

    All three columns are NULLable with no server_default. The cascade
    code is responsible for stamping ``deactivated_at = func.now()``
    in the same UPDATE that flips ``active=false``.
    """
    op.add_column(
        "tenant_configs",
        sa.Column(
            "deactivated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Wall-clock time the tenant was soft-deactivated. "
                "Stamped by deactivate_tenant_with_cascade in the same "
                "UPDATE that flips active=false. Read by the nightly "
                "retention worker to compute the 90d purge cutoff. "
                "NULL on rows that have never been deactivated (the "
                "vast majority); NULL also on rows deactivated before "
                "Step 30a.2 (no backfill -- those tenants are excluded "
                "from automated purge until next deactivation, by design)."
            ),
        ),
    )

    op.add_column(
        "conversations",
        sa.Column(
            "deactivated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Wall-clock time this conversation row was soft-"
                "deactivated. Stamped alongside active=false during "
                "tenant cascade. Currently load-bearing only for "
                "future per-conversation retention queries; the "
                "retention worker scans at tenant_configs granularity."
            ),
        ),
    )

    op.add_column(
        "identity_claims",
        sa.Column(
            "deactivated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Wall-clock time this identity_claim row was soft-"
                "deactivated. Symmetric with conversations.deactivated_at. "
                "claim_value may be PII (email / phone) so per-row "
                "timestamps are the right granularity for future "
                "PIPEDA audits even though today's worker scans at "
                "tenant_configs granularity."
            ),
        ),
    )

    # Retention-worker hot-path index. Single SELECT per nightly run:
    #   SELECT tenant_id FROM tenant_configs
    #   WHERE active = false AND deactivated_at < now() - INTERVAL '90 days'
    # Composite (active, deactivated_at) matches the leading-column rule:
    # equality on active first, range on deactivated_at second.
    op.create_index(
        "ix_tenant_configs_active_deactivated_at",
        "tenant_configs",
        ["active", "deactivated_at"],
    )


def downgrade() -> None:
    """Reverse the Step 30a.2 columns + index.

    Order matters: drop the index BEFORE the columns it references.
    Destructive on the timestamp values themselves, but those are
    reconstructible from AdminAuditLog rows (the cascade emits an
    audit row at every deactivation step).
    """
    op.drop_index(
        "ix_tenant_configs_active_deactivated_at",
        table_name="tenant_configs",
    )
    op.drop_column("identity_claims", "deactivated_at")
    op.drop_column("conversations", "deactivated_at")
    op.drop_column("tenant_configs", "deactivated_at")
