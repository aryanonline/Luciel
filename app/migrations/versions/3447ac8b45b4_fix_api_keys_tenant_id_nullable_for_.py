"""fix_api_keys_tenant_id_nullable_for_platformadmin

Revision ID: 3447ac8b45b4
Revises: 52e19e8ae552
Create Date: 2026-04-21 22:29:03.046573

Allows api_keys.tenant_id to be NULL for platform-admin keys.

Rationale (Invariant 5 — scope arithmetic only):
Platform-admin keys must have tenant_id=NULL so that cross-tenant bypass
is determined by the 'platformadmin' permission alone, not by scoping.
The original Step-18-era migration created tenant_id as NOT NULL, which
forced all platform-admin keys to carry a semantically-wrong tenant_id
(dev platform-admin keys on remax-crossroads were the workaround).

This is an additive, non-destructive DDL change:
  - No data movement
  - No backfill
  - No existing rows affected (they already have a tenant_id)
  - Reversible (downgrade re-asserts NOT NULL, which is safe as long as
    no NULL rows exist at downgrade time)

Safe to run against a DB with existing api_keys rows: PostgreSQL's
ALTER COLUMN ... DROP NOT NULL is a metadata-only change.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa



# revision identifiers, used by Alembic.
revision = '3447ac8b45b4'
down_revision = '52e19e8ae552'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "api_keys",
        "tenant_id",
        existing_type=sa.String(length=100),
        nullable=True,
    )


def downgrade() -> None:
    # WARNING: downgrade will fail if any api_keys row has tenant_id=NULL
    # at the time of downgrade. Platform-admin keys must be rotated
    # (or their tenant_id populated) before running this downgrade.
    op.alter_column(
        "api_keys",
        "tenant_id",
        existing_type=sa.String(length=100),
        nullable=False,
    )