"""add memory_items actor_user_id

Revision ID: <generated -- keep what alembic produced>
Revises: 3ad39f9e6b55
Create Date: <generated>

Step 24.5b -- Commit 2 (services). Adds memory_items.actor_user_id as a
nullable UUID FK to users.id (ON DELETE RESTRICT) plus a btree index
for the hot-path "show me all memory rows attributable to User X
across role changes" query (Pillar 12 in Commit 3 will exercise this).

Hand-written per Invariant 12. Single-phase additive change. Closes
the model-vs-DB column drift Pillar 9 detected after File 2.6a
declared the column.

Drift item D9 closes here: the legacy MemoryRepository.save_memory
fallback path that was 500'ing against the unmigrated DB now writes
cleanly. ChatService's hot path uses upsert_by_message_id so chat
turns were never affected by the gap.

ON DELETE RESTRICT protects identity history -- a User cannot be
hard-deleted while their MemoryItems reference them. Soft-delete via
User.active=False is the only lifecycle path. Same doctrine as
agents.user_id from File 1.9.

Nullable in this commit. Backfilled by the Commit 3 backfill script
(scripts/backfill_user_id.py) and flipped to NOT NULL in Commit 3's
migration alongside agents.user_id flip (Invariant 12).

Pre-flight (verified before commit):
- Local Alembic chain: 3ad39f9e6b55 (head from Commit 1) -> this rev
- pgcrypto extension already created by File 1.9 (gen_random_uuid()
  available; we don't need a server_default on this column though
  -- it's a backfill target, not a generator).

Verified replayable against fresh DB before commit.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '4e989b9392c0'
down_revision = '3ad39f9e6b55'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add the column. Nullable, no server_default -- this is a
    # backfill-target column, not an auto-populated one. ON DELETE
    # RESTRICT mirrors the agents.user_id FK pattern from File 1.9
    # for identity-history protection.
    op.add_column(
        "memory_items",
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "users.id",
                ondelete="RESTRICT",
                name="fk_memory_items_actor_user_id_users",
            ),
            nullable=True,
        ),
    )

    # Btree index for the hot-path "all memory by User X" query that
    # Pillar 12 (Commit 3) exercises and that future PIPEDA access
    # flows will use to walk all memory rows for a given platform
    # User identity. Without the index this query is a sequential
    # scan once memory_items grows past a few thousand rows.
    op.create_index(
        "ix_memory_items_actor_user_id",
        "memory_items",
        ["actor_user_id"],
        unique=False,
    )


def downgrade() -> None:
    """Reverse the addition cleanly. Drop index first (depends on the
    column existing in some PG versions), then the FK constraint, then
    the column itself.

    DROP INDEX IF EXISTS guard for partial-failure replay safety,
    matching the same pattern File 1.9's downgrade uses.
    """
    op.execute("DROP INDEX IF EXISTS ix_memory_items_actor_user_id")
    op.drop_constraint(
        "fk_memory_items_actor_user_id_users",
        "memory_items",
        type_="foreignkey",
    )
    op.drop_column("memory_items", "actor_user_id")