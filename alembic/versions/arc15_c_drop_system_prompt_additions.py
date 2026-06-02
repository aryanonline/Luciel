"""Arc 15 C — drop instances.system_prompt_additions (doctrine cleanup).

Revision ID: arc15_c_drop_system_prompt_additions
Revises: arc15_b_instance_connections
Create Date: 2026-06-02

Why this migration exists
-------------------------
Vision §3.5 / Architecture §3.5.1 ("the system prompt is NEVER written
by the customer … does not expose hooks for the admin to author
additional system-prompt stanzas") forbids a free-text raw-prompt
authoring layer. Arc 15 WU2 replaced the runtime use of
``instances.system_prompt_additions`` with the platform-composed
PRESET + BUSINESS_CONTEXT stanzas (``app/persona/composer.py``); the
chat_service composer no longer reads the column and the personality
API never writes it. The Arc 15 doctrine-alignment cleanup removes the
now-dead column end-to-end (model, schemas, services, repository,
route) — this migration drops the column from the live schema.

This chains off ``arc15_b_instance_connections`` (the prior head). The
earlier ``arc15_a_instance_config_pillars`` migration intentionally
LEFT the column in place (deprecation, not removal); that migration is
applied history and is NOT edited — this is the separate, additive
drop.

RLS posture
-----------
``instances`` keeps its tenant-isolation RLS unchanged; DROP COLUMN
does not touch the table's policies.

Rollback contract
------------------
``downgrade`` re-adds ``system_prompt_additions TEXT NULL`` — matching
the original ``arc9_c17_instances_system_prompt`` column shape — so the
upgrade/downgrade pair round-trips cleanly. Re-added rows carry NULL
(the dropped data is not recoverable, by design: the column is dead).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "arc15_c_drop_system_prompt_additions"
down_revision = "arc15_b_instance_connections"
branch_labels = None
depends_on = None


_TABLE = "instances"
_COLUMN = "system_prompt_additions"


def upgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)


def downgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.Text(), nullable=True),
    )
