"""Step 29.y Cluster 4 (E-2): worker rejection-audit idempotency index.

Revision ID: d8e2c4b1a0f3
Revises: c5d8a1e7b3f9
Create Date: 2026-05-07

Why this migration exists
-------------------------

findings_phase1e.md E-2 documents an ack-late race on SQS that can
produce duplicate ``worker_malformed_payload`` (and other
worker_*_reject) audit rows on the same SQS message:

  task_acks_late=True
  task_reject_on_worker_lost=True
  worker_prefetch_multiplier=1

If a worker is killed between writing the rejection audit row
(autocommit=True, see _reject_with_audit) and raising
Reject(requeue=False), the SQS message visibility timeout expires
and the message is redelivered. A new worker re-runs Gate 1
deterministically, re-writes the same rejection audit row -- the
audit row insertion is autocommit=True and not idempotent.

Phase 2 confirmed this in production via duplicate DLQ MessageIds
with recv_count=4/5/6 across three distinct messages. The fix is
a DB-level partial unique index on:

  (action, tenant_id, resource_natural_id)
  WHERE action LIKE 'worker_%' AND resource_natural_id IS NOT NULL

resource_natural_id for the worker-rejection rows is a
deterministic ``session={sid};message={mid}`` string composed in
``app/worker/tasks/memory_extraction.py:_reject_with_audit``, so
duplicate rejections of the same SQS payload always present the
same triple. The repository catches IntegrityError on this index
and returns silently (skip-on-conflict semantics), so the second
attempt produces no extra audit row.

Why a partial index, not a full one
-----------------------------------

Operational audit rows (deactivate, reactivate, retention enforce,
etc.) are not idempotent on (action, tenant_id, resource_natural_id)
-- a tenant can be deactivated and reactivated and deactivated
again, and each must produce a separate row. The partial filter
narrows the constraint to worker_*_reject classes, which ARE
idempotent by the message-id semantics of the SQS payload they
reject. resource_natural_id IS NOT NULL excludes the cascade-style
worker rows that don't carry a per-message identifier.

Why the index is forward-only (created_at cutoff)
--------------------------------------------------

A prior verification-harness regime (Pillars 11/13/26 against the
worker-reject path before this control existed) accumulated 223
strict-duplicate worker_* audit rows across 57 logical events on
verification-harness tenants only. Those rows pre-date this
control and would violate a naive partial UNIQUE constraint if it
were applied across the full table.

The correct disposition is documented under DISC-2026-003 in
``docs/DISCLOSURES.md`` (drift token
``D-audit-verification-harness-retry-duplicates-2026-05-07`` and
the redesign drift token
``D-cluster4-e2-rework-as-forward-only-2026-05-07``) and follows
Pattern E strictly: historical audit rows are NOT mutated,
including the historical duplicates. The control instead applies
from a fixed cutoff timestamp going forward, after which the
worker-reject path provably writes idempotently because the
partial UNIQUE index will reject any second write with an
IntegrityError that the repository catches and treats as a
benign skip-on-conflict.

The cutoff is anchored at 2026-05-08 04:00:00+00 (the wall-clock
moment immediately after the Step 29.y gap-fix C12 deploy window;
any row with a created_at strictly less than the cutoff is
outside the index's scope, including the 223 historical
duplicates and every legitimate prior worker_* row).

Why this is safe to add now
---------------------------

The index is created with IF NOT EXISTS so re-running the
migration on a database that already has it is a no-op. CREATE
INDEX ... CONCURRENTLY is NOT used here because alembic runs DDL
inside a transaction and CONCURRENTLY would error out;
admin_audit_logs is small enough at our current scale that a
brief table lock during index build is acceptable. If/when the
table grows beyond ~500k rows, switch the prod runbook to a
manual psql session that issues CREATE INDEX CONCURRENTLY out of
band before ``alembic upgrade head`` runs.

Idempotency
-----------

The partial index is named ``ux_admin_audit_logs_worker_reject_idem``
and uses IF NOT EXISTS on create so re-running this migration is
a no-op. The downgrade drops the same index name. An older,
non-date-bounded variant of this index that briefly existed on
the operator workstation during the 2026-05-07 gap-fix session
was dropped manually before this migration was authored; the
workstation state and the prod state are therefore both reachable
from the same forward upgrade path.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


# Revision identifiers, used by Alembic.
revision = "d8e2c4b1a0f3"
down_revision = "c5d8a1e7b3f9"
branch_labels = None
depends_on = None


_INDEX_NAME = "ux_admin_audit_logs_worker_reject_idem"

# Forward-only cutoff. Any admin_audit_logs row with created_at
# strictly less than this timestamp is outside the partial index
# scope; this preserves Pattern E by leaving the 223 historical
# verification-harness duplicate rows (DISC-2026-003) untouched
# while still enforcing idempotency on every worker-reject write
# from the deploy moment forward.
_FORWARD_CUTOFF_UTC = "2026-05-08 04:00:00+00"


def upgrade() -> None:
    # We use raw SQL for the partial index because Alembic's
    # op.create_index does not natively support partial-index
    # WHERE clauses across all dialects, and we want the IF NOT
    # EXISTS guard for redeploy-safety.
    op.execute(
        sa.text(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX_NAME}
            ON admin_audit_logs (action, tenant_id, resource_natural_id)
            WHERE action LIKE 'worker_%'
              AND resource_natural_id IS NOT NULL
              AND created_at >= TIMESTAMPTZ '{_FORWARD_CUTOFF_UTC}'
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text(f"DROP INDEX IF EXISTS {_INDEX_NAME}"))
