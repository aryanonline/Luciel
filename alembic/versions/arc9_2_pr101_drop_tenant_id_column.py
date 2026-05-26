"""Arc 9.2 PR #101 — DROP tenant_id column on 15 tables (Option A final step).

PR #96 added admin_id additively and backfilled it from tenant_id.
PR #97 rewrote every Wall-1 RLS policy to filter on admin_id.
PR #98/#100 migrated the HTTP boundary so every caller emits admin_id.

This migration finishes the collapse:
  1. Drop the SECDEF helper `arc9_c20_resolve_tenant_for_user` (will be
     recreated as `arc9_c20_resolve_admin_for_user` reading admin_id).
  2. For each of the 15 tables:
     a. Drop every FK whose source column is `tenant_id`
     b. Drop every index on `tenant_id` (single-column and composite)
     c. Drop every UNIQUE / CHECK constraint that includes `tenant_id`
     d. DROP COLUMN tenant_id
  3. Recreate the SECDEF helper pointed at admin_id.

Idempotent: every drop guards with IF EXISTS / pg_constraint lookup.
Downgrade is forward-only (restore from backup).

Revision ID: arc9_2_pr101_drop_tenant_id_column
Revises: arc9_2_pr97_rls_to_admin_id
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "arc9_2_pr101_drop_tenant_id_column"
down_revision = "arc9_2_pr97_rls_to_admin_id"
branch_labels = None
depends_on = None


# 15 tables that carry tenant_id (matches PR #96).
TABLES: tuple[str, ...] = (
    "admin_audit_logs",
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
    "api_keys",
    "knowledge_embeddings",
    "retention_policies",
    "deletion_logs",
)


def upgrade() -> None:
    bind = op.get_bind()

    # ---- 1. Drop the legacy SECDEF function ----------------------
    bind.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS public.arc9_c20_resolve_tenant_for_user(text) CASCADE"
        )
    )
    bind.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS public.arc9_c20_resolve_tenant_for_user CASCADE"
        )
    )

    # ---- 2. Per-table: drop FK + indexes + UQs + column -----------
    for table in TABLES:
        _drop_tenant_id_on_table(table)

    # ---- 3. Recreate SECDEF helper pointed at admin_id ------------
    bind.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION public.arc9_c20_resolve_admin_for_user(p_user_id text)
            RETURNS text
            LANGUAGE sql
            SECURITY DEFINER
            STABLE
            AS $$
                SELECT admin_id
                FROM public.scope_assignments
                WHERE user_id = p_user_id
                  AND active = TRUE
                ORDER BY created_at ASC
                LIMIT 1;
            $$;
            """
        )
    )
    bind.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION public.arc9_c20_resolve_admin_for_user(text) FROM PUBLIC"
        )
    )


def _drop_tenant_id_on_table(table: str) -> None:
    """Drop every FK / index / UQ referencing tenant_id on `table`, then the column."""
    bind = op.get_bind()

    # 2a. Drop FKs sourced from this table's tenant_id column.
    bind.execute(
        sa.text(
            f"""
            DO $$
            DECLARE r record;
            BEGIN
                FOR r IN
                    SELECT con.conname
                    FROM pg_constraint con
                    JOIN pg_class cls ON cls.oid = con.conrelid
                    JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
                    WHERE cls.relname = '{table}'
                      AND nsp.nspname = 'public'
                      AND con.contype = 'f'
                      AND EXISTS (
                          SELECT 1
                          FROM pg_attribute att
                          WHERE att.attrelid = con.conrelid
                            AND att.attnum = ANY(con.conkey)
                            AND att.attname = 'tenant_id'
                      )
                LOOP
                    EXECUTE format('ALTER TABLE public.{table} DROP CONSTRAINT %I', r.conname);
                END LOOP;
            END $$;
            """
        )
    )

    # 2b. Drop indexes that include tenant_id (single or composite).
    bind.execute(
        sa.text(
            f"""
            DO $$
            DECLARE r record;
            BEGIN
                FOR r IN
                    SELECT i.relname AS index_name
                    FROM pg_class t
                    JOIN pg_namespace n ON n.oid = t.relnamespace
                    JOIN pg_index ix ON ix.indrelid = t.oid
                    JOIN pg_class i ON i.oid = ix.indexrelid
                    WHERE t.relname = '{table}'
                      AND n.nspname = 'public'
                      AND EXISTS (
                          SELECT 1
                          FROM pg_attribute a
                          WHERE a.attrelid = t.oid
                            AND a.attnum = ANY(ix.indkey)
                            AND a.attname = 'tenant_id'
                      )
                      AND NOT ix.indisprimary
                LOOP
                    EXECUTE format('DROP INDEX IF EXISTS public.%I', r.index_name);
                END LOOP;
            END $$;
            """
        )
    )

    # 2c. Drop UNIQUE / CHECK constraints that reference tenant_id.
    bind.execute(
        sa.text(
            f"""
            DO $$
            DECLARE r record;
            BEGIN
                FOR r IN
                    SELECT con.conname
                    FROM pg_constraint con
                    JOIN pg_class cls ON cls.oid = con.conrelid
                    JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
                    WHERE cls.relname = '{table}'
                      AND nsp.nspname = 'public'
                      AND con.contype IN ('u', 'c')
                      AND EXISTS (
                          SELECT 1
                          FROM pg_attribute att
                          WHERE att.attrelid = con.conrelid
                            AND att.attnum = ANY(con.conkey)
                            AND att.attname = 'tenant_id'
                      )
                LOOP
                    EXECUTE format('ALTER TABLE public.{table} DROP CONSTRAINT %I', r.conname);
                END LOOP;
            END $$;
            """
        )
    )

    # 2d. Finally drop the column.
    bind.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = '{table}'
                      AND column_name = 'tenant_id'
                ) THEN
                    EXECUTE 'ALTER TABLE public.{table} DROP COLUMN tenant_id';
                END IF;
            END $$;
            """
        )
    )


def downgrade() -> None:
    """Irreversible: we do not restore the tenant_id column.

    Downgrade is intentionally a no-op.  Backfill would require recovering
    values that no longer exist anywhere in the schema (the dual-write
    listener is also gone in this PR).  Use a database restore from the
    backup taken before this migration if rollback is needed.
    """
    raise NotImplementedError(
        "Arc 9.2 PR #101 is forward-only.  Restore from backup to rollback."
    )
