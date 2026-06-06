"""add_user_consents_table

Revision ID: 107ed978475b
Revises: 8f88dbaf4ee7
Create Date: 2026-04-16 17:07:37.629934

Step 22 — PIPEDA user consent.

Backfilled 2026-04-19: the original commit shipped with empty upgrade()/
downgrade() bodies (autogenerate silently produced no-ops, likely
tripped by the pgvector `vector` type on knowledge_embeddings). The
table was created manually to match the model, then this migration
was rewritten by hand so fresh DBs get the same schema through the
normal alembic upgrade path.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '107ed978475b'
down_revision = '8f88dbaf4ee7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_consents",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=100), nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("consent_type", sa.String(length=50), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False),
        sa.Column("collection_method", sa.String(length=50), nullable=True),
        sa.Column("consent_text", sa.Text(), nullable=True),
        sa.Column("consent_context", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_user_consents_user_id", "user_consents", ["user_id"], unique=False
    )
    op.create_index(
        "ix_user_consents_tenant_id", "user_consents", ["tenant_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_user_consents_tenant_id", table_name="user_consents")
    op.drop_index("ix_user_consents_user_id", table_name="user_consents")
    op.drop_table("user_consents")