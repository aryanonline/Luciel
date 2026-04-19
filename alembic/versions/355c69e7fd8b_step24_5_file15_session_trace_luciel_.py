"""step24_5_file15_session_trace_luciel_instance_id

Revision ID: 355c69e7fd8b
Revises: 46a146184195
Create Date: 2026-04-19 09:36:59.903058

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa



# revision identifiers, used by Alembic.
revision = '355c69e7fd8b'
down_revision = '46a146184195'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- sessions ----
    op.add_column(
        "sessions",
        sa.Column("luciel_instance_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_sessions_luciel_instance_id",
        "sessions",
        "luciel_instances",
        ["luciel_instance_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_sessions_luciel_instance_id",
        "sessions",
        ["luciel_instance_id"],
        unique=False,
    )

    # ---- traces ----
    op.add_column(
        "traces",
        sa.Column("luciel_instance_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_traces_luciel_instance_id",
        "traces",
        "luciel_instances",
        ["luciel_instance_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_traces_luciel_instance_id",
        "traces",
        ["luciel_instance_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_traces_luciel_instance_id", table_name="traces")
    op.drop_constraint(
        "fk_traces_luciel_instance_id", "traces", type_="foreignkey"
    )
    op.drop_column("traces", "luciel_instance_id")

    op.drop_index("ix_sessions_luciel_instance_id", table_name="sessions")
    op.drop_constraint(
        "fk_sessions_luciel_instance_id", "sessions", type_="foreignkey"
    )
    op.drop_column("sessions", "luciel_instance_id")