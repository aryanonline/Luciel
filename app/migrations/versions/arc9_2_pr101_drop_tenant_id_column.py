"""Arc 9.2 PR #101 — DROP tenant_id column on 15 tables (Option A final step).

PR #96 added admin_id additively and backfilled it from tenant_id.
PR #97 rewrote every Wall-1 RLS policy to filter on admin_id.
PR #98/#100 migrated the HTTP boundary so every caller emits admin_id.

This migration finishes the collapse:
  1. Drop the SECDEF helper `arc9_c20_resolve_tenant_for_user` (will be
     recreated as `arc9_c20_resolve_admin_for_user` reading admin_id).
  2. For each of the 15 tables:
     a. Drop every FK whose source column is `tenant_id`
     b. Drop every UNIQUE / CHECK constraint that includes `tenant_id`
        (must happen before index drops -- a UNIQUE constraint owns
        its backing index, and DROP INDEX errors out otherwise).
     c. Drop every remaining (non-constraint-owned) index on `tenant_id`
     d. DROP COLUMN tenant_id CASCADE
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

    # ---- 1. Drop legacy SECDEF functions that read tenant_id ------
    #
    # Both arc9_c20_resolve_tenant_for_user(uuid) and
    # arc9_c22_bootstrap_identity(uuid) SELECT from
    # scope_assignments.tenant_id.  They must be dropped BEFORE the
    # column drop so the column drop does not cascade-explode.
    bind.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS public.arc9_c20_resolve_tenant_for_user(uuid) CASCADE"
        )
    )
    bind.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS public.arc9_c22_bootstrap_identity(uuid) CASCADE"
        )
    )

    # ---- 2. Per-table: drop FK + UQs + indexes + column -----------
    for table in TABLES:
        _drop_tenant_id_on_table(table)

    # ---- 3. Recreate the C22 bootstrap function on admin_id -------
    #
    # Python caller (app/identity/bootstrap.py) now selects an
    # ``admin_id`` column.  The function signature is unchanged
    # (same uuid input, same number of columns, same order); only
    # the third-from-last column name changes from tenant_id to
    # admin_id, and the internal CTE filters on admin_id.
    bind.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION public.arc9_c22_bootstrap_identity(
                p_user_id uuid
            )
            RETURNS TABLE (
                canonical_tenant_id varchar,
                canonical_tier      varchar,
                scope_assignment_id uuid,
                admin_id            varchar,
                domain_id           varchar,
                role                varchar,
                started_at          timestamptz,
                ended_at            timestamptz,
                ended_reason        varchar,
                ended_note          text,
                ended_by_api_key_id integer,
                active              boolean
            )
            LANGUAGE sql
            STABLE
            SECURITY DEFINER
            SET search_path = public, pg_temp
            AS $$
                WITH active_scopes AS (
                    SELECT
                        sa.id,
                        sa.user_id,
                        sa.admin_id,
                        sa.domain_id,
                        sa.role,
                        sa.started_at,
                        sa.ended_at,
                        sa.ended_reason,
                        sa.ended_note,
                        sa.ended_by_api_key_id,
                        sa.active
                    FROM public.scope_assignments AS sa
                    WHERE sa.user_id = p_user_id
                      AND sa.active = true
                      AND sa.ended_at IS NULL
                ),
                canonical AS (
                    SELECT admin_id
                    FROM active_scopes
                    ORDER BY (role = 'owner') DESC, started_at DESC
                    LIMIT 1
                ),
                canonical_with_tier AS (
                    SELECT
                        c.admin_id AS aid,
                        COALESCE(a.tier, '')::varchar AS tier
                    FROM canonical c
                    LEFT JOIN public.admins a ON a.id = c.admin_id
                )
                SELECT
                    COALESCE((SELECT aid  FROM canonical_with_tier), '')::varchar,
                    COALESCE((SELECT tier FROM canonical_with_tier), '')::varchar,
                    s.id,
                    s.admin_id,
                    s.domain_id,
                    s.role,
                    s.started_at,
                    s.ended_at,
                    s.ended_reason,
                    s.ended_note,
                    s.ended_by_api_key_id,
                    s.active
                FROM active_scopes s
                ORDER BY s.started_at ASC
            $$;
            """
        )
    )
    # Preserve the original grant matrix (owner luciel_ops; EXECUTE
    # granted to luciel_app; REVOKE from PUBLIC).
    bind.execute(
        sa.text(
            "ALTER FUNCTION public.arc9_c22_bootstrap_identity(uuid) "
            "OWNER TO luciel_ops"
        )
    )
    bind.execute(
        sa.text(
            "REVOKE EXECUTE ON FUNCTION "
            "public.arc9_c22_bootstrap_identity(uuid) FROM PUBLIC"
        )
    )
    bind.execute(
        sa.text(
            "GRANT EXECUTE ON FUNCTION "
            "public.arc9_c22_bootstrap_identity(uuid) TO luciel_app"
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

    # 2b. Drop UNIQUE / CHECK constraints that reference tenant_id FIRST
    #     (must precede index drops -- a UNIQUE constraint owns its backing
    #     index, and Postgres refuses DROP INDEX on a constraint-owned
    #     index with DependentObjectsStillExist).
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

    # 2c. Drop remaining indexes that include tenant_id (single or
    #     composite).  Any UNIQUE-backing indexes were removed in 2b
    #     by their owning constraint; this catches the plain indexes.
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
                      AND NOT EXISTS (
                          -- Skip any index still owned by a constraint;
                          -- shouldn't happen after 2b but defensive.
                          SELECT 1 FROM pg_constraint c
                          WHERE c.conindid = ix.indexrelid
                      )
                LOOP
                    EXECUTE format('DROP INDEX IF EXISTS public.%I', r.index_name);
                END LOOP;
            END $$;
            """
        )
    )

    # 2d. Finally drop the column.  CASCADE catches any view, default,
    #     or trigger that still pins tenant_id.
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
                    EXECUTE 'ALTER TABLE public.{table} DROP COLUMN tenant_id CASCADE';
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
