"""Arc 11 Cleanup A — rename retention_policies.data_category
'knowledge_embeddings' → 'knowledge_chunks'.

Revision ID: arc11_cleanup_a_data_category_rename
Revises: arc11_d3_hnsw_index_chunks
Create Date: 2026-05-28

Why this migration exists
-------------------------

Arc 11 Step 2 renamed the underlying table from
``knowledge_embeddings`` to ``knowledge_chunks``, but the
``data_category`` identifier persisted in ``retention_policies``
rows stayed at the legacy string for backwards-compatibility with
any data loaded before the rename. ARC11_PLAN.md §13 (Step 2
carry-forward) flagged this as a Step 11 / paired-migration item.

The no-deferrals closeout (Cleanup A) lands the rename: the code
side now uses ``"knowledge_chunks"`` as the canonical
``data_category`` (see ``app/policy/retention.py``,
``app/schemas/retention.py``, ``app/policy/retention_rules.py``,
``app/services/onboarding_service.py``). This migration updates
any persisted rows in lockstep.

Production verification (ARC11_PLAN.md §12) showed zero customer
admins / instances / traces, so this UPDATE almost certainly
matches 0 rows in prod. The migration is correctness insurance
for any environment that ran the legacy code between plan-writing
and merge.

Rollback
--------

Symmetric: reverse the rename. Safe because the new code accepts
either name on read paths (the dict key is the only identifier and
either spelling resolves to the same row from the moment the
downgrade lands until the code redeploys).
"""
from __future__ import annotations

from alembic import op


# ---------------------------------------------------------------------
# Alembic identifiers.
# ---------------------------------------------------------------------
revision = "arc11_cleanup_a_data_category_rename"
down_revision = "arc11_d3_hnsw_index_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE retention_policies
           SET data_category = 'knowledge_chunks'
         WHERE data_category = 'knowledge_embeddings'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE retention_policies
           SET data_category = 'knowledge_embeddings'
         WHERE data_category = 'knowledge_chunks'
        """
    )
