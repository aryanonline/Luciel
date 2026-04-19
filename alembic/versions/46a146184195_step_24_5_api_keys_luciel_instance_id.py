"""step_24_5_api_keys_luciel_instance_id

Adds a nullable luciel_instance_id foreign-key column to api_keys so
a chat key can be bound to exactly one LucielInstance.

Design notes:
- Nullable. Every existing api_keys row stays NULL (unbound) after
  upgrade. No backfill required. Unbound keys continue to resolve
  their persona via the existing tenant/domain/agent config path at
  chat time.
- ON DELETE SET NULL. If the referenced LucielInstance is ever hard-
  deleted (not today — we only soft-deactivate — but keep the option
  open), the key survives with luciel_instance_id=NULL rather than
  cascade-dying. Orphaned keys are safer than missing keys.
- ondelete="RESTRICT" would have been stricter, but it would forbid
  any future hard-delete path. SET NULL is the middle ground.
- Index added so the middleware's per-request lookup stays O(log n).

Hand-written, not autogenerate (same discipline as File 8 — avoids
Alembic dropping the pgvector embedding column).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa



# revision identifiers, used by Alembic.
revision = '46a146184195'
down_revision = 'c957a155c325'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add the nullable FK column.
    op.add_column(
        "api_keys",
        sa.Column(
            "luciel_instance_id",
            sa.Integer(),
            sa.ForeignKey(
                "luciel_instances.id",
                ondelete="SET NULL",
                name="fk_api_keys_luciel_instance_id",
            ),
            nullable=True,
            comment=(
                "Optional pin to a specific LucielInstance. Chat keys "
                "bound to an instance can only talk to that one Luciel. "
                "Admin keys leave this NULL."
            ),
        ),
    )
    op.create_index(
        "ix_api_keys_luciel_instance_id",
        "api_keys",
        ["luciel_instance_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_api_keys_luciel_instance_id", table_name="api_keys")
    op.drop_constraint(
        "fk_api_keys_luciel_instance_id",
        "api_keys",
        type_="foreignkey",
    )
    op.drop_column("api_keys", "luciel_instance_id")