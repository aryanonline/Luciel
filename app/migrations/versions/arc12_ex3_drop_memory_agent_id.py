"""Arc 12 EX3 — drop superseded ``memory_items.agent_id``.

Revision ID: arc12_ex3_drop_memory_agent_id
Revises: arc12_ex3_drop_trace_agent_domain
Create Date: 2026-05-29

Context
-------
Per the Arc 12 excision plan, EX1 swept code-level references to the v1
agent layer (services/repositories no longer filter, write, or expose
``MemoryItem.agent_id``; new rows are inserted with ``agent_id=NULL``).
EX2 re-sealed every live RLS policy to ``admin_id`` (+
``luciel_instance_id``); no policy references ``memory_items.agent_id``.
The column is NOT in the admin_audit_logs hash chain. With nothing live
reading or writing it on ``memory_items`` anymore, it is safe to drop.

The original create migration (``92b3ce2809d4_add_agent_configs_table_
and_memory_``) added the column with index ``ix_memory_items_agent_id``;
the index is dropped before the column. Downgrade re-adds the column as
nullable ``String(100)`` and recreates the index, matching the pre-EX3
shape.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "arc12_ex3_drop_memory_agent_id"
down_revision = "arc12_ex3_drop_trace_agent_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        op.f("ix_memory_items_agent_id"),
        table_name="memory_items",
    )
    op.drop_column("memory_items", "agent_id")


def downgrade() -> None:
    op.add_column(
        "memory_items",
        sa.Column("agent_id", sa.String(length=100), nullable=True),
    )
    op.create_index(
        op.f("ix_memory_items_agent_id"),
        "memory_items",
        ["agent_id"],
        unique=False,
    )
