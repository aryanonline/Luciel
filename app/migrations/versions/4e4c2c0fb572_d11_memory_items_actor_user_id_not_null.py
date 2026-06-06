"""d11 memory_items actor_user_id NOT NULL flip

Revision ID: 4e4c2c0fb572
Revises: 4e989b9392c0
Create Date: 2026-04-27

Step 28 -- Phase 1, Commit 5. Closes drift item D11 from the Step 24.5b
canonical recap by flipping memory_items.actor_user_id from nullable to
NOT NULL.

This migration completes a deferral named explicitly in the docstring of
the Step 24.5b additive migration (4e989b9392c0_add_memory_items_actor_
user_id.py), which stated:

    "Nullable in this commit. Backfilled by the Commit 3 backfill script
     (scripts/backfill_user_id.py) and flipped to NOT NULL in Commit 3's
     migration alongside agents.user_id flip (Invariant 12)."

The 24.5b NOT NULL flip never landed -- it was deferred to the post-24.5b
sweep. Step 28 Phase 1 is that sweep.

Pre-flight orphan sweep (executed before this migration):
- 10 historical orphan rows had actor_user_id IS NULL.
- All 10 were pre-architecture local-dev test residue:
    - NULL actor_user_id, NULL luciel_instance_id, NULL message_id
    - No session linkage (orphaned message_id chain)
    - Created 2026-04-13 to 2026-04-16, before Step 25b LucielInstance-
      centered architecture
    - Tenants: demo-tenant (3), remax-sarah (2), remax-crossroads (5)
- Hard-deleted in a single transaction with admin_audit_logs row id=2086
  recording the sweep (action='sweep', resource_type='memory_items',
  actor_label='step28-d11-orphan-sweep', before_json captures all 10 ids
  and tenant breakdown).
- Forensic anchor: admin_audit_logs.id = 2086.
- Post-sweep state verified: total=0, null_count=0.

Hand-written per Invariant 12. memory_items has pgvector + JSONB columns
that ban alembic --autogenerate for this table.

Single-phase nullability flip. The FK constraint
(fk_memory_items_actor_user_id_users, ON DELETE RESTRICT) and the index
(ix_memory_items_actor_user_id) added in 4e989b9392c0 are preserved --
op.alter_column changes nullability only.

Prod safety:
- Prod runs durable identity layer since Step 24.5b shipped. Prod chat
  turns populate actor_user_id correctly via the post-24.5b code path.
- Prod orphan count must be verified == 0 before this migration runs in
  prod (Phase 1 6-phase rollout runbook will gate on this).
- Local DB at this point: memory_items is empty (the 10 orphans were the
  only rows). The NOT NULL constraint holds trivially against empty
  tables and against any future correctly-attributed memory write.

Verified replayable: upgrade -> downgrade -> upgrade against the
post-sweep local DB before commit.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = '4e4c2c0fb572'
down_revision = '28a3f1c0e9b2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NOT NULL flip. FK and index untouched.
    op.alter_column(
        "memory_items",
        "actor_user_id",
        nullable=False,
    )


def downgrade() -> None:
    """Restore nullability. FK and index untouched.

    Note: downgrade does NOT recreate the orphan rows that the pre-flight
    sweep removed -- those rows had no recoverable identity and their
    deletion is recorded forensically in admin_audit_logs.id=2086. The
    downgrade exists to permit emergency rollback of the NOT NULL
    constraint itself, not to undo the data sweep.
    """
    op.alter_column(
        "memory_items",
        "actor_user_id",
        nullable=True,
    )
