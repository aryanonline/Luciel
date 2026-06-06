"""Step 27b: memory_items.message_id + luciel_instance_id (async extraction idempotency)

Revision ID: 8e2a1f5b9c4d
Revises: 4d9c21f3e8a7
Create Date: 2026-04-24 19:00:00.000000

Adds two nullable columns to `memory_items` to support the Step 27b async
memory extraction worker:

  - `message_id INTEGER NULL` — FK to `messages.id` ON DELETE SET NULL.
    Idempotency key for the Celery task; populated only by the worker
    upsert path. Legacy sync rows keep NULL.

  - `luciel_instance_id INTEGER NULL` — FK to `luciel_instances.instance_id`
    ON DELETE SET NULL. Scope binding; populated only by the worker upsert
    path. Legacy sync rows keep NULL.

Plus a composite partial unique index that enforces idempotency at the DB
layer:

  - `ix_memory_items_tenant_message_unique ON (tenant_id, message_id)
    WHERE message_id IS NOT NULL`

Composite (tenant_id, message_id) per Invariant 13 (mandatory tenant
predicates) — two tenants cannot collision-block each other's message
ids. Partial WHERE clause means the index only enforces uniqueness on
the new worker-written rows; legacy NULL rows are unaffected.

Invariant 7: additive only. No backfill, no UPDATE on existing rows.
Invariant 12: hand-written; verified against fresh DB before commit.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic
revision = "8e2a1f5b9c4d"
down_revision = "4d9c21f3e8a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------- message_id ----------
    op.add_column(
        "memory_items",
        sa.Column(
            "message_id",
            sa.Integer(),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_memory_items_message_id",
        source_table="memory_items",
        referent_table="messages",
        local_cols=["message_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_memory_items_message_id",
        "memory_items",
        ["message_id"],
        unique=False,
    )

    # ---------- luciel_instance_id ----------
    op.add_column(
        "memory_items",
        sa.Column(
            "luciel_instance_id",
            sa.Integer(),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_memory_items_luciel_instance_id",
        source_table="memory_items",
        referent_table="luciel_instances",
        local_cols=["luciel_instance_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_memory_items_luciel_instance_id",
        "memory_items",
        ["luciel_instance_id"],
        unique=False,
    )

    # ---------- composite partial unique index (idempotency) ----------
    # Postgres-specific partial index: only enforces uniqueness when
    # message_id IS NOT NULL. Legacy rows (NULL) are unaffected.
    op.create_index(
        "ix_memory_items_tenant_message_unique",
        "memory_items",
        ["tenant_id", "message_id"],
        unique=True,
        postgresql_where=sa.text("message_id IS NOT NULL"),
    )


def downgrade() -> None:
    # Reverse order of upgrade.
    op.drop_index(
        "ix_memory_items_tenant_message_unique",
        table_name="memory_items",
    )

    op.drop_index(
        "ix_memory_items_luciel_instance_id",
        table_name="memory_items",
    )
    op.drop_constraint(
        "fk_memory_items_luciel_instance_id",
        "memory_items",
        type_="foreignkey",
    )
    op.drop_column("memory_items", "luciel_instance_id")

    op.drop_index(
        "ix_memory_items_message_id",
        table_name="memory_items",
    )
    op.drop_constraint(
        "fk_memory_items_message_id",
        "memory_items",
        type_="foreignkey",
    )
    op.drop_column("memory_items", "message_id")