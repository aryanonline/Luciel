"""step29x widen admin_audit_logs.action to String(64)

Revision ID: a1f29c7e4b08
Revises: 8ddf0be96f44
Create Date: 2026-05-07

Pillar 24 caught a production failure: the action constant
'luciel_instance_forensic_toggle' is 31 chars but the column was
String(30), causing StringDataRightTruncation on every forensic
toggle. Widen to 64 to fit current and reasonably-future actions.

Postgres ALTER COLUMN ... TYPE varchar(n) where n > old length is a
metadata-only change (no rewrite, no table lock beyond a brief
ACCESS EXCLUSIVE for the catalog update). Safe online.
"""
from alembic import op
import sqlalchemy as sa


revision = "a1f29c7e4b08"
down_revision = "8ddf0be96f44"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "admin_audit_logs",
        "action",
        existing_type=sa.String(length=30),
        type_=sa.String(length=64),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Reversible only if no row has action longer than 30 chars.
    # The forensic toggle action is 31 chars, so a true downgrade
    # would need data cleanup first. We document but do not enforce.
    op.alter_column(
        "admin_audit_logs",
        "action",
        existing_type=sa.String(length=64),
        type_=sa.String(length=30),
        existing_nullable=False,
    )
