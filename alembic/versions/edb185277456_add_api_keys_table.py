"""add api_keys table

Revision ID: edb185277456
Revises: b0e003ffa07f
Create Date: 2026-04-13 17:32:38.071322

Adds the api_keys table and its indexes.

Backfilled 2026-04-19: original autogenerate included a spurious
`drop_index(ix_knowledge_embedding_vector)` + `drop_column(embedding)`
pair because Alembic didn't recognize the pgvector `vector(1536)` type
declared via raw ALTER TABLE in b0e003ffa07f. Those two DROPs have been
removed. The downgrade's matching re-add (with sa.NullType()) has also
been removed, since the embedding column was never supposed to be
dropped in the first place.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'edb185277456'
down_revision = 'b0e003ffa07f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'api_keys',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('key_hash', sa.String(length=128), nullable=False),
        sa.Column('key_prefix', sa.String(length=20), nullable=False),
        sa.Column('tenant_id', sa.String(length=100), nullable=False),
        sa.Column('domain_id', sa.String(length=100), nullable=True),
        sa.Column('display_name', sa.String(length=200), nullable=False),
        sa.Column('permissions', sa.JSON(), nullable=False),
        sa.Column('rate_limit', sa.Integer(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_by', sa.String(length=100), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_api_keys_key_hash'), 'api_keys', ['key_hash'], unique=True
    )
    op.create_index(
        op.f('ix_api_keys_tenant_id'), 'api_keys', ['tenant_id'], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_api_keys_tenant_id'), table_name='api_keys')
    op.drop_index(op.f('ix_api_keys_key_hash'), table_name='api_keys')
    op.drop_table('api_keys')