"""Arc 12 EX3 — drop superseded ``api_keys.domain_id`` / ``api_keys.agent_id``.

Revision ID: arc12_ex3_drop_api_key_agent_domain
Revises: arc12_ex3_drop_memory_agent_id
Create Date: 2026-05-29

Context
-------
Per the Arc 12 excision plan, EX1a stopped the key-mint layer from
writing meaningful values into ``api_keys.domain_id`` and
``api_keys.agent_id`` (new rows are inserted with ``None``). EX1c
dropped both fields from public API contracts / Pydantic schemas, and
EX2 re-sealed every live RLS policy off ``admin_id`` (+
``luciel_instance_id``); no policy references these two columns. They
are NOT carried by the admin_audit_logs hash chain. With nothing live
reading or writing them on ``api_keys`` anymore, it is safe to drop.

The create migration ``edb185277456_add_api_keys_table`` did not
create an index on ``domain_id``, and ``8b896ecd5881_add_agent_id_to_
api_keys_and_traces`` added ``agent_id`` without an index either —
nothing to drop before the column drop. Downgrade re-adds both
columns as nullable ``String(100)``, matching the pre-EX3 shape.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "arc12_ex3_drop_api_key_agent_domain"
down_revision = "arc12_ex3_drop_memory_agent_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("api_keys", "agent_id")
    op.drop_column("api_keys", "domain_id")


def downgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("domain_id", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "api_keys",
        sa.Column("agent_id", sa.String(length=100), nullable=True),
    )
