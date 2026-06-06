"""add_agent_config_and_agent_id_columns

Revision ID: b0b4a3861c4d
Revises: edb185277456
Create Date: 2026-04-14 19:50:13.553585

Adds agent_id columns and indexes on knowledge_embeddings and sessions.

Backfilled 2026-04-19: the original commit's autogenerate produced a
spurious `op.drop_column('knowledge_embeddings', 'embedding')` because
Alembic didn't recognize the pgvector `vector(1536)` type declared via
raw ALTER TABLE in b0e003ffa07f. That DROP has been removed. The
downgrade's matching re-add (with sa.NullType()) has also been removed,
since the embedding column was never supposed to be dropped in the
first place.

The existing production DB still has the embedding column (the drop
was harmless there because the migration has already run and the column
is preserved by being out of the model's migration history). This
backfill makes fresh DBs (incl. Step 26b production redeploy) land with
the correct schema.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b0b4a3861c4d'
down_revision = 'edb185277456'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'knowledge_embeddings',
        sa.Column('agent_id', sa.String(length=100), nullable=True),
    )
    op.create_index(
        op.f('ix_knowledge_embeddings_agent_id'),
        'knowledge_embeddings',
        ['agent_id'],
        unique=False,
    )
    op.add_column(
        'sessions',
        sa.Column('agent_id', sa.String(length=100), nullable=True),
    )
    op.create_index(
        op.f('ix_sessions_agent_id'),
        'sessions',
        ['agent_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_sessions_agent_id'), table_name='sessions')
    op.drop_column('sessions', 'agent_id')
    op.drop_index(
        op.f('ix_knowledge_embeddings_agent_id'),
        table_name='knowledge_embeddings',
    )
    op.drop_column('knowledge_embeddings', 'agent_id')