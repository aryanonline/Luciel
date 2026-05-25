"""Arc 9.2 PR #96 — Additive admin_id column on 16 tenant_id tables (Option A step 1).

This migration is purely additive:
  1. ADD COLUMN admin_id VARCHAR(100) NULL  (per table)
  2. UPDATE table SET admin_id = tenant_id  (backfill)
  3. ALTER COLUMN admin_id SET NOT NULL
  4. ADD FK admin_id -> admins.id ON DELETE RESTRICT
  5. CREATE INDEX on admin_id

No existing column is touched. tenant_id remains intact (dropped later in PR #101).
Idempotent: every step checks existence first so reruns are safe.

Down-revision reverses additively (drop FK, drop index, drop column).

Revision ID: arc9_2_pr96_additive_admin_id
Revises: arc9_1_b1_rls_unprotected_tables
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "arc9_2_pr96_additive_admin_id"
down_revision = "arc9_1_b1_rls_unprotected_tables"
branch_labels = None
depends_on = None


# 16 tables that currently carry tenant_id (excludes admins itself, whose PK
# is already admins.id).
#
# Most tables require admin_id NOT NULL (every row is tenant-scoped).  Four
# tables (api_keys, knowledge_embeddings, retention_policies, deletion_logs)
# allow NULL tenant_id for legitimate platform-wide / cross-tenant rows
# (platform-admin API keys, shared domain knowledge, default retention
# policies, system deletion events).  admin_id mirrors that nullability.
TABLES_NOT_NULL: tuple[str, ...] = (
    "admin_audit_logs",
    "agent_configs",
    "conversations",
    "identity_claims",
    "memory_items",
    "messages",
    "scope_assignments",
    "sessions",
    "subscriptions",
    "traces",
    "user_consents",
    "user_invites",
)

TABLES_NULLABLE: tuple[str, ...] = (
    "api_keys",
    "knowledge_embeddings",
    "retention_policies",
    "deletion_logs",
)

TABLES: tuple[str, ...] = TABLES_NOT_NULL + TABLES_NULLABLE


def _fk_name(table: str) -> str:
    return f"fk_{table}_admin_id_admins"


def _ix_name(table: str) -> str:
    return f"ix_{table}_admin_id"


def upgrade() -> None:
    bind = op.get_bind()

    for table in TABLES:
        # Step 1 — ADD COLUMN admin_id (nullable for backfill window).
        bind.execute(
            sa.text(
                f"ALTER TABLE {table} "
                f"ADD COLUMN IF NOT EXISTS admin_id VARCHAR(100)"
            )
        )

        # Step 2 — Backfill from tenant_id.  Idempotent: only rows where
        # admin_id is still NULL get touched.  For NULLABLE tables, rows
        # with tenant_id IS NULL stay NULL (platform-wide rows).
        bind.execute(
            sa.text(
                f"UPDATE {table} SET admin_id = tenant_id "
                f"WHERE admin_id IS NULL AND tenant_id IS NOT NULL"
            )
        )

        # Step 3 — Enforce NOT NULL only on tables where tenant_id is NOT NULL.
        if table in TABLES_NOT_NULL:
            bind.execute(
                sa.text(
                    f"ALTER TABLE {table} ALTER COLUMN admin_id SET NOT NULL"
                )
            )

        # Step 4 — Add FK -> admins.id ON DELETE RESTRICT (idempotent).
        fk_name = _fk_name(table)
        bind.execute(
            sa.text(
                f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = '{fk_name}'
                    ) THEN
                        ALTER TABLE {table}
                            ADD CONSTRAINT {fk_name}
                            FOREIGN KEY (admin_id)
                            REFERENCES admins(id)
                            ON DELETE RESTRICT;
                    END IF;
                END $$;
                """
            )
        )

        # Step 5 — Index on admin_id (idempotent).
        ix_name = _ix_name(table)
        bind.execute(
            sa.text(
                f"CREATE INDEX IF NOT EXISTS {ix_name} "
                f"ON {table} (admin_id)"
            )
        )


def downgrade() -> None:
    bind = op.get_bind()

    # Reverse order (not strictly required since they're independent tables).
    for table in reversed(TABLES):
        ix_name = _ix_name(table)
        fk_name = _fk_name(table)

        bind.execute(
            sa.text(f"DROP INDEX IF EXISTS {ix_name}")
        )
        bind.execute(
            sa.text(
                f"ALTER TABLE {table} "
                f"DROP CONSTRAINT IF EXISTS {fk_name}"
            )
        )
        bind.execute(
            sa.text(
                f"ALTER TABLE {table} DROP COLUMN IF EXISTS admin_id"
            )
        )
